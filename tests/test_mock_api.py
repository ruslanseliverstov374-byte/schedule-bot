# -*- coding: utf-8 -*-
"""Самопроверка офлайн-мока API расписания (tests/mock_api.py).

Запуск из корня проекта::

    python tests/test_mock_api.py

Скрипт поднимает мок на 127.0.0.1, ходит в него обычным stdlib-клиентом
(urllib + http.client) и печатает итог ``OK: N проверок``. Код возврата 0 —
все проверки пройдены, 1 — есть ошибки. Наружу (в интернет) не ходит.

Файл специально оформлен как обычный скрипт: если запустить его через
``python -m unittest``, он дополнительно отдаст те же проверки в unittest.
"""

import json
import os
import sys
import threading
import traceback
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPConnection

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from mock_api import (  # noqa: E402  (путь к модулю правим выше)
    API_PREFIX,
    DEFAULT_API_KEY,
    FIXTURES_DIR,
    MockApi,
    load_default_groups,
    load_default_timetables,
)

FIXTURE_TIMETABLE = os.path.join(FIXTURES_DIR, "timetable_sample.json")
SAMPLE_TITLE, SAMPLE_YEAR, SAMPLE_WEEK = "26281", 2026, 41
EMPTY_GROUP, EMPTY_GROUP_ID = "22411", 1341
UNKNOWN_GROUP = "24999"
UNKNOWN_WEEK = 12


# ------------------------------------------------------------------- проверки

CASES = []


def check(title, condition, detail=""):
    """Зафиксировать результат одной проверки."""
    CASES.append((title, bool(condition), detail))
    return bool(condition)


class ApiError(Exception):
    """Ответ API со статусом, отличным от 200 (тело разобрано в JSON)."""

    def __init__(self, status, payload):
        super().__init__("HTTP %s: %s" % (status, payload))
        self.status = status
        self.payload = payload


def call(base, path, params=None, api_key=DEFAULT_API_KEY, timeout=10):
    """GET-запрос к моку: 200 -> тело, иначе исключение ApiError."""
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if api_key is not None:
        headers["api-key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    payload = json.loads(raw.decode("utf-8"))
    if status != 200:
        raise ApiError(status, payload)
    return payload


def load_fixture_timetable():
    """Фикстура расписания — чтобы сравнивать ответ мока с реальными данными."""
    with open(FIXTURE_TIMETABLE, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def fixture_lesson(days, day):
    """Первый пункт дня: (пара, предмет, преподаватель, аудитория)."""
    item = days[day]["items"][0]
    return (item["para"], item["title"],
            item["teachers"][0]["name"], item["auditories"][0]["name"])


# ------------------------------------------------------------------ сценарии

def run_checks():
    """Выполнить все проверки мока по порядку."""
    # --- 1. запуск сервера ------------------------------------------------
    api = MockApi()
    base = api.start()
    host_port = base.replace("http://", "").replace(API_PREFIX, "")
    host, port = host_port.rsplit(":", 1)
    check("сервер стартует на 127.0.0.1 и отдаёт свободный порт",
          host == "127.0.0.1" and port.isdigit() and int(port) > 0, base)
    check("сервер работает в фоновом потоке-демоне",
          api._thread is not None and api._thread.daemon and api._thread.is_alive())

    try:
        # --- 2. список групп ----------------------------------------------
        payload = call(base, "/groups", {"limit": 500})
        groups = payload.get("data") or []
        names = [item.get("name") for item in groups]
        check("GET /groups отдаёт 239 групп из фикстуры",
              payload.get("meta", {}).get("count") == 239 and len(groups) == 239,
              "count=%s" % payload.get("meta", {}).get("count"))
        check("записи групп содержат id, name, hasSubgroups, subgroups",
              all(set(item) == {"id", "name", "hasSubgroups", "subgroups"}
                  for item in groups))
        check("в выдаче есть коды вида 22411, 23101, 26281, 26281м",
              all(code in names for code in ("22411", "23101", "26281", "26281м")))

        # --- 3. фильтр по name --------------------------------------------
        filtered = call(base, "/groups", {"limit": 500, "name": "2628"})["data"]
        check("фильтр name=2628 находит ровно 3 группы",
              [item["name"] for item in filtered] == ["26281", "26281м", "26282"],
              str([item["name"] for item in filtered]))
        filter_meta = call(base, "/groups", {"limit": 500, "name": "2628"})["meta"]
        check("meta фильтра содержит limit, count и сам запрос name",
              filter_meta == {"limit": 500, "count": 3, "name": "2628"}, str(filter_meta))
        page = call(base, "/groups", {"limit": 3})
        check("limit урезает выдачу, pageInfo.endCursor и hasNext корректны",
              len(page["data"]) == 3 and page["pageInfo"]["endCursor"] == page["data"][-1]["id"]
              and page["pageInfo"]["hasNext"] is True, str(page["pageInfo"]))
        check("фильтр по несуществующему имени даёт пустой список",
              call(base, "/groups", {"limit": 500, "name": "нет-такой-группы"})["data"] == [])
        mixed = call(base, "/groups", {"limit": 500, "name": "М"})["data"]
        check("фильтр name не учитывает регистр",
              call(base, "/groups", {"limit": 500, "name": "м"})["data"] == mixed
              and len(mixed) == 41
              and all("м" in item["name"].lower() for item in mixed),
              "%d групп: %s" % (len(mixed), [item["name"] for item in mixed[:4]]))

        # --- 4. расписание группы -----------------------------------------
        payload = call(base, "/timetable",
                       {"title": SAMPLE_TITLE, "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        fixture = load_fixture_timetable()
        entry = (payload.get("data") or [{}])[0]
        days = entry.get("lessons") or {}
        check("GET /timetable возвращает meta недели (limit/count/week/year/date_from/date_to)",
              payload.get("meta") == {"limit": 10, "count": 1, "week": 41, "year": 2026,
                                      "date_from": "2026-10-05", "date_to": "2026-10-11"},
              str(payload.get("meta")))
        check("запись расписания повторяет структуру реального API",
              (entry.get("title"), entry.get("status")) == (SAMPLE_TITLE, True)
              and entry.get("group") == {"id": 1473, "name": SAMPLE_TITLE}
              and entry.get("course") == {"id": 7, "name": "1"}
              and entry.get("subgroup") is None and entry.get("facultet") is None,
              json.dumps({key: entry.get(key) for key in
                          ("title", "status", "group", "course", "subgroup", "facultet")},
                         ensure_ascii=False))
        check("расписание содержит 6 учебных дней недели 41",
              len(days) == 6 and list(days) == ["2026-10-05", "2026-10-06", "2026-10-07",
                                                "2026-10-08", "2026-10-09", "2026-10-10"],
              str(list(days)))
        check("ответ мока совпадает с фикстурой реального API",
              payload.get("data") == fixture.get("data"))
        check("пары содержат день, подпись, номер пары, время, тип, преподавателей и аудитории",
              fixture_lesson(days, "2026-10-05") == (2, "История России", "Фан-Юнг Г.Ю.", "E703")
              and fixture_lesson(days, "2026-10-06") == (1, "Педагогика школы",
                                                         "Замчевская Е.С.", "B202b"),
              "%s | %s" % (fixture_lesson(days, "2026-10-05"),
                           fixture_lesson(days, "2026-10-06")))
        check("подписи дней недели — как в реальном ответе",
              days["2026-10-05"]["label"] == "5 октября 2026, понедельник"
              and days["2026-10-06"]["label"] == "6 октября 2026, вторник")

        # --- 5. группа без занятий ----------------------------------------
        payload = call(base, "/timetable",
                       {"title": EMPTY_GROUP, "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        records = payload.get("data") or []
        check("группа без занятий отвечает записью с пустым lessons (как реальный API)",
              len(records) == 1 and records[0]["title"] == EMPTY_GROUP
              and records[0]["group"] == {"id": EMPTY_GROUP_ID, "name": EMPTY_GROUP}
              and records[0]["lessons"] == [],
              json.dumps(records[0].get("lessons") if records else None))

        # --- 6. группа и неделя, которых нет в моке ------------------------
        payload = call(base, "/timetable",
                       {"title": UNKNOWN_GROUP, "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        check("неизвестная группа отдаёт meta недели и пустой data",
              payload.get("data") == [] and payload["meta"]["count"] == 0
              and payload["meta"]["week"] == SAMPLE_WEEK
              and payload["meta"]["date_from"] == "2026-10-05",
              json.dumps(payload.get("meta"), ensure_ascii=False))
        payload = call(base, "/timetable",
                       {"title": SAMPLE_TITLE, "week": UNKNOWN_WEEK, "year": SAMPLE_YEAR})
        empty_records = payload.get("data") or []
        check("неизвестная неделя известной группы отвечает записью с пустым lessons",
              len(empty_records) == 1 and empty_records[0]["title"] == SAMPLE_TITLE
              and empty_records[0]["lessons"] == [] and payload["meta"]["count"] == 1
              and payload["meta"]["week"] == UNKNOWN_WEEK
              and payload["meta"]["date_from"] == "2026-03-16",
              json.dumps(payload.get("meta"), ensure_ascii=False))

        # --- 7. доступ по api-key -------------------------------------------
        try:
            call(base, "/groups", {"limit": 5}, api_key=None)
            no_key = None
        except ApiError as error:
            no_key = (error.status, error.payload)
        check("запрос без заголовка api-key получает 403 invalid api-key",
              no_key == (403, {"error": "invalid api-key"}), str(no_key))
        try:
            call(base, "/groups", {"limit": 5}, api_key="bad-key")
            bad_key = None
        except ApiError as error:
            bad_key = (error.status, error.payload)
        check("запрос с неверным ключом получает 403 invalid api-key",
              bad_key == (403, {"error": "invalid api-key"}), str(bad_key))

        # --- 8. set_timetable / set_groups / set_api_key --------------------
        custom_entry = {"id": 1, "title": "22412", "status": True,
                        "group": {"id": 1342, "name": "22412"},
                        "course": {"id": 3, "name": "2"}, "subgroup": None, "facultet": None,
                        "lessons": {"2026-10-05": {"label": "5 октября 2026, понедельник",
                                                   "items": []}}}
        api.set_timetable("22412", SAMPLE_YEAR, SAMPLE_WEEK, {"data": [custom_entry]})
        payload = call(base, "/timetable",
                       {"title": "22412", "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        check("set_timetable подменяет расписание группы",
              payload["data"] == [custom_entry] and payload["meta"]["count"] == 1)
        api.set_timetable("22413", SAMPLE_YEAR, SAMPLE_WEEK, [])
        payload = call(base, "/timetable",
                       {"title": "22413", "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        check("set_timetable(..., []) означает «занятий нет»",
              payload["data"] == [] and payload["meta"]["count"] == 0)
        api.set_groups([{"id": 1, "name": "90001", "hasSubgroups": False, "subgroups": []}])
        payload = call(base, "/groups", {"limit": 500})
        check("set_groups заменяет список групп и пересчитывает meta.count",
              [item["name"] for item in payload["data"]] == ["90001"]
              and payload["meta"]["count"] == 1, str(payload["meta"]))
        api.set_api_key("test-key-42")
        check("set_api_key меняет ожидаемый ключ",
              call(base, "/groups", {"limit": 5}, api_key="test-key-42")["meta"]["count"] == 1)
        try:
            call(base, "/groups", {"limit": 5})
            rejected = False
        except ApiError as error:
            rejected = error.status == 403
        check("после смены ключа прежний ключ отклоняется", rejected)
        api.set_api_key(None)
        check("set_api_key(None) возвращает реальный ключ по умолчанию",
              call(base, "/groups", {"limit": 5})["meta"]["count"] == 1)

        # --- 9. преподаватели ----------------------------------------------
        payload = call(base, "/teachers", {"limit": 20})
        data = payload.get("data") or []
        info = payload.get("pageInfo") or {}
        check("GET /teachers отдаёт limit, count, name и 20 записей",
              payload.get("meta") == {"limit": 20, "count": 20, "name": None},
              str(payload.get("meta")))
        check("pageInfo содержит startCursor, endCursor, hasNext, hasPrev",
              set(info) == {"startCursor", "endCursor", "hasNext", "hasPrev"}
              and info["startCursor"] is None and info["endCursor"] == data[-1]["id"]
              and info["hasNext"] is True and info["hasPrev"] is False, str(info))
        check("записи преподавателей — словари из id и name",
              all(set(item) == {"id", "name"} for item in data) and len(data) == 20)
        payload = call(base, "/teachers", {"limit": 20, "name": "Фан-Юнг"})
        check("фильтр /teachers?name=Фан-Юнг находит Фан-Юнг Г.Ю.",
              [item["name"] for item in payload["data"]] == ["Фан-Юнг Г.Ю."],
              str(payload["data"]))
        payload = call(base, "/teachers", {"limit": 20, "after": data[-1]["id"]})
        check("пагинация /teachers?after=<endCursor> двигает выдачу вперёд",
              payload["data"] and payload["data"][0]["id"] != data[0]["id"]
              and payload["pageInfo"]["hasPrev"] is True, str(payload["pageInfo"]))

        # --- 10. расписание преподавателя ------------------------------------
        payload = call(base, "/teachers/437/timetable",
                       {"year": SAMPLE_YEAR, "week": SAMPLE_WEEK})
        entry = (payload.get("data") or [{}])[0]
        lessons = entry.get("lessons") or {}
        check("GET /teachers/<id>/timetable повторяет структуру /timetable",
              payload.get("meta", {}).get("week") == SAMPLE_WEEK
              and entry.get("group") == {"id": 1473, "name": SAMPLE_TITLE}
              and all(set(day) == {"label", "items"} for day in lessons.values()),
              str(list(lessons)))
        check("расписание преподавателя оставляет только его пары",
              bool(lessons) and all(item["teachers"][0]["id"] == 437
                                    for day in lessons.values() for item in day["items"]),
              str(list(lessons)))

        # --- 11. неизвестный путь -------------------------------------------
        try:
            call(base, "/unknown", {"limit": 5})
            unknown = None
        except ApiError as error:
            unknown = (error.status, error.payload)
        check("неизвестный путь получает 404 not found",
              unknown == (404, {"error": "not found"}), str(unknown))
        try:
            call(base, "/", None)
            root = None
        except ApiError as error:
            root = (error.status, error.payload)
        check("корень мока тоже 404 not found", root == (404, {"error": "not found"}), str(root))

        # --- 12. журнал запросов --------------------------------------------
        api.set_api_key(DEFAULT_API_KEY)
        api.clear_requests()
        call(base, "/groups", {"limit": 500, "name": "23101"})
        call(base, "/timetable", {"title": SAMPLE_TITLE, "week": SAMPLE_WEEK, "year": SAMPLE_YEAR})
        try:
            call(base, "/unknown", None)
        except ApiError:
            pass
        try:
            call(base, "/groups", {"limit": 5}, api_key="bad-key")
        except ApiError:
            pass
        journal = api.requests
        check("журнал записывает все обращения по порядку",
              len(journal) == 4
              and [item["path"] for item in journal] == [API_PREFIX + "/groups",
                                                         API_PREFIX + "/timetable",
                                                         API_PREFIX + "/unknown",
                                                         API_PREFIX + "/groups"],
              str([item["path"] for item in journal]))
        check("запись журнала содержит path, params, api_key и status",
              all(set(item) == {"path", "params", "api_key", "status"} for item in journal))
        check("в журнале видны параметры, ключ и статус ответа",
              journal[0]["params"] == {"limit": "500", "name": "23101"}
              and journal[0]["api_key"] == DEFAULT_API_KEY and journal[0]["status"] == 200
              and journal[1]["params"] == {"title": SAMPLE_TITLE, "week": "41", "year": "2026"}
              and journal[3]["status"] == 403 and journal[3]["api_key"] == "bad-key",
              json.dumps(journal, ensure_ascii=False))
        check("журнал очищается методом clear_requests",
              (api.clear_requests(), api.requests == [])[1])

        # --- 13. поведение HTTP-соединения ----------------------------------
        connection = HTTPConnection(host, int(port), timeout=10)
        try:
            connection.request("HEAD", API_PREFIX + "/groups?limit=1",
                               headers={"api-key": DEFAULT_API_KEY})
            response = connection.getresponse()
            head_status, head_body = response.status, response.read()
            connection.request("GET", API_PREFIX + "/groups?limit=1",
                               headers={"api-key": DEFAULT_API_KEY})
            response = connection.getresponse()
            second_status = response.status
            response.read()
        finally:
            connection.close()
        check("HEAD и повторный GET по тому же соединению (HTTP/1.1) работают",
              head_status == 200 and head_body == b"" and second_status == 200,
              "HEAD=%s, GET=%s" % (head_status, second_status))
        check("Content-Type ответа — JSON в UTF-8",
              response.getheader("Content-Type") == "application/json; charset=utf-8",
              str(response.getheader("Content-Type")))

        # --- 14. независимость от data/ --------------------------------------
        groups_fixture = load_default_groups()
        timetables_fixture = load_default_timetables()
        check("данные по умолчанию берутся из tests/fixtures",
              len(groups_fixture) == 239
              and (SAMPLE_TITLE, SAMPLE_YEAR, SAMPLE_WEEK) in timetables_fixture,
              str(list(timetables_fixture)))
        check("set_timetable умеет задать расписание преподавателя",
              (lambda: (api.set_timetable(437, SAMPLE_YEAR, SAMPLE_WEEK, {"data": []}),
                        call(base, "/teachers/437/timetable",
                             {"year": SAMPLE_YEAR, "week": SAMPLE_WEEK})["data"] == [])[1])(),
              "ключ (437, 2026, 41)")
    finally:
        # --- 15. остановка сервера -------------------------------------------
        api.stop()
        check("stop() освобождает порт и поток",
              api.port is None and api.base is None
              and (api._thread is None or not api._thread.is_alive()))
        check("повторный stop() не падает", (api.stop() is None))
    return CASES


# --------------------------------------------------------------- unittest-обёртка

class MockApiTestCase(unittest.TestCase):
    """Те же проверки для запуска через ``python -m unittest tests/test_mock_api.py``."""

    def test_mock_api(self):
        cases = run_checks()
        failed = [item for item in cases if not item[1]]
        if failed:
            self.fail("Провалено проверок: %d\n%s" % (
                len(failed), "\n".join(" - %s: %s" % (t, d) for t, ok, d in failed)))


# ---------------------------------------------------------------------- запуск

def main():
    """Запустить проверки, напечатать отчёт, вернуть код возврата."""
    print("Проверка офлайн-мока API расписания (tests/mock_api.py)")
    print("-" * 72)
    try:
        cases = run_checks()
    except Exception:
        print("КРИТИЧНО: проверки прерваны исключением:\n")
        traceback.print_exc()
        return 1
    failed = 0
    for number, (title, ok, detail) in enumerate(cases, 1):
        print("%2d. [%s] %s" % (number, "OK" if ok else "FAIL", title))
        if not ok:
            failed += 1
            if detail:
                print("      получено: %s" % detail)
    print("-" * 72)
    if failed:
        print("ПРОВАЛ: %d из %d проверок не пройдено" % (failed, len(cases)))
        return 1
    print("OK: %d проверок" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
