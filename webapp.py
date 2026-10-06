# -*- coding: utf-8 -*-
"""Веб-слой бота: health-check для хостинга, страница состояния, вебхук Telegram.

Зачем это нужно, если бот умеет работать через long polling: бесплатные
хостинги (Render и подобные) держат сервис только пока тот слушает порт, а
Telegram умеет присылать обновления на публичный HTTPS-адрес. Модуль закрывает
обе задачи одним stdlib-сервером:

* ``GET  /health``   — health-check платформы: всегда 200 и ровно ``ok``,
  не обращается к базе и не падает даже при полностью сломанном хранилище;
* ``GET  /``         — публичная страница состояния: только агрегированные
  числа (пользователи, группы, кэш расписания, ДЗ, изменения), никаких имён,
  идентификаторов и текстов переписки;
* ``POST /telegram`` — приём обновлений Telegram через вебхук.

Обновления принимаются мгновенно: ответ 200 отдаётся сразу, а сам update
кладётся в очередь. Разбирает очередь один фоновый поток по одному, поэтому
порядок обновлений сохраняется.

Пример встраивания в bot.py::

    from webapp import StatusApp

    self.web = StatusApp(self.store, on_update=self.handle_update,
                         webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
                         bot_username=(self.me or {}).get("username", ""),
                         engine=self.engine, logger=self.log)
    port = self.web.start()          # слушает 0.0.0.0:$PORT (по умолчанию 8080)
    self.tg.set_webhook("https://<сервис>.onrender.com/telegram",
                        secret_token=os.environ.get("WEBHOOK_SECRET", ""))
    ...
    self.web.stop()                  # остановка при выходе

Зависимостей кроме стандартной библиотеки нет; код совместим с Python 3.14.
"""

import html
import json
import os
import queue
import string
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_PORT = 8080             # если ни аргумент, ни переменная PORT не заданы
DEFAULT_HOST = "0.0.0.0"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_BODY = 1024 * 1024          # столько Telegram не присылает: защита от мусора
BACKLOG_WARNING = 100           # с какого размера очереди предупреждать в лог
WORKER_TIMEOUT = 5.0            # сколько секунд ждём рабочий поток в stop()

STYLE = """
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 34px 16px 60px; background: #10131a; color: #e7eaf0;
           font: 15px/1.5 "Segoe UI", Roboto, Arial, sans-serif; }
    .wrap { max-width: 880px; margin: 0 auto; }
    header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 26px 0 10px; font-size: 16px; font-weight: 600; color: #aab4c8; }
    .badge { padding: 3px 10px; border-radius: 999px; font-size: 13px;
             background: #14361f; border: 1px solid #235c39; color: #7fe0a6; }
    .sub { margin: 8px 0 0; color: #93a0b6; font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: 12px; margin-top: 20px; }
    .card { background: #171c25; border: 1px solid #252c39; border-radius: 12px;
            padding: 14px 16px; }
    .label { color: #94a0b6; font-size: 13px; }
    .value { margin: 4px 0 2px; font-size: 26px; font-weight: 600; }
    .hint { color: #6d7a91; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; background: #171c25;
            border: 1px solid #252c39; border-radius: 12px; overflow: hidden; }
    th, td { padding: 9px 14px; border-bottom: 1px solid #232a36; font-size: 14px;
             text-align: left; }
    th { color: #94a0b6; font-weight: 500; }
    tr:last-child th, tr:last-child td { border-bottom: none; }
    .note { margin: 10px 0 0; color: #6d7a91; font-size: 12px; }
"""

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Состояние бота $username</title>
<style>
$style</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Состояние бота $username</h1>
    <span class="badge">работает</span>
  </header>
  <p class="sub">Время работы: $uptime. Страница собрана: $updated (UTC).</p>

  <div class="grid">
$cards
  </div>

  <h2>Служебные отметки</h2>
  <table>
    <tr><th>Последнее обновление списка групп</th><td>$groups_updated_at</td></tr>
    <tr><th>Последний тик движка напоминаний</th><td>$last_tick</td></tr>
    <tr><th>Состояние движка напоминаний</th><td>$engine_state</td></tr>
  </table>

  <h2>Обработка обновлений Telegram</h2>
  <table>
    <tr><th>Принято</th><th>Обработано</th><th>Ошибок</th><th>Запущен (UTC)</th></tr>
    <tr><td>$received</td><td>$handled</td><td>$errors</td><td>$started_at</td></tr>
  </table>

  <p class="note">Здесь только агрегированные числа: имена, идентификаторы
  пользователей и содержимое переписки на страницу не попадают.</p>
  <p class="note">Ошибки — сбои обработки и отклонённые вебхуки (неверный секрет,
  невалидный JSON). Если числа не растут, значит обновления до бота не доходят.</p>
</div>
</body>
</html>
"""

CARD = ('      <div class="card"><div class="label">%s</div>'
        '<div class="value">%s</div><div class="hint">%s</div></div>')


# --------------------------------------------------------------- форматирование

def format_duration(seconds):
    """Время работы коротко: «2 д 3 ч 4 мин», «1 ч 05 мин», «17 с»."""
    total = max(0, int(seconds or 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%d д %d ч %d мин" % (days, hours, minutes)
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    if minutes:
        return "%d мин %02d с" % (minutes, secs)
    return "%d с" % secs


def to_datetime(value):
    """Понимает datetime и строки вида «2025-01-02 03:04:05» (UTC хранилища)."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def format_stamp(value):
    """Отметка времени из базы в вид «02.01.2025 03:04»; иначе прочерк.

    Всё, что не разобралось как дата, на страницу не попадает: так на ней не
    может оказаться случайная строка из базы.
    """
    moment = to_datetime(value)
    return moment.strftime("%d.%m.%Y %H:%M") if moment else "—"


def format_moment(value):
    """Отметка времени движка напоминаний: то же плюс явное указание UTC."""
    moment = to_datetime(value)
    return moment.strftime("%d.%m.%Y %H:%M") + " UTC" if moment else "—"


def as_number(value):
    """Число для страницы: None и мусор превращаются в прочерк."""
    if value is None or value == "" or isinstance(value, bool):
        return "—"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "—"


def pick(stats, key, fallback=None):
    """Значение из stats(); если его нет — запасной вариант (вызывается лениво)."""
    value = (stats or {}).get(key)
    if value is not None:
        return value
    return fallback() if callable(fallback) else fallback


def resolve_port(port=None):
    """Порт: аргумент, иначе переменная окружения PORT, иначе 8080."""
    value = port
    if value is None:
        value = os.environ.get("PORT") or DEFAULT_PORT
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return number if number >= 0 else DEFAULT_PORT


# ------------------------------------------------------------------- приложение

class StatusApp:
    """HTTP-обёртка бота: health-check, страница состояния и вебхук Telegram.

    ``store``          — хранилище (store.Store): stats(), groups_count(),
                         groups_in_use(), groups_updated_at(), get_meta(key);
    ``on_update``      — вызывается с одним аргументом (dict update из Telegram)
                         в рабочем потоке; None — обновления только считаются;
    ``webhook_secret`` — секрет вебхука; пустая строка — заголовок не проверяется;
    ``bot_username``   — имя бота для страницы состояния (без «@»);
    ``engine``         — движок напоминаний (reminders.ReminderEngine) или None:
                         читаются только атрибуты last_tick и last_error;
    ``logger``         — функция логирования одной строки (print по умолчанию).

    Счётчики (метод ``stats()``), они же видны на странице:

    * ``updates_received`` — сколько обновлений принято в очередь;
    * ``updates_handled``  — сколько успешно обработано рабочим потоком;
    * ``errors``           — сбои обработки и отклонённые вебхуки (403/400/413);
    * ``started_at``       — время запуска «ГГГГ-ММ-ДД ЧЧ:ММ:СС» (UTC) или None.

    Секреты и токены в лог не попадают: пишутся только факты и коды.
    """

    def __init__(self, store, on_update=None, webhook_secret="", bot_username="",
                 engine=None, logger=print):
        self.store = store
        self.on_update = on_update
        self.webhook_secret = str(webhook_secret or "")
        self.bot_username = str(bot_username or "")
        self.engine = engine
        self.log = logger if callable(logger) else (lambda message: None)
        self._lock = threading.RLock()
        self._counters = {"updates_received": 0, "updates_handled": 0, "errors": 0}
        self._started_at = None
        self._server = None
        self._server_thread = None
        self._worker_thread = None
        self._queue = queue.Queue()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------- управление

    def start(self, port=None, host=DEFAULT_HOST):
        """Поднимает сервер в потоке-демоне. Возвращает фактический порт.

        ``port=0`` — взять любой свободный порт (используется в тестах).
        """
        with self._lock:
            if self._server is not None:
                return self.port
            number = resolve_port(port)
            server = _WebServer((host, number), _StatusHandler, app=self)
            work_queue = queue.Queue()
            stop_event = threading.Event()
            worker = threading.Thread(target=self._worker_loop,
                                      args=(work_queue, stop_event),
                                      name="webapp-worker", daemon=True)
            thread = threading.Thread(target=server.serve_forever,
                                      name="webapp-http", daemon=True)
            self._server = server
            self._server_thread = thread
            self._worker_thread = worker
            self._queue = work_queue
            self._stop_event = stop_event
            self._started_at = datetime.now(timezone.utc)
            worker.start()
            thread.start()
            self.log("Веб-сервер запущен: http://%s:%d (страница /, health /health)"
                     % (host, self.port))
            return self.port

    def stop(self):
        """Останавливает сервер и рабочий поток. Повторный вызов безопасен.

        Очередь успевает опустеть, но ожидание ограничено WORKER_TIMEOUT:
        зависший обработчик не мешает процессу завершиться (поток — демон).
        """
        with self._lock:
            server, self._server = self._server, None
            thread, self._server_thread = self._server_thread, None
            worker, self._worker_thread = self._worker_thread, None
            stop_event, self._stop_event = self._stop_event, threading.Event()
            self._queue = queue.Queue()
        if server is None and worker is None:
            return
        if stop_event is not None:
            stop_event.set()
        if server is not None:
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                try:
                    server.shutdown()
                except Exception as error:
                    self.log("Остановка веб-сервера: %s" % error)
            try:
                server.server_close()
            except Exception as error:
                self.log("Закрытие сокета веб-сервера: %s" % error)
        if worker is not None and worker.is_alive():
            worker.join(WORKER_TIMEOUT)
        self.log("Веб-сервер остановлен")

    # --------------------------------------------------------------- свойства

    @property
    def port(self):
        """Фактический порт сервера (0, если сервер не запущен)."""
        server = self._server
        if server is None:
            return 0
        try:
            return int(server.server_address[1])
        except (AttributeError, IndexError, TypeError):
            return 0

    @property
    def running(self):
        """Работает ли сервер прямо сейчас."""
        thread = self._server_thread
        return bool(self._server is not None and thread is not None and thread.is_alive())

    def stats(self):
        """Счётчики для страницы: updates_received, updates_handled, errors, started_at."""
        with self._lock:
            counters = dict(self._counters)
            started = self._started_at
        counters["started_at"] = started.strftime("%Y-%m-%d %H:%M:%S") if started else None
        return counters

    # ---------------------------------------------------------- очередь updates

    def _accept(self, update):
        """Кладёт update в очередь: ответ Telegram уходит, не дожидаясь обработки."""
        self._bump("updates_received")
        self._queue.put(update)
        size = self._queue.qsize()
        if size >= BACKLOG_WARNING and size % BACKLOG_WARNING == 0:
            self.log("Очередь обновлений выросла: %d (обработчик не успевает)" % size)

    def _worker_loop(self, work_queue, stop_event):
        """Разбирает очередь по одному обновлению: порядок сохраняется."""
        while True:
            if stop_event.is_set() and work_queue.empty():
                return           # останавливаемся, но сначала дочищаем очередь
            try:
                update = work_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if update is None:
                return
            self._handle_update(update)

    def _handle_update(self, update):
        """Вызывает on_update и ведёт счётчики. Исключения наружу не выходят."""
        try:
            if self.on_update is not None:
                self.on_update(update)
        except Exception as error:
            self._bump("errors")
            self.log("Ошибка обработки обновления: %s" % error)
            return
        self._bump("updates_handled")

    def _bump(self, name, amount=1):
        """Увеличивает счётчик (вызывается из потоков сервера и рабочего)."""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    # ------------------------------------------------------------- страница

    def render_page(self):
        """HTML страницы состояния. Исключения не глушим — их ловит обработчик."""
        return _render(self._collect())

    def _collect(self):
        """Собирает данные страницы: только числа и даты, без персональных данных."""
        store = self.store
        stats = dict(self._call(store, "stats") or {})
        engine = self.engine
        counters = self.stats()
        username = self.bot_username or (self._call(store, "get_meta", "bot_username") or "")
        last_error = getattr(engine, "last_error", None)
        if engine is None:
            engine_state = "не подключён"
        elif last_error:
            engine_state = "подключён, была ошибка"
        else:
            engine_state = "подключён, ошибок нет"

        def in_use_count():
            titles = self._call(store, "groups_in_use")
            return None if titles is None else len(titles)

        cards = [
            ("Пользователей", as_number(pick(stats, "users",
                                             lambda: self._call(store, "count_users"))),
             "всего в базе"),
            ("Групп в кэше", as_number(pick(stats, "groups",
                                            lambda: self._call(store, "groups_count"))),
             "список с сайта расписания"),
            ("Групп с подписчиками", as_number(pick(stats, "groups_in_use", in_use_count)),
             "у этих групп есть студенты"),
            ("Недель расписания", as_number(stats.get("timetable_weeks")), "лежат в кэше"),
            ("Открытых ДЗ", as_number(stats.get("homework")), "ждут сдачи"),
            ("Изменений расписания", as_number(stats.get("changes")), "нашёл движок"),
        ]
        return {
            "username": username or "бот расписания",
            "uptime": format_duration(self._uptime()),
            "updated": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M"),
            "cards": cards,
            "groups_updated_at": format_stamp(self._call(store, "groups_updated_at")),
            "last_tick": format_moment(getattr(engine, "last_tick", None)),
            "engine_state": engine_state,
            "received": as_number(counters["updates_received"]),
            "handled": as_number(counters["updates_handled"]),
            "errors": as_number(counters["errors"]),
            "started_at": format_stamp(counters["started_at"]),
        }

    def _uptime(self):
        """Секунды с момента запуска сервера."""
        started = self._started_at
        if started is None:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())

    @staticmethod
    def _call(obj, name, *args):
        """Метод хранилища, если он есть: отсутствие метода — не ошибка."""
        method = getattr(obj, name, None) if obj is not None else None
        if method is None:
            return None
        return method(*args)


def _render(data):
    """Подставляет данные в шаблон. Всё, что пришло извне, экранируется."""
    cards = "\n".join(CARD % (html.escape(str(title)), html.escape(str(value)),
                              html.escape(str(hint)))
                      for title, value, hint in data["cards"])
    return string.Template(PAGE).substitute(
        style=STYLE,
        username=html.escape(str(data["username"])),
        uptime=html.escape(str(data["uptime"])),
        updated=html.escape(str(data["updated"])),
        cards=cards,
        groups_updated_at=html.escape(str(data["groups_updated_at"])),
        last_tick=html.escape(str(data["last_tick"])),
        engine_state=html.escape(str(data["engine_state"])),
        received=html.escape(str(data["received"])),
        handled=html.escape(str(data["handled"])),
        errors=html.escape(str(data["errors"])),
        started_at=html.escape(str(data["started_at"])),
    )


# ------------------------------------------------------------------ HTTP-слой

class _WebServer(ThreadingHTTPServer):
    """HTTP-сервер с потоками-демонами: процесс не держится за соединения."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app=None):
        self.app = app
        super().__init__(address, handler)


class _StatusHandler(BaseHTTPRequestHandler):
    """Обработчик запросов: health-check, страница состояния, вебхук Telegram."""

    server_version = "ScheduleBotWeb/1.0"
    protocol_version = "HTTP/1.1"   # соединение держим, поэтому всегда есть Content-Length

    # -- журналирование ----------------------------------------------------

    def log_message(self, format, *args):
        """Тишина в stderr: о важном сообщает logger приложения (одна строка)."""
        return

    # -- ответы ------------------------------------------------------------

    def _send(self, status, body, content_type, close=False, head_only=False, extra=None):
        """Единая точка отправки: всегда Content-Length, при ошибке — закрытие."""
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if head_only:
            return
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _plain(self, status, text, close=False, head_only=False, extra=None):
        self._send(status, text, "text/plain; charset=utf-8",
                   close=close, head_only=head_only, extra=extra)

    def _guard(self, action):
        """Неожиданная ошибка не должна уронить сервер или оставить запрос без ответа."""
        try:
            action()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            app = self._app()
            if app is not None:
                app._bump("errors")
                app.log("Ошибка обработки запроса %s: %s" % (urlsplit(self.path).path, error))
            try:
                self._plain(500, "внутренняя ошибка", close=True)
            except Exception:
                self.close_connection = True

    def _app(self):
        return getattr(self.server, "app", None)

    # -- маршруты ----------------------------------------------------------

    def do_GET(self):
        self._guard(lambda: self._route(head_only=False))

    def do_HEAD(self):
        self._guard(lambda: self._route(head_only=True))

    def do_POST(self):
        self._guard(self._post)

    def _route(self, head_only):
        """GET/HEAD: health-check и страница состояния, остальное — 404."""
        path = urlsplit(self.path).path
        if path == "/health":
            # Обязан ответить всегда: к базе не обращаемся вовсе.
            self._plain(200, "ok", head_only=head_only)
            return
        if path in ("/service.json", "/status.json"):
            # Состояние всех ботов этого сервиса (пишет multi.py) — нужно, чтобы
            # видеть снаружи, поднялся ли второй бот, не открывая панель Render.
            self._service_json(head_only)
            return
        if path == "/":
            self._page(head_only)
            return
        self._not_found(head_only)

    def _service_json(self, head_only):
        # Файл состояния пишет multi.py рядом с базой; путь берём от самого
        # webapp.py, чтобы не тянуть сюда bot.py (иначе круговой импорт).
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "data", "service-status.json")
        payload = {"bots": [], "note": "состояние пишет multi.py"}
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
        except Exception as error:
            payload = {"bots": [], "error": str(error)}
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        self._send(200, body, "application/json; charset=utf-8", head_only=head_only)

    def _page(self, head_only):
        """Страница состояния: любая ошибка хранилища — 500 коротким текстом."""
        app = self._app()
        if app is None:
            self._plain(500, "приложение не подключено", close=True, head_only=head_only)
            return
        try:
            page = app.render_page()
        except Exception as error:
            app._bump("errors")
            app.log("Страница состояния недоступна: %s" % error)
            self._plain(500, "ошибка: не удалось собрать статистику",
                        close=True, head_only=head_only)
            return
        self._send(200, page, "text/html; charset=utf-8", head_only=head_only)

    def _not_found(self, head_only=False):
        self._plain(404, "не найдено", close=True, head_only=head_only)

    def _method_not_allowed(self):
        self._plain(405, "метод не поддерживается", close=True,
                    extra={"Allow": "GET, HEAD, POST"})

    def _post(self):
        """POST: единственный маршрут — приём вебхука /telegram."""
        if urlsplit(self.path).path != "/telegram":
            self._not_found()
            return
        app = self._app()
        if app is None:
            self._plain(503, "приложение не подключено", close=True)
            return
        if app.webhook_secret:
            provided = self.headers.get(SECRET_HEADER) or ""
            if provided != app.webhook_secret:
                app._bump("errors")
                app.log("Вебхук отклонён: неверный секрет")
                self._plain(403, "forbidden", close=True)
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length < 0 or length > MAX_BODY:
            app._bump("errors")
            app.log("Вебхук отклонён: некорректная длина тела")
            self._plain(413, "слишком большое тело запроса", close=True)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            update = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            app._bump("errors")
            app.log("Вебхук отклонён: невалидный JSON")
            self._plain(400, "невалидный JSON", close=True)
            return
        if not isinstance(update, dict):
            app._bump("errors")
            app.log("Вебхук отклонён: ожидался объект update")
            self._plain(400, "ожидался объект update", close=True)
            return
        # update_id может отсутствовать — это не ошибка: просто считаем и кладём.
        app._accept(update)
        self._plain(200, "ok")


def _unsupported_method(handler):
    """PUT/DELETE и прочие методы, которых у нас нет, отвечают 405."""
    handler._method_not_allowed()


for _method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT"):
    setattr(_StatusHandler, "do_" + _method, _unsupported_method)
