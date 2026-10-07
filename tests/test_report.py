# -*- coding: utf-8 -*-
"""Проверки модуля отчётов (report.py) — офлайн, на временных базах.

Запуск из корня проекта::

    python tests/test_report.py

Тест не ходит в интернет и не трогает базы бота: он создаёт временную папку,
заводит в ней две базы (``bot.db`` и ``kgasu.db``), сеет пользователей с
разными датами и проверяет сводку, сортировки, поиск, CSV и подписи дат.
Отдельно проверяются битые входы: несуществующая папка, файл с мусором
вместо базы, база без колонки ``messages_count`` и «магазин» без таблицы users.

Итог печатается строками ``Проверок: N, провалов: M`` и ``OK: N проверок``,
код возврата 0 — всё прошло, 1 — есть ошибки.
"""

import csv
import gc
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

try:  # чтобы русские подписи и эмодзи не ломали вывод в старой консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import report                                              # noqa: E402
from store import Store                                    # noqa: E402

# Опорное «сейчас»: все даты в тесте считаются от него, поэтому тест не зависит
# от дня запуска.
NOW = datetime.now()
TODAY = NOW.date()

CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    """Записывает результат одной проверки."""
    CHECKS.append(name)
    ok = bool(condition)
    if ok:
        print("  ✅ %s" % name)
    else:
        print("  ❌ %s %s" % (name, ("— " + str(detail)) if detail else ""))
        FAILURES.append("%s %s" % (name, detail))
    return ok


def stamp(days_ago, hour=12, minute=0):
    """Строка времени как в базе: сегодня минус N суток."""
    base = datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute)
    return (base - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def day_text(days_ago):
    """Только дата (без времени) — так тоже бывает в базе."""
    return stamp(days_ago)[:10]


def ids(rows):
    return [row["tg_id"] for row in rows]


def row_of(rows, tg_id):
    for row in rows:
        if row["tg_id"] == tg_id:
            return row
    return {}


# --------------------------------------------------------------- временная папка

def _probe_folder(directory):
    """Проверяет кандидата на роль временной папки: (можно писать, можно убрать).

    Проверяем и каталог, и SQLite (ему нужны блокировки файла), и уборку: в
    песочницах системный TEMP бывает доступен на запись, но созданную папку
    потом нельзя удалить — тогда после теста остаётся мусор, поэтому ищем
    место, где уборка действительно работает.
    """
    probe_dir = os.path.join(directory, "probe-dir")
    try:
        os.makedirs(probe_dir, exist_ok=True)
        connection = sqlite3.connect(os.path.join(probe_dir, "probe.db"))
        try:
            connection.execute("CREATE TABLE probe(x INTEGER)")
            connection.commit()
        finally:
            connection.close()
    except Exception:
        shutil.rmtree(probe_dir, ignore_errors=True)
        return False, False
    shutil.rmtree(probe_dir, ignore_errors=True)
    return True, not os.path.exists(probe_dir)


def make_workdir():
    """Временная папка для теста; удаляется в конце.

    Сначала пробуем системный TEMP, затем папки рядом с проектом (как делают
    другие тесты проекта). Берём первого кандидата, где работает и запись,
    и уборка; если такого нет — работаем там, где хотя бы можно писать.
    """
    candidates = [tempfile.mkdtemp(prefix="report-test-")]
    for base in (os.path.dirname(ROOT), os.path.join(ROOT, "data")):
        try:
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, "report-test-%s" % uuid.uuid4().hex[:8])
            os.mkdir(path)
            candidates.append(path)
        except OSError:
            continue

    best = ""
    for path in candidates:
        writable, removable = _probe_folder(path)
        if writable and removable:
            best = path
            break
        if writable and not best:
            best = path                      # запасной вариант: писать можно
    for path in candidates:
        if path != best:
            shutil.rmtree(path, ignore_errors=True)
    if not best:
        raise RuntimeError("не удалось создать временную папку для теста")
    return best


# ------------------------------------------------------------------- засев базы

#: Пользователи основного бота. Даты — «дней назад, час, минута», seen=None значит пусто.
BOT_USERS = [
    {"tg_id": 101, "username": "sofa_student", "name": "Софа", "group": "26281",
     "subgroup": "Подгруппа 1", "admin": 1, "evening": 1, "before": 15, "change": 1,
     "evening_time": "20:00", "created": (0, 10, 0), "seen": (0, 14, 3)},
    # Имя со точкой с запятой и кавычками — проверяем экранирование в CSV.
    {"tg_id": 102, "username": "ivan", "name": "Иван; \"Тест\"", "group": "26281",
     "subgroup": "", "admin": 0, "evening": 0, "before": 0, "change": 0,
     "evening_time": "20:00", "created": (2, 9, 0), "seen": (1, 20, 11)},
    {"tg_id": 103, "username": "", "name": "Пётр", "group": "26ЗК01з",
     "subgroup": "", "admin": 0, "evening": 1, "before": 30, "change": 1,
     "evening_time": "21:30", "created": (10, 8, 0), "seen": (10, 12, 0)},
    # Без имени и без группы, дата захода пустая, все уведомления выключены.
    {"tg_id": 104, "username": "@noname", "name": "", "group": "",
     "subgroup": "", "admin": 0, "evening": 0, "before": 0, "change": 0,
     "evening_time": "", "created": (0, 9, 0), "seen": None},
    {"tg_id": 105, "username": "boss", "name": "Админ", "group": "26281",
     "subgroup": "", "admin": 1, "evening": 1, "before": 10, "change": 1,
     "evening_time": "19:00", "created": (3, 11, 0), "seen": (0, 9, 0)},
]

KGASU_USERS = [
    {"tg_id": 201, "username": "k_student", "name": "Алия", "group": "26РП01",
     "subgroup": "", "admin": 1, "evening": 1, "before": 0, "change": 1,
     "evening_time": "20:00", "created": (0, 8, 0), "seen": (0, 11, 0)},
    {"tg_id": 202, "username": "", "name": "Марат", "group": "26РП01",
     "subgroup": "", "admin": 0, "evening": 1, "before": 15, "change": 1,
     "evening_time": "20:00", "created": (5, 8, 0), "seen": (3, 11, 0)},
    {"tg_id": 203, "username": "old", "name": "Гость", "group": "",
     "subgroup": "", "admin": 0, "evening": 0, "before": 0, "change": 0,
     "evening_time": "", "created": (30, 8, 0), "seen": None},
]


def seed_users(store, users):
    """Кладёт пользователей прямо в базу — с точными датами создания и захода."""
    for user in users:
        created = stamp(*user["created"])
        seen = stamp(*user["seen"]) if user["seen"] else ""
        store.execute(
            "INSERT INTO users(tg_id, username, first_name, group_title, subgroup,"
            " is_admin, evening_enabled, evening_time, before_minutes, change_alerts,"
            " hw_alerts, created_at, updated_at, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["tg_id"], user["username"], user["name"], user["group"], user["subgroup"],
             user["admin"], user["evening"], user["evening_time"], user["before"],
             user["change"], 1, created, created, seen))


def seed_bot_db(path, title_provider="unifirst"):
    """База основного бота: 5 пользователей, общее ДЗ и личная заметка."""
    store = Store(path)
    store.set_meta("provider", title_provider)
    seed_users(store, BOT_USERS)
    store.add_homework("26281", "История", "§§1-2, вопросы 3-5", due_date="2030-01-15")
    store.add_homework("26ЗК01з", "Анатомия", "конспект главы 4",
                       scope="personal", owner_id=103)
    return store


def seed_kgasu_db(path):
    """База второго бота: 3 пользователя, один заходил сегодня."""
    store = Store(path)
    store.set_meta("provider", "kgasu")
    seed_users(store, KGASU_USERS)
    return store


def make_minimal_db(path, with_messages):
    """База «чужой» схемы: урезанная таблица users; messages_count — по желанию.

    Так проверяем устойчивость: колонок, которые читает отчёт, может не быть,
    а значения бывают NULL и «когда-то» вместо даты.
    """
    schema = {"tg_id": "INTEGER PRIMARY KEY", "username": "TEXT", "first_name": "TEXT",
              "group_title": "TEXT", "subgroup": "TEXT", "is_admin": "INTEGER",
              "evening_enabled": "INTEGER", "evening_time": "TEXT",
              "before_minutes": "INTEGER", "change_alerts": "INTEGER",
              "created_at": "TEXT", "last_seen": "TEXT"}
    if with_messages:
        schema["messages_count"] = "INTEGER"
    names = list(schema)
    definition = ", ".join("%s %s" % (name, schema[name]) for name in names)
    statement = "INSERT INTO users(%s) VALUES(%s)" % (
        ", ".join(names), ", ".join("?" for _ in names))
    first = {"tg_id": 1, "username": "masha", "first_name": "Маша", "group_title": "26281",
             "subgroup": "", "is_admin": 0, "evening_enabled": 1, "evening_time": "20:00",
             "before_minutes": 0, "change_alerts": 1, "created_at": stamp(0, 8, 0),
             "last_seen": stamp(0, 12, 0), "messages_count": 7}
    # NULL в имени и группе плюс битая дата — отчёт обязан это пережить.
    second = {"tg_id": 2, "created_at": "когда-то", "last_seen": "когда-то",
              "messages_count": 3}
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE users(%s)" % definition)
        for row in (first, second):
            connection.execute(statement, tuple(row.get(name) for name in names))
        connection.commit()
    finally:
        connection.close()
    return path


class GhostStore:
    """«Магазин» без таблицы users: любые запросы падают — модуль не должен."""

    def __init__(self, path="нет-такой-базы.db"):
        self.path = path

    def query(self, sql, params=()):
        raise sqlite3.OperationalError("no such table: users")


class SpelledStore:
    """Тот же магазин, но путь к базе записан иначе ('.', обратные слэши).

    Нужен, чтобы проверить: своя база не попадает в блок «другие боты», даже
    если её путь записан по-разному в store и в найденном файле.
    """

    def __init__(self, store, path):
        self._store = store
        self.path = path

    def query(self, sql, params=()):
        return self._store.query(sql, params)


# ----------------------------------------------------- 1. find_databases

def check_find_databases(main_dir, broken_dir, plain_dir):
    print("\n1. find_databases: базы в папке")
    found = report.find_databases(main_dir)
    names = [os.path.basename(item["path"]) for item in found]
    check("найдены обе базы в папке", names == ["bot.db", "kgasu.db"], names)
    check("только файлы .db (журналы WAL не считаются)",
          all(name.endswith(".db") for name in names), names)

    bot = found[0] if found else {}
    kgasu = found[1] if len(found) > 1 else {}
    check("bot: имя бота взято из meta.provider", bot.get("bot") == "unifirst", bot)
    check("bot: человеческое название бота",
          bot.get("title") == "Поволжский ГУФКСиТ", bot.get("title"))
    check("bot: пользователей, ДЗ и последний заход",
          bot.get("users") == 5 and bot.get("homework") == 2
          and bot.get("last_seen") == stamp(0, 14, 3) and bot.get("ok") is True
          and bot.get("error") == "", bot)
    check("kgasu: имя и название второго бота",
          kgasu.get("bot") == "kgasu" and kgasu.get("title") == "КГАСУ", kgasu)
    check("kgasu: свои числа (3 пользователя, заход сегодня)",
          kgasu.get("users") == 3 and kgasu.get("last_seen") == stamp(0, 11, 0), kgasu)

    missing = report.find_databases(os.path.join(main_dir, "нет-такой-папки"))
    check("несуществующая папка — пустой список, без исключения", missing == [], missing)
    check("путь None тоже не ломает поиск", report.find_databases(None) == [])

    broken = report.find_databases(broken_dir)
    broken_names = [os.path.basename(item["path"]) for item in broken]
    check("битая база попала в список рядом с чужой", broken_names == ["broken.db", "foreign.db"],
          broken_names)
    bad = broken[0] if broken else {}
    check("битый файл: ok=False и понятный текст ошибки",
          bad.get("ok") is False and bool(bad.get("error")) and bad.get("users") == 0
          and bad.get("homework") == 0, bad)
    check("битый файл: имя бота выведено из имени файла", bad.get("bot") == "broken", bad)
    foreign = broken[1] if len(broken) > 1 else {}
    check("чужая база без таблицы users читается: ok=True и нули",
          foreign.get("ok") is True and foreign.get("users") == 0
          and foreign.get("error") == "", foreign)

    minimal = report.find_databases(plain_dir)
    check("урезанная схема без messages_count читается", len(minimal) == 1
          and minimal[0]["ok"] is True and minimal[0]["users"] == 2, minimal)


# ------------------------------------------------------- 2. users_rows и поиск

def check_users_rows(store):
    print("\n2. users_rows: сортировки, поиск, пагинация")
    rows = report.users_rows(store)
    check("без параметров вернулись все пять пользователей", len(rows) == 5, ids(rows))
    check("порядок по умолчанию — недавние сверху",
          ids(rows) == [101, 105, 102, 103, 104], ids(rows))
    check("пользователь с пустой датой захода — в конце", rows[-1]["tg_id"] == 104, ids(rows))

    created = report.users_rows(store, order="created")
    check("сортировка 'created': сначала новые",
          ids(created) == [101, 104, 102, 105, 103], ids(created))

    by_group = report.users_rows(store, order="group")
    groups = [row["group"] for row in by_group]
    check("сортировка 'group': по названию, без группы — в конце",
          groups == ["26281", "26281", "26281", "26ЗК01з", ""], groups)
    check("сортировка 'group': внутри группы по имени",
          ids(by_group) == [105, 102, 101, 103, 104], ids(by_group))

    by_name = report.users_rows(store, order="name")
    check("сортировка 'name': по имени без учёта регистра",
          ids(by_name) == [105, 104, 102, 103, 101], ids(by_name))

    check("неизвестный порядок не ломает список",
          len(report.users_rows(store, order="как-нибудь")) == 5)

    found = report.users_rows(store, query="софа")
    check("поиск в нижнем регистре находит «Софу»", ids(found) == [101], ids(found))
    check("count_users считает то же самое", report.count_users(store, "софа") == 1)
    check("поиск по username находит без '@'", ids(report.users_rows(store, query="boss")) == [105])
    check("поиск «админ» находит «Админ» (кириллица без учёта регистра)",
          ids(report.users_rows(store, query="админ")) == [105])
    check("поиск по названию группы находит троих",
          report.count_users(store, "26281") == 3, report.count_users(store, "26281"))
    check("поиск без совпадений — пусто",
          report.users_rows(store, query="неттакого") == []
          and report.count_users(store, "неттакого") == 0)
    check("пустой запрос возвращает всех", report.count_users(store, "") == 5
          and report.count_users(store) == 5)

    first_page = report.users_rows(store, limit=2)
    second_page = report.users_rows(store, limit=2, offset=2)
    third_page = report.users_rows(store, limit=2, offset=4)
    check("пагинация: страницы идут по порядку", ids(first_page) == [101, 105]
          and ids(second_page) == [102, 103] and ids(third_page) == [104],
          (ids(first_page), ids(second_page), ids(third_page)))
    check("пагинация: страницы не пересекаются и покрывают всех",
          sorted(ids(first_page) + ids(second_page) + ids(third_page)) == [101, 102, 103, 104, 105])
    check("offset без limit отбрасывает начало",
          ids(report.users_rows(store, offset=4)) == [104])
    check("limit больше списка не мешает", len(report.users_rows(store, limit=99)) == 5)

    sofa = row_of(rows, 101)
    check("поля строки: имя, username без '@', группа, подгруппа",
          sofa.get("name") == "Софа" and sofa.get("username") == "sofa_student"
          and sofa.get("group") == "26281" and sofa.get("subgroup") == "Подгруппа 1", sofa)
    check("поля строки: настройки напоминаний и админ",
          sofa.get("evening_time") == "20:00" and sofa.get("before_minutes") == 15
          and sofa.get("is_admin") is True and sofa.get("quiet") is False, sofa)
    check("поля строки: messages=0 без колонки messages_count",
          sofa.get("messages") == 0, sofa)
    check("тихий режим: у 102 и 104 он включён, у остальных нет",
          [row["tg_id"] for row in rows if row["quiet"]] == [102, 104],
          [row["tg_id"] for row in rows if row["quiet"]])
    check("личные заметки считаются только у владельца",
          row_of(rows, 103).get("homework") == 1 and row_of(rows, 102).get("homework") == 0,
          [(row["tg_id"], row["homework"]) for row in rows])
    check("пользователь без имени показан как «без имени»",
          row_of(rows, 104).get("name") == "без имени"
          and row_of(rows, 104).get("last_seen") == ""
          and row_of(rows, 104).get("is_admin") is False, row_of(rows, 104))
    check("в строке нет служебных полей сортировки",
          all("_seen_at" not in row and "_created_at" not in row for row in rows))


# ------------------------------------------------------------------ 3. summary

def check_summary(store, kgasu_store, main_dir, empty_dir):
    print("\n3. summary: сводка по боту и соседние базы")
    data = report.summary(store, main_dir)
    check("всего пользователей, с группой, админов",
          data["total"] == 5 and data["with_group"] == 4 and data["admins"] == 2, data)
    check("тихих ровно двое", data["quiet"] == 2, data)
    check("за сегодня заходили двое (101 и 105)", data["today"] == 2, data)
    check("за последние 7 суток заходили трое", data["week"] == 3, data)
    check("новых сегодня двое (101 и 104)", data["new_today"] == 2, data)
    check("новых за неделю четверо", data["new_week"] == 4, data)
    check("active_today/active_week повторяют today/week",
          data["active_today"] == data["today"] and data["active_week"] == data["week"], data)
    check("messages=0, когда колонки messages_count нет", data["messages"] == 0, data)
    check("ДЗ: одно общее, одна личная заметка, обе записи открыты",
          data["homework_group"] == 1 and data["homework_personal"] == 1
          and data["homework_open"] == 2, data)
    check("top_groups: крупная группа первой, пустая не попала",
          data["top_groups"] == [{"group": "26281", "users": 3},
                                 {"group": "26ЗК01з", "users": 1}], data["top_groups"])
    check("others: вторая база с числами и названием",
          len(data["others"]) == 1 and data["others"][0]["bot"] == "kgasu"
          and data["others"][0]["title"] == "КГАСУ"
          and data["others"][0]["users"] == 3
          and data["others"][0]["active_today"] == 1
          and data["others"][0]["last_seen"] == stamp(0, 11, 0)
          and data["others"][0]["ok"] is True
          and data["others"][0]["error"] == "", data["others"])
    check("своя база в others не попадает, даже если путь записан иначе",
          [item["bot"] for item in report.summary(
              SpelledStore(store, os.path.join(main_dir, ".", "bot.db")),
              main_dir)["others"]] == ["kgasu"],
          report.summary(SpelledStore(store, os.path.join(main_dir, ".", "bot.db")),
                         main_dir)["others"])
    check("база бота вне папки — в others видны обе соседние",
          [item["bot"] for item in report.summary(
              GhostStore(os.path.join(main_dir, "другой", "bot.db")), main_dir)["others"]]
          == ["unifirst", "kgasu"],
          report.summary(GhostStore(os.path.join(main_dir, "другой", "bot.db")),
                         main_dir)["others"])

    mirrored = report.summary(kgasu_store, main_dir)
    check("у второго бота в others — первая база",
          len(mirrored["others"]) == 1
          and mirrored["others"][0]["title"] == "Поволжский ГУФКСиТ"
          and mirrored["others"][0]["users"] == 5, mirrored["others"])

    empty = report.summary(Store(os.path.join(empty_dir, "empty.db")), empty_dir)
    check("пустая база: все числа нули, списки пусты",
          empty["total"] == 0 and empty["today"] == 0 and empty["week"] == 0
          and empty["new_today"] == 0 and empty["new_week"] == 0
          and empty["quiet"] == 0 and empty["admins"] == 0 and empty["messages"] == 0
          and empty["homework_group"] == 0 and empty["homework_personal"] == 0
          and empty["homework_open"] == 0, empty)
    check("пустая база: top_groups и others пусты",
          empty["top_groups"] == [] and empty["others"] == [], empty)

    ghost = report.summary(GhostStore(), None)
    check("сводка на «магазине» без таблицы users — нули без исключения",
          ghost["total"] == 0 and ghost["top_groups"] == [] and ghost["others"] == [], ghost)
    check("users_rows и count_users на битом магазине безопасны",
          report.users_rows(GhostStore()) == [] and report.count_users(GhostStore()) == 0)


# ------------------------------------------------------------------ 4. users_csv

def check_csv(store):
    print("\n4. users_csv: BOM, экранирование, колонки")
    rows = report.users_rows(store)
    text = report.users_csv(rows, bot_title="Поволжский ГУФКСиТ")
    check("выгрузка начинается с BOM", text.startswith("\ufeff"), text[:12])
    check("BOM попадает в байты как UTF-8 EF BB BF",
          text.encode("utf-8")[:3] == b"\xef\xbb\xbf")
    check("переводы строк — CRLF", "\r\n" in text and text.count("\r\n") == 6,
          text.count("\r\n"))

    parsed = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    header, body = parsed[0], parsed[1:]
    check("заголовок без колонки «бот»", header == report.CSV_HEADERS[1:], header)
    check("строк ровно столько же, сколько пользователей, плюс заголовок",
          len(parsed) == len(rows) + 1 == 6, len(parsed))
    check("колонок в каждой строке столько же, сколько в заголовке",
          all(len(line) == len(header) for line in body),
          [len(line) for line in body])

    line_102 = next((line for line in body if line[0] == "102"), [])
    check("имя с ';' и кавычками прошло круговой прогон без потерь",
          len(line_102) > 2 and line_102[1] == "Иван; \"Тест\"", line_102)
    check("кавычки внутри значения экранированы удвоением",
          '""Тест""' in text, text.split("\r\n")[1][:120])
    check("username без '@' и тихий режим словом",
          len(line_102) > 5 and line_102[2] == "ivan" and line_102[5] == "да", line_102)
    line_101 = next((line for line in body if line[0] == "101"), [])
    check("админ отмечен «да», обычный пользователь — «нет»",
          len(line_101) > 10 and line_101[10] == "да"
          and next(line for line in body if line[0] == "103")[10] == "нет", line_101)
    check("личные заметки попали в выгрузку",
          next(line for line in body if line[0] == "103")[9] == "1")
    check("пустое имя заменено на «без имени»",
          next(line for line in body if line[0] == "104")[1] == "без имени")
    check("сообщений ноль, когда колонки messages_count нет",
          all(line[8] == "0" for line in body), [line[8] for line in body])

    with_bot = report.users_csv(rows, bot_title="Поволжский ГУФКСиТ", include_bot=True)
    parsed_bot = list(csv.reader(io.StringIO(with_bot.lstrip("\ufeff")), delimiter=";"))
    check("include_bot добавляет первую колонку «бот»",
          parsed_bot[0] == report.CSV_HEADERS and parsed_bot[0][0] == "бот", parsed_bot[0])
    check("include_bot: название бота в каждой строке",
          all(line[0] == "Поволжский ГУФКСиТ" for line in parsed_bot[1:])
          and all(len(line) == 14 for line in parsed_bot), parsed_bot[1:2])
    check("у строки своё название бота важнее общего",
          "КГАСУ" in report.users_csv([dict(rows[0], bot_title="КГАСУ")],
                                      bot_title="Поволжский ГУФКСиТ", include_bot=True))
    empty_csv = list(csv.reader(io.StringIO(
        report.users_csv([]).lstrip("\ufeff")), delimiter=";"))
    check("пустой список — только заголовок", len(empty_csv) == 1, empty_csv)


# --------------------------------------------------- 5. подписи дат

def check_formats():
    print("\n5. format_last_seen и format_span")
    check("сегодняшний заход со временем",
          report.format_last_seen(stamp(0, 14, 3), now=NOW) == "сегодня 14:03",
          report.format_last_seen(stamp(0, 14, 3), now=NOW))
    check("вчерашний заход со временем",
          report.format_last_seen(stamp(1, 20, 11), now=NOW) == "вчера 20:11",
          report.format_last_seen(stamp(1, 20, 11), now=NOW))
    check("старая дата — ДД.ММ.ГГГГ",
          report.format_last_seen(stamp(10, 8, 0), now=NOW) == stamp(10, 8, 0)[8:10] + "."
          + stamp(10, 8, 0)[5:7] + "." + stamp(10, 8, 0)[:4],
          report.format_last_seen(stamp(10, 8, 0), now=NOW))
    check("дата без времени — просто «сегодня»",
          report.format_last_seen(day_text(0), now=NOW) == "сегодня",
          report.format_last_seen(day_text(0), now=NOW))
    check("пустая, None и битая дата дают прочерк",
          report.format_last_seen("", now=NOW) == "—"
          and report.format_last_seen(None, now=NOW) == "—"
          and report.format_last_seen("когда-то", now=NOW) == "—"
          and report.format_last_seen("2026-13-45 99:99:99", now=NOW) == "—")

    base = datetime(2026, 10, 7, 23, 15, 4)
    check("только что", report.format_span(base, now=base + timedelta(seconds=30)) == "только что")
    check("минуты", report.format_span(base, now=base + timedelta(minutes=12)) == "12 мин",
          report.format_span(base, now=base + timedelta(minutes=12)))
    check("часы", report.format_span(base, now=base + timedelta(hours=3)) == "3 ч",
          report.format_span(base, now=base + timedelta(hours=3)))
    check("дни", report.format_span(base, now=base + timedelta(days=2, hours=5)) == "2 дн",
          report.format_span(base, now=base + timedelta(days=2, hours=5)))
    check("ровно 60 секунд — уже минута",
          report.format_span(base, now=base + timedelta(seconds=60)) == "1 мин")
    check("время «из будущего» считается «только что»",
          report.format_span(base, now=base - timedelta(minutes=5)) == "только что")
    check("битая и пустая дата в format_span дают прочерк",
          report.format_span("", now=base) == "—"
          and report.format_span("не дата", now=base) == "—"
          and report.format_span(None, now=base) == "—")


# ------------------------------------------- 6. битые данные и чужая схема

def check_broken_inputs(plain_dir, messages_dir):
    print("\n6. Битые данные и чужая схема")
    plain_store = Store(os.path.join(plain_dir, "minimal.db"))
    plain_rows = report.users_rows(plain_store)
    check("база без messages_count: строки есть, сообщений ноль",
          len(plain_rows) == 2 and all(row["messages"] == 0 for row in plain_rows),
          plain_rows)
    plain_summary = report.summary(plain_store, plain_dir)
    check("база без messages_count: сводка не падает",
          plain_summary["total"] == 2 and plain_summary["messages"] == 0, plain_summary)
    check("NULL в имени и группе не ломают строку",
          row_of(plain_rows, 2).get("name") == "без имени"
          and row_of(plain_rows, 2).get("group") == ""
          and row_of(plain_rows, 2).get("is_admin") is False, row_of(plain_rows, 2))
    check("NULL в настройках считается выключенным — тихий режим",
          row_of(plain_rows, 2).get("quiet") is True, row_of(plain_rows, 2))
    check("битая дата «когда-то» не попала в счётчик за сегодня",
          plain_summary["today"] == 1, plain_summary)

    messages_store = Store(os.path.join(messages_dir, "messages.db"))
    messages_rows = report.users_rows(messages_store)
    check("колонка messages_count есть: значения читаются",
          sorted(row["messages"] for row in messages_rows) == [3, 7], messages_rows)
    messages_summary = report.summary(messages_store, messages_dir)
    check("сводка суммирует сообщения", messages_summary["messages"] == 10,
          messages_summary)
    check("поиск в базе с NULL-именем не падает",
          report.count_users(messages_store, "маша") == 1
          and report.count_users(messages_store, "неттакого") == 0)


# ---------------------------------------------------------------------- запуск

def main():
    workdir = make_workdir()
    print("Временная папка: %s" % workdir)
    main_dir = os.path.join(workdir, "main")
    broken_dir = os.path.join(workdir, "broken")
    plain_dir = os.path.join(workdir, "plain")
    messages_dir = os.path.join(workdir, "messages")
    empty_dir = os.path.join(workdir, "empty")
    for folder in (main_dir, broken_dir, plain_dir, messages_dir, empty_dir):
        os.makedirs(folder, exist_ok=True)

    try:
        store = seed_bot_db(os.path.join(main_dir, "bot.db"))
        kgasu_store = seed_kgasu_db(os.path.join(main_dir, "kgasu.db"))

        # Битый файл и валидная «чужая» база в одной папке.
        with open(os.path.join(broken_dir, "broken.db"), "wb") as handle:
            handle.write("это вовсе не база SQLite, а просто мусор".encode("utf-8") * 20)
        foreign = sqlite3.connect(os.path.join(broken_dir, "foreign.db"))
        try:
            foreign.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT)")
            foreign.execute("INSERT INTO notes(text) VALUES('чужая база')")
            foreign.commit()
        finally:
            foreign.close()

        make_minimal_db(os.path.join(plain_dir, "minimal.db"), with_messages=False)
        make_minimal_db(os.path.join(messages_dir, "messages.db"), with_messages=True)

        check_find_databases(main_dir, broken_dir, plain_dir)
        check_users_rows(store)
        check_summary(store, kgasu_store, main_dir, empty_dir)
        check_csv(store)
        check_formats()
        check_broken_inputs(plain_dir, messages_dir)
    finally:
        gc.collect()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 60)
    print("Проверок: %d, провалов: %d" % (len(CHECKS), len(FAILURES)))
    if FAILURES:
        for failure in FAILURES:
            print("  ❌ %s" % failure)
        print("ИТОГ: ЕСТЬ ОШИБКИ")
        return 1
    print("OK: %d проверок" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
