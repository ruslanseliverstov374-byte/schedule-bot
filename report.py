# -*- coding: utf-8 -*-
"""Отчёты по пользователям бота: сводки, списки и выгрузка в CSV.

Модуль нужен админ-панели: показать, сколько людей пользуется ботом, кто
заходил сегодня, и отдать список студентов файлом. Зависимостей нет — только
стандартная библиотека.

Две важные особенности:

  * базы открываются ТОЛЬКО на чтение (``file:...?mode=ro``), поэтому отчёт
    не мешает работающему боту и ничего в нём не меняет;
  * данные в базе «живые»: даты бывают пустыми и битыми, колонки — не все
    (например ``messages_count`` появилась позже), а таблицы может не быть
    вовсе. Ни одна функция модуля не падает на таком мусоре: непонятное
    значение превращается в ноль, пустую строку или прочерк.

Публичный API::

    find_databases(data_dir="data")      -> список баз ботов в папке
    summary(store, data_dir="data")      -> сводка по боту и соседним базам
    users_rows(store, limit, offset, order, query) -> строки для списка
    count_users(store, query="")         -> сколько пользователей нашлось
    users_csv(rows, bot_title, include_bot) -> CSV-строка (BOM, ';', CRLF)
    format_last_seen(value, now=None)    -> 'сегодня 14:03' и т.п.
    format_span(value, now=None)         -> '12 мин', '3 ч', '2 дн'

Пример::

    from store import Store
    import report

    store = Store("data/bot.db")
    data = report.summary(store, "data")
    rows = report.users_rows(store, order="last_seen", query="софа")
    text = report.users_csv(rows, bot_title="Поволжский ГУФКСиТ", include_bot=True)
"""

import csv
import io
import os
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta

# ------------------------------------------------------------------ константы

#: Понятные названия ботов: ключ — провайдер бота из meta.provider.
BOT_TITLES = {"unifirst": "Поволжский ГУФКСиТ", "kgasu": "КГАСУ"}

#: Заголовок выгрузки: первая колонка («бот») появляется только при include_bot.
CSV_HEADERS = ["бот", "tg_id", "имя", "username", "группа", "подгруппа",
               "тихий режим", "время вечера", "минут до пары", "сообщений",
               "личных заметок", "админ", "создан", "был(а) в боте"]

#: Прочерк для пустых и битых дат — так в отчёте сразу видно «данных нет».
DASH = "—"

#: Опорная дата для сортировки записей без last_seen/created_at.
_EPOCH = datetime(1970, 1, 1)

# Максимум групп в сводке: больше десяти строк админ всё равно не читает.
_TOP_GROUPS_LIMIT = 10

# Форматы даты-времени, которые встречаются в базе (плюс дата без времени).
_MOMENT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


# ------------------------------------------------------- мелкие безопасные helpers

def _as_text(value):
    """Строка без лишних пробелов; None и мусор превращаются в пустую строку."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _as_int(value, default=0):
    """Целое число из чего угодно; None, '' и мусор дают default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    text = _as_text(value).replace(",", ".")
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        return default


def _parse_moment(value):
    """Дата-время из строки базы или None, если строка пуста/битая.

    База хранит время строкой ``'2026-10-07 23:15:04'``, но встречаются и
    короткие значения (``'2026-10-07 23:15'``, ``'2026-10-07'``) — их тоже
    понимаем, а всё остальное честно считаем отсутствующим.
    """
    text = _as_text(value).replace("T", " ").strip()
    if not text:
        return None
    for candidate, template in zip((text[:19], text[:16], text[:10]), _MOMENT_FORMATS):
        try:
            return datetime.strptime(candidate, template)
        except ValueError:
            continue
    return None


def _has_time(value):
    """Есть ли в строке время (иначе показываем только день)."""
    text = _as_text(value)
    return len(text) > 10 and ":" in text[10:]


def _current_moment(now=None):
    """Приводит now к datetime: принимаем и datetime, и date, и None."""
    if isinstance(now, datetime):
        return now
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day)
    return datetime.now()


def _error_text(error):
    """Короткий текст ошибки без трассировки — его показываем админу."""
    text = str(error).strip()
    if not text:
        return error.__class__.__name__
    return ("%s: %s" % (error.__class__.__name__, text))[:200]


def _norm_path(path):
    """Путь в одном виде — чтобы сравнить базу бота с найденными в папке."""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return ""


def _date_ok(column):
    """SQL-условие «в колонке настоящая дата вида ГГГГ-ММ-ДД».

    Нужно, чтобы битые строки ('когда-то', '', NULL) не попадали в счётчики
    «за сегодня» и не всплывали наверх при сортировке по дате.
    """
    return ("substr(COALESCE(%s,''),1,10) GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'" % column)


def _bot_from_filename(path):
    """Имя бота по имени файла: bot.db -> unifirst, kgasu.db -> kgasu."""
    stem = os.path.splitext(os.path.basename(path))[0].strip().lower()
    if "kgasu" in stem:
        return "kgasu"
    if "unifirst" in stem:
        return "unifirst"
    if stem in ("", "bot"):
        return "unifirst"
    return stem


def _title_for(bot):
    """Человеческое название бота; для незнакомого имени — само имя."""
    return BOT_TITLES.get(bot) or bot or ""


def _truthy(value):
    """Правда ли значение (bool из кода, 1/0 из SQLite или строка из CSV)."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "нет", "no", "false", "выкл")
    return bool(value)


def _yes_no(value):
    return "да" if _truthy(value) else "нет"


def _is_quiet(row):
    """Тихий режим: пользователь выключил все уведомления.

    Дайджест вечером выключен, напоминаний перед парой нет и о заменах не
    сообщаем. Пустые значения (NULL) считаем нулями — так же, как сам бот
    читает настройки.
    """
    return (_as_int(row.get("evening_enabled"), 0) == 0
            and _as_int(row.get("before_minutes"), 0) == 0
            and _as_int(row.get("change_alerts"), 0) == 0)


# --------------------------------------------------- чтение базы только на чтение

def _readonly_uri(path):
    """URI для открытия базы только на чтение.

    Путь кодируем: в именах папок на Windows встречаются пробелы и решётки,
    а без кодирования SQLite понимает '#' как конец пути и открывает не тот файл.
    """
    absolute = os.path.abspath(path).replace("\\", "/")
    return "file:%s?mode=ro" % urllib.parse.quote(absolute, safe="/:")


def _open_readonly(path):
    """Соединение с базой только на чтение (без создания файла)."""
    connection = sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=1")
    except sqlite3.Error:
        pass
    return connection


def _scalar(connection, sql, params=(), default=0):
    """Одно число из запроса; 0, если таблицы нет или запрос не прошёл."""
    try:
        row = connection.execute(sql, params).fetchone()
    except Exception:
        return default
    if not row or row[0] is None:
        return default
    return _as_int(row[0], default)


def _text_scalar(connection, sql, params=(), default=""):
    """Одна строка из запроса; default, если пусто или запрос не прошёл."""
    try:
        row = connection.execute(sql, params).fetchone()
    except Exception:
        return default
    if not row or row[0] is None:
        return default
    return _as_text(row[0]) or default


def _inspect_database(path, today=""):
    """Что видно в базе по пути: пользователи, ДЗ, последний заход.

    Никогда не бросает исключение: у занятой, битой или чужой базы будет
    ``ok: False`` и понятный текст ошибки, а не падение отчёта.
    """
    info = {"path": path, "bot": _bot_from_filename(path), "title": "", "users": 0,
            "homework": 0, "last_seen": "", "active_today": 0, "ok": False, "error": ""}
    info["title"] = _title_for(info["bot"])

    try:
        connection = _open_readonly(path)
    except Exception as error:
        info["error"] = _error_text(error)
        return info

    try:
        # Первый же запрос показывает, база это или мусор: файл читается лениво.
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except Exception as error:
        info["error"] = _error_text(error)
        info["ok"] = False
        return info

    try:
        provider = _text_scalar(connection, "SELECT value FROM meta WHERE key='provider'")
        if provider:
            info["bot"] = provider
            info["title"] = _title_for(provider)
        # Всё остальное — по отдельности: нет таблицы, значит ноль, а не ошибка.
        info["users"] = _scalar(connection, "SELECT COUNT(*) FROM users")
        info["homework"] = _scalar(connection, "SELECT COUNT(*) FROM homework")
        info["last_seen"] = _text_scalar(
            connection,
            "SELECT MAX(last_seen) FROM users WHERE " + _date_ok("last_seen"))
        if today:
            info["active_today"] = _scalar(
                connection,
                "SELECT COUNT(*) FROM users WHERE " + _date_ok("last_seen")
                + " AND substr(last_seen,1,10)=?", (today,))
        info["ok"] = True
        info["error"] = ""
    except Exception as error:                     # на всякий случай: не падаем
        info["ok"] = False
        info["error"] = _error_text(error)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return info


def _scan_databases(data_dir="data"):
    """Полные сведения по всем ``*.db`` папки (внутренняя, с активными за сегодня)."""
    result = []
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        names = sorted(os.listdir(data_dir))
    except Exception:
        return result                                   # папки нет — просто пусто
    for name in names:
        lowered = str(name).lower()
        if not lowered.endswith(".db"):
            continue                                    # журналы -wal/-shm пропускаем
        # Базы тестов и проверок лежат рядом, но ботами не являются: иначе они
        # попадали бы в сводку «другие боты в этом сервисе».
        if lowered.startswith(("check", "simulate", "test", "debug", "healthcheck")):
            continue
        path = os.path.join(data_dir, name)
        try:
            if not os.path.isfile(path):
                continue
        except OSError:
            continue
        result.append(_inspect_database(path, today))
    return result


# ------------------------------------------------------------------ публичный API

def find_databases(data_dir="data"):
    """Список баз ботов в папке: ``[{'path', 'bot', 'title', 'users', 'homework',
    'last_seen', 'ok', 'error'}]``.

    В одной папке ``data/`` обычно лежат две базы: ``bot.db`` (ПГУФКСиТ) и
    ``kgasu.db`` (КГАСУ). Имя бота берётся из ``meta.provider``, а если там
    пусто — из имени файла. База читается только на чтение, поэтому занятый
    файл и битый файл дают ``ok: False`` с текстом ошибки, а не исключение.
    Отсутствующая папка — пустой список.
    """
    keys = ("path", "bot", "title", "users", "homework", "last_seen", "ok", "error")
    return [{key: info.get(key) for key in keys} for info in _scan_databases(data_dir)]


def _store_path(store):
    """Путь к базе бота — по нему отличаем свою базу от соседних."""
    for attribute in ("path", "db_path"):
        value = getattr(store, attribute, "")
        if isinstance(value, str) and value:
            return value
    return ""


def _query(store, sql, params=()):
    """Строки запроса как словари; любая ошибка базы — пустой список."""
    try:
        rows = store.query(sql, params)
    except Exception:
        return []
    result = []
    for row in rows or []:
        if isinstance(row, dict):
            result.append(row)
            continue
        try:
            result.append(dict(row))
        except Exception:
            continue
    return result


def _count(store, sql, params=()):
    """Число из запроса с колонкой ``n``; при любой ошибке — 0."""
    rows = _query(store, sql, params)
    if not rows:
        return 0
    return _as_int(rows[0].get("n"), 0)


def _users_columns(store):
    """Какие колонки есть в таблице users (нужно для messages_count)."""
    try:
        rows = store.query("PRAGMA table_info(users)") or []
    except Exception:
        rows = []
    names = set()
    for row in rows:
        if isinstance(row, dict):
            name = row.get("name")
        else:
            try:
                name = row[1]
            except Exception:
                name = None
        if name:
            names.add(str(name))
    return names


def _top_groups(store, limit=_TOP_GROUPS_LIMIT):
    """Самые крупные группы: ``[{'group': '26281', 'users': 12}]``."""
    rows = _query(
        store,
        "SELECT COALESCE(group_title,'') AS grp, COUNT(*) AS n FROM users"
        " WHERE TRIM(COALESCE(group_title,''))<>''"
        " GROUP BY COALESCE(group_title,'') ORDER BY n DESC, grp LIMIT ?",
        (limit,))
    result = []
    for row in rows:
        name = _as_text(row.get("grp"))
        if not name:
            continue
        result.append({"group": name, "users": _as_int(row.get("n"), 0)})
    return result[:limit]


def _other_bots(store, data_dir):
    """Краткие сводки по остальным базам в той же папке.

    База самого бота в список не попадает — по ней есть основной блок сводки.
    """
    own = _norm_path(_store_path(store))
    others = []
    for info in _scan_databases(data_dir):
        if own and _norm_path(info["path"]) == own:
            continue
        others.append({"bot": info["bot"], "title": info["title"], "users": info["users"],
                       "active_today": info["active_today"], "last_seen": info["last_seen"],
                       "ok": info["ok"], "error": info["error"]})
    return others


def summary(store, data_dir="data"):
    """Сводка по этому боту плюс краткий блок по остальным ботам в папке.

    Возвращает словарь: ``total``, ``with_group``, ``quiet``, ``admins``,
    ``today``, ``week``, ``new_today``, ``new_week``, ``active_today``,
    ``active_week``, ``messages``, ``homework_group``, ``homework_personal``,
    ``homework_open``, ``top_groups`` (до 10, по убыванию), ``others``.

    ``today``/``active_today`` — пользователи с ``last_seen`` за сегодняшний
    календарный день, ``week``/``active_week`` — за последние 7 суток,
    ``new_*`` — по ``created_at``. Колонки ``messages_count`` может не быть —
    тогда ``messages`` равно нулю. На пустой базе все числа нули, а списки пусты.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    seen_ok = _date_ok("last_seen")
    created_ok = _date_ok("created_at")
    columns = _users_columns(store)

    data = {
        "total": _count(store, "SELECT COUNT(*) AS n FROM users"),
        "with_group": _count(
            store, "SELECT COUNT(*) AS n FROM users"
                   " WHERE TRIM(COALESCE(group_title,''))<>''"),
        "quiet": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE COALESCE(evening_enabled,0)=0"
                   " AND COALESCE(before_minutes,0)=0 AND COALESCE(change_alerts,0)=0"),
        "admins": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE COALESCE(is_admin,0)<>0"),
        "today": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE " + seen_ok
                   + " AND substr(last_seen,1,10)=?", (today,)),
        "week": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE " + seen_ok
                   + " AND substr(last_seen,1,10)>=?", (week_start,)),
        "new_today": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE " + created_ok
                   + " AND substr(created_at,1,10)=?", (today,)),
        "new_week": _count(
            store, "SELECT COUNT(*) AS n FROM users WHERE " + created_ok
                   + " AND substr(created_at,1,10)>=?", (week_start,)),
        "messages": (_count(store, "SELECT COALESCE(SUM(messages_count),0) AS n FROM users")
                     if "messages_count" in columns else 0),
        "homework_group": _count(
            store, "SELECT COUNT(*) AS n FROM homework WHERE scope='group'"),
        "homework_personal": _count(
            store, "SELECT COUNT(*) AS n FROM homework WHERE scope='personal'"),
        "homework_open": _count(
            store, "SELECT COUNT(*) AS n FROM homework WHERE status='open'"),
        "top_groups": _top_groups(store),
        "others": _other_bots(store, data_dir),
    }
    # «Активные» — то же самое, что «за сегодня/неделю»: держим оба имени,
    # потому что ими пользуются разные экраны админки.
    data["active_today"] = data["today"]
    data["active_week"] = data["week"]
    return data


def _homework_counts(store):
    """Сколько личных заметок у каждого пользователя: ``{tg_id: count}``."""
    counts = {}
    for row in _query(store, "SELECT owner_id AS owner, COUNT(*) AS n FROM homework"
                             " WHERE owner_id IS NOT NULL GROUP BY owner_id"):
        owner = _as_int(row.get("owner"), None)
        if owner is None:
            continue
        counts[owner] = _as_int(row.get("n"), 0)
    return counts


def _user_item(row, homework_counts, columns):
    """Одна строка отчёта по пользователю (без служебных полей сортировки)."""
    tg_id = _as_int(row.get("tg_id"), 0)
    return {
        "tg_id": tg_id,
        "name": _as_text(row.get("first_name")) or "без имени",
        "username": _as_text(row.get("username")).lstrip("@"),
        "group": _as_text(row.get("group_title")),
        "subgroup": _as_text(row.get("subgroup")),
        "quiet": _is_quiet(row),
        "evening_time": _as_text(row.get("evening_time")),
        "before_minutes": _as_int(row.get("before_minutes"), 0),
        "created_at": _as_text(row.get("created_at")),
        "last_seen": _as_text(row.get("last_seen")),
        "messages": (_as_int(row.get("messages_count"), 0)
                     if "messages_count" in columns else 0),
        "homework": homework_counts.get(tg_id, 0),
        "is_admin": _as_int(row.get("is_admin"), 0) != 0,
        "_seen_at": _parse_moment(row.get("last_seen")),
        "_created_at": _parse_moment(row.get("created_at")),
    }


def _user_rows(store, query=""):
    """Все подходящие пользователи (для списка, выгрузки и счётчика).

    Поиск идёт в Python: SQLite-функция LIKE не понимает регистр кириллицы,
    поэтому «софа» не находило бы «Софу». Пользователей у бота немного, так
    что это ещё и надёжнее.
    """
    columns = _users_columns(store)
    homework_counts = _homework_counts(store)
    rows = []
    for row in _query(store, "SELECT * FROM users"):
        try:
            rows.append(_user_item(row, homework_counts, columns))
        except Exception:
            continue                       # одна битая строка не ломает список

    needle = _as_text(query).lower()
    if needle:
        rows = [item for item in rows
                if needle in item["name"].lower()
                or needle in item["username"].lower()
                or needle in item["group"].lower()]
    return rows


def users_rows(store, limit=None, offset=0, order="last_seen", query=""):
    """Строки для списка и выгрузки.

    Поля: ``tg_id``, ``name`` (имя или «без имени»), ``username`` (без '@'),
    ``group``, ``subgroup``, ``quiet``, ``evening_time``, ``before_minutes``,
    ``created_at``, ``last_seen``, ``messages`` (0, если колонки нет),
    ``homework`` (личные заметки), ``is_admin``.

    ``order``: ``'last_seen'`` (сначала недавние), ``'created'`` (сначала новые),
    ``'group'`` (по названию группы), ``'name'`` (по имени). ``query`` — подстрока
    для поиска по имени, username и группе без учёта регистра. ``limit``/``offset``
    — постранично; ``limit`` пустой или ≤ 0 значит «без ограничения».
    """
    rows = _user_rows(store, query)
    mode = _as_text(order).lower() or "last_seen"
    if mode == "created":
        rows.sort(key=lambda item: (item["_created_at"] is not None,
                                    item["_created_at"] or _EPOCH), reverse=True)
    elif mode == "group":
        # Пустая группа — в конец: такие пользователи ещё не выбрали группу.
        rows.sort(key=lambda item: (0 if item["group"] else 1,
                                    item["group"].lower(), item["name"].lower()))
    elif mode == "name":
        rows.sort(key=lambda item: (item["name"].lower(), item["tg_id"]))
    else:
        rows.sort(key=lambda item: (item["_seen_at"] is not None,
                                    item["_seen_at"] or _EPOCH), reverse=True)

    start = max(_as_int(offset, 0), 0)
    if start:
        rows = rows[start:]
    size = _as_int(limit, 0)
    if size > 0:
        rows = rows[:size]

    for item in rows:
        item.pop("_seen_at", None)
        item.pop("_created_at", None)
    return rows


def count_users(store, query=""):
    """Сколько всего пользователей — с учётом того же поиска query."""
    return len(_user_rows(store, query))


def users_csv(rows, bot_title="", include_bot=False):
    """Выгрузка списка в CSV-строку: UTF-8 BOM, разделитель ';', переводы CRLF.

    Колонки: бот (если ``include_bot``), tg_id, имя, username, группа, подгруппа,
    тихий режим (да/нет), время вечера, минут до пары, сообщений, личных заметок,
    админ (да/нет), создан, был(а) в боте. Значения экранирует модуль csv, поэтому
    точка с запятой, кавычки и перевод строки внутри имени файл не портят.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    header = list(CSV_HEADERS)
    if not include_bot:
        header = header[1:]
    writer.writerow(header)

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            values = []
            if include_bot:
                # У строки может быть своё название бота — тогда оно важнее общего.
                values.append(_as_text(row.get("bot_title")) or _as_text(bot_title))
            values.extend([
                _as_int(row.get("tg_id"), 0),
                _as_text(row.get("name")) or "без имени",
                _as_text(row.get("username")),
                _as_text(row.get("group")),
                _as_text(row.get("subgroup")),
                _yes_no(row.get("quiet")),
                _as_text(row.get("evening_time")),
                _as_int(row.get("before_minutes"), 0),
                _as_int(row.get("messages"), 0),
                _as_int(row.get("homework"), 0),
                _yes_no(row.get("is_admin")),
                _as_text(row.get("created_at")),
                _as_text(row.get("last_seen")),
            ])
            writer.writerow(values)
        except Exception:
            continue                       # одна испорченная строка не ломает выгрузку

    return "\ufeff" + buffer.getvalue()


def format_last_seen(value, now=None):
    """Когда пользователя видели: ``'сегодня 14:03'``, ``'вчера 20:11'``,
    ``'07.10.2026'``, а для пустой или битой строки — ``'—'``."""
    moment = _parse_moment(value)
    if moment is None:
        return DASH
    current = _current_moment(now)
    day = moment.date()
    today = current.date()
    with_time = _has_time(value)
    if day == today:
        return ("сегодня %s" % moment.strftime("%H:%M")) if with_time else "сегодня"
    if day == today - timedelta(days=1):
        return ("вчера %s" % moment.strftime("%H:%M")) if with_time else "вчера"
    return moment.strftime("%d.%m.%Y")


def format_span(value, now=None):
    """Сколько времени прошло: ``'только что'``, ``'12 мин'``, ``'3 ч'``,
    ``'2 дн'``, а для пустой или битой строки — ``'—'``.

    Время «из будущего» (часы на сервере разошлись) считаем «только что»:
    показывать админу минус пять минут бессмысленно.
    """
    moment = _parse_moment(value)
    if moment is None:
        return DASH
    seconds = (_current_moment(now) - moment).total_seconds()
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return "%d мин" % int(seconds // 60)
    if seconds < 86400:
        return "%d ч" % int(seconds // 3600)
    return "%d дн" % int(seconds // 86400)
