# -*- coding: utf-8 -*-
"""Офлайн-мок HTTP API расписания api.unifirst.ru/api/v1 — только стандартная библиотека.

Мок нужен тестам бота и парсера: он поднимает локальный HTTP-сервер на
127.0.0.1 и отвечает теми же JSON-структурами, что и реальный API
(структуры сняты с живого API и лежат в tests/fixtures).

Эндпоинты:

    GET /api/v1/groups?limit=500[&name=<подстрока>][&after=<id>]
    GET /api/v1/timetable?title=<группа>[ <подгруппа>]&week=<ISO-неделя>&year=<год>
    GET /api/v1/teachers?limit=20[&name=...][&after=<id>|&before=<id>]
    GET /api/v1/teachers/<id>/timetable?year=<год>&week=<неделя>

Заголовок ``api-key`` обязателен: без него или с неверным ключом мок отдаёт
403 ``{"error": "invalid api-key"}`` (реальный сервер отвечает HTML-страницей
404 Drupal). Неизвестный путь — 404 ``{"error": "not found"}``.

Пример:

    from tests.mock_api import MockApi          # или sys.path.insert(0, "tests")

    api = MockApi()
    base = api.start()                          # http://127.0.0.1:PORT/api/v1
    try:
        client = Unifirst(base=base)            # клиент бота смотрит на мок
        groups = client.groups()
        assert api.requests[0]["path"] == "/api/v1/groups"
    finally:
        api.stop()

Никаких внешних зависимостей и никаких обращений в сеть, кроме 127.0.0.1.
"""

import contextlib
import hashlib
import json
import os
import threading
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ константы

#: Ключ доступа реального API — используется моком по умолчанию.
DEFAULT_API_KEY = "777e5031c257a3d5ce7aa54aa56015cb"

#: Префикс пути, который имитирует мок (реальная база — https://api.unifirst.ru/api/v1).
API_PREFIX = "/api/v1"

#: Каталог с фикстурами: tests/fixtures.
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

#: Каталог с реальными ответами API (используется как запасной источник).
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

#: Фикстуры: список групп, расписание группы 26281 (неделя 41 2026 года), преподаватели.
GROUPS_FIXTURE = "groups.json"
TIMETABLE_FIXTURE = "timetable_sample.json"
TEACHERS_FIXTURE = "teachers.json"

#: Значения по умолчанию, как у реального API.
DEFAULT_GROUPS_LIMIT = 500
DEFAULT_TEACHERS_LIMIT = 20
DEFAULT_TIMETABLE_LIMIT = 10
MAX_LIMIT = 1000

#: Группа из фикстуры расписания и её неделя.
SAMPLE_TITLE = "26281"
SAMPLE_YEAR = 2026
SAMPLE_WEEK = 41


class MockApiError(Exception):
    """Ошибка самого мока (например, сервер не запущен)."""


# -------------------------------------------------------------- чтение данных

def _first_existing(*paths):
    """Первый существующий путь из перечисленных."""
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def _read_json(path):
    """Прочитать JSON-файл, записанный в UTF-8 (в том числе с BOM)."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _entries(payload):
    """Достать список записей из ответа API или из «голого» списка."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        data = payload.get("data")
        return list(data) if isinstance(data, list) else []
    if isinstance(payload, list):
        return list(payload)
    return []


def load_default_groups():
    """Список групп по умолчанию: tests/fixtures/groups.json, иначе data/groups.json.

    Фикстура — реальный ответ API (239 групп). Если файлов нет, возвращается
    небольшой встроенный набор, чтобы мок оставался работоспособным.
    """
    path = _first_existing(os.path.join(FIXTURES_DIR, GROUPS_FIXTURE),
                           os.path.join(DATA_DIR, GROUPS_FIXTURE))
    if path:
        return _entries(_read_json(path))
    return [
        {"id": 1341, "name": "22411", "hasSubgroups": False, "subgroups": []},
        {"id": 1342, "name": "22412", "hasSubgroups": False, "subgroups": []},
        {"id": 1473, "name": SAMPLE_TITLE, "hasSubgroups": False, "subgroups": []},
    ]


def load_default_timetables():
    """Расписания по умолчанию: {('26281', 2026, 41): полный ответ API}."""
    path = _first_existing(os.path.join(FIXTURES_DIR, TIMETABLE_FIXTURE),
                           os.path.join(DATA_DIR, TIMETABLE_FIXTURE))
    if not path:
        return {}
    payload = _read_json(path)
    entries = _entries(payload)
    if not entries:
        return {}
    title = entries[0].get("title") or SAMPLE_TITLE
    meta = payload.get("meta") if isinstance(payload, dict) else None
    year = int((meta or {}).get("year") or SAMPLE_YEAR)
    week = int((meta or {}).get("week") or SAMPLE_WEEK)
    return {(str(title), year, week): entries}


def load_default_teachers():
    """Преподаватели по умолчанию: tests/fixtures/teachers.json, иначе из data/."""
    path = _first_existing(os.path.join(FIXTURES_DIR, TEACHERS_FIXTURE),
                           os.path.join(DATA_DIR, TEACHERS_FIXTURE))
    if path:
        return _entries(_read_json(path))
    return [{"id": 437, "name": "Фан-Юнг Г.Ю."}]


def _now_year_week():
    """Текущие ISO-год и неделя — нужны только для правдоподобной заглушки."""
    today = date.today().isocalendar()
    return int(today[0]), int(today[1])


# ------------------------------------------------------------ вспомогательное

def _wrap(entries, meta=None):
    """Ответ вида реального API: {'meta': {...}, 'data': [...]}.

    Порядок ключей меты — как у живого API: limit, count, дальше остальные.
    """
    rows = list(entries or [])
    source = dict(meta or {})
    payload = {"meta": {"limit": source.pop("limit", len(rows)),
                        "count": source.pop("count", len(rows))}}
    payload["meta"].update(source)
    payload["data"] = rows
    return payload


def empty_record(group):
    """Запись группы без занятий: реальный API отдаёт одну запись с ``lessons: []``.

    Проверено на живом API: ``GET /timetable?title=22411&week=41&year=2026`` вернул
    ``{"meta": {...}, "data": [{"id": 128729, "title": "22411", ...,
    "course": {"id": 792, "name": "5"}, "lessons": []}]}``.
    """
    name = str((group or {}).get("name") or "")
    return {
        "id": _stable_id("timetable:%s" % name, 100000, 199999),
        "title": name,
        "status": True,
        "group": {"id": (group or {}).get("id"), "name": name},
        "course": {"id": 792, "name": "5"},
        "subgroup": None,
        "facultet": None,
        "lessons": [],
    }


def _safe_int(value, default=None):
    """Целое из строки запроса; None, если значение не число."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _limit_of(raw, fallback):
    """Ограничение выдачи: неположительное и нечисловое приводим к значению по умолчанию."""
    value = _safe_int(raw, None)
    if value is None or value <= 0:
        return fallback
    return min(value, MAX_LIMIT)


def _week_dates(year, week):
    """Понедельник и воскресенье ISO-недели (без падения на неверных значениях)."""
    try:
        monday = date.fromisocalendar(int(year), int(week), 1)
    except (ValueError, TypeError):
        monday = date.fromisocalendar(*_now_year_week(), 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _stable_id(text, low=1000, high=9999):
    """Устойчивый «id» для придуманной записи — одинаковый между запусками."""
    digest = hashlib.sha1(str(text).encode("utf-8")).hexdigest()
    return low + int(digest[:8], 16) % (high - low + 1)


# -------------------------------------------------------------------- сервер

class _ApiError(Exception):
    """Внутренняя ошибка обработчика: статус + тело ответа."""

    def __init__(self, status, payload):
        super().__init__("HTTP %s" % status)
        self.status = status
        self.payload = payload


class MockApi:
    """Локальный HTTP-сервер, имитирующий https://api.unifirst.ru/api/v1.

    Параметры конструктора:
        groups: список групп (записи ``{"id", "name", "hasSubgroups", "subgroups"}``)
            для ``/groups``; None — взять фикстуру tests/fixtures/groups.json.
        timetables: расписания для ``/timetable``. Ключ — ``(название, год, неделя)``
            или ``(id преподавателя, год, неделя)``, значение — полный ответ API либо
            просто список записей.
        api_key: ожидаемое значение заголовка ``api-key``.

    Журнал запросов — ``self.requests``: список словарей
    ``{"path", "params", "api_key", "status"}`` в порядке поступления.
    """

    def __init__(self, groups=None, timetables=None, api_key=DEFAULT_API_KEY, teachers=None):
        self.api_key = DEFAULT_API_KEY if api_key is None else str(api_key)
        self._groups = self._group_records(groups) if groups is not None else load_default_groups()
        self._teachers = (self._teacher_records(teachers) if teachers is not None
                          else load_default_teachers())
        self._timetables = {}
        #: ключи, для которых расписание задано явно (в том числе пустое).
        self._explicit = set()
        source = load_default_timetables() if timetables is None else timetables
        for key, payload in dict(source or {}).items():
            # Ключ может быть коротким — (название, год, неделя) — как в фикстуре.
            if isinstance(key, (tuple, list)) and len(key) == 3:
                target, year, week = key
                key = self._timetable_key(target, year, week)
            elif not isinstance(key, tuple) or len(key) != 4:
                raise MockApiError("Ключ расписания должен быть (название|id, год, неделя): %r" % (key,))
            explicit, entries = self._store_timetable(key, payload)
            self._timetables[key] = entries
            if explicit:
                self._explicit.add(key)
        self.requests = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._base = None

    # -- нормализация входных данных --------------------------------------

    @staticmethod
    def _group_records(groups):
        """Привести данные групп к списку записей (принимаем и ответ API целиком)."""
        if isinstance(groups, dict) and not groups.get("data"):
            groups = [dict(record, name=name) for name, record in groups.items()]
        return [dict(record) for record in _entries(groups)]

    @staticmethod
    def _teacher_records(teachers):
        """Привести данные преподавателей к списку записей."""
        records = []
        for record in _entries(teachers):
            if isinstance(record, dict):
                records.append({"id": record.get("id"), "name": record.get("name")})
            else:
                name = str(record)
                records.append({"id": _stable_id(name), "name": name})
        return records

    @staticmethod
    def _store_timetable(key, payload):
        """Разобрать расписание: ``(задан_явно, [записи])``.

        Принимаем полный ответ API ``{"meta", "data"}``, готовый список записей
        или одну запись. Пустой список — это тоже явно заданное расписание
        («группа есть, занятий нет»), поэтому его нельзя терять.
        """
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        elif isinstance(payload, dict):
            payload = [payload]  # одна запись расписания
        return True, _entries(payload)

    def _weeks(self, key):
        """Записи расписания по ключу; ``None`` — для этого ключа ничего не задано."""
        if key in self._explicit:
            return self._timetables.get(key) or []
        return self._timetables.get(key)

    @staticmethod
    def _timetable_key(target, year, week):
        """Ключ расписания: строка — название группы, целое — id преподавателя."""
        kind = "teacher" if isinstance(target, int) and not isinstance(target, bool) else "group"
        return (kind, str(target).strip(), int(year), int(week))

    # -- управление сервером ----------------------------------------------

    def start(self):
        """Поднять сервер на свободном порту 127.0.0.1 и вернуть базовый URL.

        Сервер обслуживается потоком-демоном, поэтому тест не «зависнет»,
        даже если забыть вызвать :meth:`stop`.
        """
        if self._server is not None:
            raise MockApiError("Сервер уже запущен: %s" % self._base)
        self.requests = []
        # Обработчик берёт данные из класса, поэтому каждый запуск «привязывает»
        # к нему свой экземпляр мока.
        _Handler.api = self
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05},
                                  name="mock-api", daemon=True)
        thread.start()
        self._server, self._thread = server, thread
        self._base = "http://%s:%d%s" % (host, port, API_PREFIX)
        return self._base

    def stop(self):
        """Остановить сервер (безопасно вызывать повторно и без start())."""
        server, thread = self._server, self._thread
        self._server, self._thread, self._base = None, None, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    @property
    def base(self):
        """Базовый URL работающего сервера (None, если он не запущен)."""
        return self._base

    @property
    def port(self):
        """Порт работающего сервера (None, если он не запущен)."""
        if self._server is None:
            return None
        return self._server.server_address[1]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False

    # -- данные ------------------------------------------------------------

    def set_groups(self, groups):
        """Заменить список групп (можно передать ответ API целиком)."""
        self._groups = self._group_records(groups)

    def set_teachers(self, teachers):
        """Заменить список преподавателей для ``/teachers``."""
        self._teachers = self._teacher_records(teachers)

    def set_timetable(self, title, year, week, payload):
        """Задать расписание для группы (строка) или преподавателя (целое).

        ``payload`` — полный ответ API ``{"meta", "data"}``, список записей или
        одна запись. Пустой список означает «занятий нет»: запрос вернёт
        ``{"meta": {...}, "data": []}``.
        """
        key = self._timetable_key(title, year, week)
        explicit, entries = self._store_timetable(key, payload)
        self._timetables[key] = entries
        if explicit:
            self._explicit.add(key)

    def set_api_key(self, key):
        """Сменить ожидаемый ключ доступа (None — вернуть реальный ключ по умолчанию)."""
        self.api_key = DEFAULT_API_KEY if key is None else str(key)

    def group_names(self):
        """Названия известных моку групп — удобно для проверок в тестах."""
        return [str(item.get("name")) for item in self._groups]

    def clear_requests(self):
        """Очистить журнал запросов."""
        self.requests = []

    def record(self, path, params, api_key, status):
        """Добавить запись в журнал запросов (вызывает обработчик)."""
        entry = {"path": path, "params": params, "api_key": api_key, "status": status}
        with self._lock:
            self.requests.append(entry)
        return entry

    # -- поиск данных ------------------------------------------------------

    def _find_group(self, name):
        """Запись группы по имени без учёта регистра."""
        wanted = str(name or "").strip().lower()
        for record in self._groups:
            if str(record.get("name", "")).strip().lower() == wanted:
                return record
        return None

    # -- обработчики эндпоинтов -------------------------------------------

    def _handle_groups(self, params):
        """GET /groups — список групп с фильтром по подстроке имени."""
        limit = _limit_of(params.get("limit"), DEFAULT_GROUPS_LIMIT)
        name = params.get("name")
        records = list(self._groups)
        if name not in (None, ""):
            needle = str(name).strip().lower()
            records = [item for item in records
                       if needle in str(item.get("name", "")).lower()]
        cursor = _safe_int(params.get("after"), None)
        offset = 0
        if cursor is not None:
            for index, item in enumerate(records):
                if item.get("id") == cursor:
                    offset = index + 1
                    break
        page = records[offset:offset + limit]
        meta = {"limit": limit, "count": len(records), "name": None if name in (None, "") else name}
        payload = _wrap(page, meta)
        if page:
            has_next = offset + len(page) < len(records)
            # Реальный API отдаёт endCursor = null, когда выдача закончилась
            # (проверено: /groups?limit=500&name=2628 -> {"endCursor": ..., "hasNext": false}).
            payload["pageInfo"] = {"endCursor": page[-1].get("id") if has_next else None,
                                   "hasNext": has_next}
        return 200, payload

    def _handle_timetable(self, params):
        """GET /timetable — расписание группы на неделю."""
        title = params.get("title")
        if title in (None, ""):
            return 400, {"error": "title is required"}
        year = _safe_int(params.get("year"), None)
        if year is None:
            return 400, {"error": "year is required"}
        week = _safe_int(params.get("week"), None)
        if week is None:
            return 400, {"error": "week is required"}
        limit = _limit_of(params.get("limit"), DEFAULT_TIMETABLE_LIMIT)
        date_from, date_to = _week_dates(year, week)
        meta = {"limit": limit, "week": week, "year": year,
                "date_from": date_from, "date_to": date_to}
        key = ("group", str(title).strip(), year, week)
        records = self._weeks(key)
        if records:
            return 200, _wrap(records, dict(meta, count=len(records)))
        if key in self._explicit:
            # Расписание задано явно пустым — отдаём пустую выдачу без заглушки.
            return 200, _wrap([], dict(meta, count=0))
        # Группа известна, но на эту неделю занятий нет: реальный API отдаёт
        # одну запись с пустым lessons (проверено на живом API).
        group = self._find_group(title)
        if group is not None:
            return 200, _wrap([empty_record(group)], dict(meta, count=1))
        # Совсем неизвестная группа — пустая выдача.
        return 200, _wrap([], dict(meta, count=0))

    def _handle_teachers(self, params):
        """GET /teachers — преподаватели с курсорной пагинацией."""
        limit = _limit_of(params.get("limit"), DEFAULT_TEACHERS_LIMIT)
        name = params.get("name")
        records = list(self._teachers)
        if name not in (None, ""):
            needle = str(name).strip().lower()
            records = [item for item in records
                       if needle in str(item.get("name", "")).lower()]
        total = len(records)
        start = 0
        after = _safe_int(params.get("after"), None)
        if after is not None:
            for index, item in enumerate(records):
                if item.get("id") == after:
                    start = index + 1
                    break
        before = _safe_int(params.get("before"), None)
        if before is not None:
            for index, item in enumerate(records):
                if item.get("id") == before:
                    start = max(0, index - limit)
                    break
        page = records[start:start + limit]
        meta = {"limit": limit, "count": len(page),
                "name": None if name in (None, "") else name}
        payload = _wrap(page, meta)
        # Реальный API: когда выдача закончилась, курсоры равны null
        # (проверено: /teachers?limit=20 без продолжения -> startCursor/endCursor = null).
        has_next = start + len(page) < total
        has_prev = start > 0
        payload["pageInfo"] = {
            "startCursor": page[0]["id"] if has_prev and page else None,
            "endCursor": page[-1]["id"] if has_next and page else None,
            "hasNext": has_next,
            "hasPrev": has_prev,
        }
        return 200, payload

    def _handle_teacher_timetable(self, teacher_id, params):
        """GET /teachers/<id>/timetable — расписание преподавателя (структура как /timetable)."""
        if not str(teacher_id).isdigit():
            return 404, {"error": "not found"}
        number = int(teacher_id)
        year = _safe_int(params.get("year"), None)
        if year is None:
            return 400, {"error": "year is required"}
        week = _safe_int(params.get("week"), None)
        if week is None:
            return 400, {"error": "week is required"}
        limit = _limit_of(params.get("limit"), DEFAULT_TIMETABLE_LIMIT)
        date_from, date_to = _week_dates(year, week)
        meta = {"limit": limit, "week": week, "year": year,
                "date_from": date_from, "date_to": date_to}
        key = ("teacher", str(number), year, week)
        records = self._weeks(key)
        if records or key in self._explicit:
            return 200, _wrap(records, dict(meta, count=len(records)))
        # Отдельной записи нет — собираем расписание из пар этого преподавателя.
        filtered = []
        for record in self._weeks(("group", SAMPLE_TITLE, year, week)) or []:
            lessons = {}
            for day, value in (record.get("lessons") or {}).items():
                items = [item for item in (value.get("items") or [])
                         if _teacher_in(item, number)]
                if items:
                    lessons[day] = {"label": value.get("label"), "items": items}
            if lessons:
                filtered.append(dict(record, lessons=lessons))
        return 200, _wrap(filtered, dict(meta, count=len(filtered)))

    # -- точки входа обработчика ------------------------------------------

    def handle(self, path, params, api_key):
        """Обработать запрос: вернуть ``(статус, тело-словарь)``.

        Вызывается HTTP-обработчиком; в журнал запись попадает до ответа,
        поэтому в ``self.requests`` всегда виден и статус, и параметры.
        """
        try:
            if api_key != self.api_key:
                return 403, {"error": "invalid api-key"}
            return self._route(path, params)
        except _ApiError as error:
            return error.status, error.payload

    def _route(self, path, params):
        """Разбор пути после префикса ``/api/v1``."""
        parts = [part for part in str(path).split("/") if part]
        if parts and parts[0] == "api" and len(parts) > 1 and parts[1] == "v1":
            parts = parts[2:]
        if parts == ["groups"]:
            return self._handle_groups(params)
        if parts == ["timetable"]:
            return self._handle_timetable(params)
        if parts == ["teachers"]:
            return self._handle_teachers(params)
        if len(parts) == 3 and parts[0] == "teachers" and parts[2] == "timetable":
            return self._handle_teacher_timetable(parts[1], params)
        raise _ApiError(404, {"error": "not found"})


def _teacher_in(item, teacher_id):
    """Есть ли преподаватель с таким id среди преподавателей пары."""
    for teacher in (item or {}).get("teachers") or []:
        if isinstance(teacher, dict) and teacher.get("id") == teacher_id:
            return True
    return False


# ------------------------------------------------------------------ HTTP-слой

class _Handler(BaseHTTPRequestHandler):
    """Обработчик запросов: журналирование, проверка api-key, JSON-ответы."""

    server_version = "MockUnifirstApi/1.0"
    protocol_version = "HTTP/1.1"  # держим соединение, всегда отдавая Content-Length
    api = None  # ссылка на MockApi (проставляется при старте сервера)

    # -- журналирование ---------------------------------------------------

    def log_message(self, format, *args):
        """Молчание: мок не должен засорять вывод тестов."""
        return

    # -- разбор запроса ---------------------------------------------------

    def _request_params(self):
        """Параметры строки запроса: первый ключ побеждает, значения — строки."""
        query = urllib.parse.urlsplit(self.path).query
        params = {}
        for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            params.setdefault(key, value)
        return params

    def _handle(self, with_body=True):
        """Общая логика для GET/HEAD: разобрать, обработать, записать в журнал."""
        api = self.api
        path = urllib.parse.urlsplit(self.path).path
        params = self._request_params()
        api_key = self.headers.get("api-key")
        status, payload = api.handle(path, params, api_key)
        api.record(path, params, api_key, status)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    # -- методы HTTP ------------------------------------------------------

    def do_GET(self):
        self._handle(with_body=True)

    def do_HEAD(self):
        self._handle(with_body=False)

    def do_OPTIONS(self):
        """Разрешающий ответ CORS — на случай проверок из браузера/JS."""
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "api-key, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._method_not_allowed()

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        """Мок только читает: на изменяющие методы отвечаем 404 как реальный API."""
        path = urllib.parse.urlsplit(self.path).path
        params = self._request_params()
        api_key = self.headers.get("api-key")
        payload = {"error": "not found"}
        status = 404
        self.api.record(path, params, api_key, status)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def running_mock(**kwargs):
    """Контекстный менеджер: ``with running_mock() as api: ...`` — сам запустит и остановит."""
    api = MockApi(**kwargs)
    api.start()
    try:
        yield api
    finally:
        api.stop()


__all__ = [
    "API_PREFIX", "DEFAULT_API_KEY", "MockApi", "MockApiError", "empty_record",
    "load_default_groups", "load_default_teachers", "load_default_timetables",
    "running_mock",
]
