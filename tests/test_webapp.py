# -*- coding: utf-8 -*-
"""Проверка веб-слоя бота (webapp.py): health-check, страница состояния, вебхук.

Запуск:
    python tests/test_webapp.py

Всё офлайн: приложение поднимается на свободном порту 127.0.0.1, хранилище,
движок напоминаний и обработчик обновлений — поддельные. Тест не зависит от
текущей даты: все отметки времени заданы явно. Персональные данные в поддельном
хранилище специально «подложены» — их не должно быть на странице состояния.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# Консоль Windows по умолчанию cp1251 и не умеет ни русских галочек, ни эмодзи.
# Печатаем в UTF-8 (то же делает `chcp 65001` в check.bat), иначе тест падает
# на кодировке ещё до первой проверки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from webapp import StatusApp                                 # noqa: E402

SECRET = "test-secret-123"
BOT_USERNAME = "povolzh_sport_bot"
USER_ID = 987654321          # «персональные данные»: на страницу попасть не должны
USERNAME = "ivan_petrov"
GROUP_TITLE = "26281"

CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print("  ✅ %s" % name)
    else:
        print("  ❌ %s %s" % (name, ("— " + str(detail)) if detail else ""))
        FAILURES.append("%s %s" % (name, detail))


def wait_for(predicate, timeout=2.0):
    """Ждёт условия: рабочий поток разбирает очередь асинхронно (до 2 секунд)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


# ------------------------------------------------------------- подделки

class FakeStore:
    """Правдоподобное хранилище плюс ловушки с персональными данными."""

    GROUPS_UPDATED_AT = "2025-01-02 03:04:05"

    def __init__(self):
        self.calls = []

    def stats(self):
        self.calls.append("stats")
        return {
            "users": 128,
            "groups": 96,
            "groups_in_use": 17,
            "timetable_weeks": 34,
            "homework": 12,
            "changes": 5,
            # лишние ключи: невнимательная реализация напечатала бы их как есть
            "last_user_id": USER_ID,
            "last_username": USERNAME,
            "last_group": GROUP_TITLE,
        }

    def groups_count(self):
        return 96

    def groups_in_use(self):
        return ["26%03d" % number for number in range(17)]

    def groups_updated_at(self):
        return self.GROUPS_UPDATED_AT

    def get_meta(self, key, default=None):
        # в мете тоже лежит «личное»: страница обязана его не показывать
        return {"last_user_id": USER_ID, "bot_username": ""}.get(key, default)


class MinimalStore:
    """Хранилище без stats(): хватает методов, перечисленных в интерфейсе."""

    def groups_count(self):
        return 96

    def groups_in_use(self):
        return ["26%03d" % number for number in range(17)]

    def groups_updated_at(self):
        return "2025-01-02 03:04:05"

    def get_meta(self, key, default=None):
        return default


class BrokenStore:
    """Хранилище, которое всегда падает: страница — 500, health — всё равно ok."""

    def stats(self):
        raise RuntimeError("база занята")

    def groups_count(self):
        raise RuntimeError("база занята")

    def groups_in_use(self):
        raise RuntimeError("база занята")

    def groups_updated_at(self):
        raise RuntimeError("база занята")

    def get_meta(self, key, default=None):
        raise RuntimeError("база занята")


class FakeEngine:
    """Движок напоминаний: веб-слой читает только last_tick и last_error."""

    def __init__(self, last_error=None):
        self.last_tick = datetime(2025, 1, 2, 3, 0, 0)
        self.last_error = last_error


class Collector:
    """Поддельный on_update: складывает полученные update в список."""

    def __init__(self):
        self._items = []
        self._lock = threading.Lock()

    def __call__(self, update):
        with self._lock:
            self._items.append(update)

    def snapshot(self):
        with self._lock:
            return list(self._items)

    def update_ids(self):
        return [item.get("update_id") for item in self.snapshot()]


# ------------------------------------------------------------- HTTP-клиент

def call(port, method, path, body=None, headers=None):
    """Запрос на http://127.0.0.1:<port>. Возвращает (код, заголовки, текст)."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=data, method=method,
                                     headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read().decode("utf-8")


def json_headers(secret=SECRET):
    """Заголовки вебхука Telegram."""
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return headers


def post_update(port, update, secret=SECRET):
    """Отправляет update вебхуком и возвращает код ответа."""
    body = update if isinstance(update, str) else json.dumps(update, ensure_ascii=False)
    status, _, _ = call(port, "POST", "/telegram", body=body, headers=json_headers(secret))
    return status


def port_is_free(port):
    """Свободен ли порт: никто не слушает и порт можно занять заново."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(("127.0.0.1", port))
        return False                      # кто-то отвечает — порт всё ещё занят
    except OSError:
        pass
    finally:
        probe.close()
    binder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    binder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        binder.bind(("127.0.0.1", port))  # как это делает сам сервер
        return True
    except OSError:
        return False
    finally:
        binder.close()


def make_app(store, collector=None, engine=None, secret=SECRET):
    """Приложение с тихим логгером: вывод теста не должен засоряться."""
    return StatusApp(store, on_update=collector, webhook_secret=secret,
                     bot_username=BOT_USERNAME, engine=engine,
                     logger=lambda message: None)


# ------------------------------------------------------------------- проверки

def main():
    store = FakeStore()
    collector = Collector()
    app = make_app(store, collector, FakeEngine())
    apps = [app]
    try:
        port = app.start(port=0, host="127.0.0.1")

        print("\n1. Запуск и health-check")
        check("start(port=0) вернул свободный порт", isinstance(port, int) and port > 0, port)
        check("свойство port совпадает с портом из start()", app.port == port, app.port)
        check("running == True после запуска", app.running is True, app.running)

        status, headers, body = call(port, "GET", "/health")
        check("/health отвечает 200", status == 200, status)
        check("/health отдаёт ровно «ok»", body == "ok", repr(body))
        check("/health отдаёт text/plain",
              "text/plain" in (headers.get("Content-Type") or ""), headers.get("Content-Type"))

        print("\n2. Страница состояния")
        status, headers, page = call(port, "GET", "/")
        content_type = (headers.get("Content-Type") or "").lower()
        check("/ отвечает 200", status == 200, status)
        check("/ отдаёт HTML в UTF-8",
              content_type.startswith("text/html") and "utf-8" in content_type, content_type)
        check("страница на русском и со встроенным CSS",
              'lang="ru"' in page and "<style>" in page, page[:80])
        check("на странице есть имя бота", BOT_USERNAME in page)
        missing = [value for value in ("128", "96", "17", "34", "12", "5")
                   if 'class="value">%s</div>' % value not in page]
        check("на странице все числа хранилища", not missing, "нет: %s" % missing)
        check("показана дата обновления списка групп",
              "02.01.2025 03:04" in page, page[:200])
        check("показано время последнего тика движка напоминаний",
              "02.01.2025 03:00 UTC" in page, page[:200])
        check("показаны счётчики обновлений",
              all(label in page for label in ("Принято", "Обработано", "Ошибок")), "")

        print("\n3. Страница без персональных данных")
        check("нет id пользователя", str(USER_ID) not in page)
        check("нет имени пользователя и названия группы",
              USERNAME not in page and GROUP_TITLE not in page)

        print("\n4. Вебхук: проверка секрета")
        status = post_update(port, {"update_id": 1, "message": {"text": "/start"}}, secret=None)
        check("без заголовка секрета — 403", status == 403, status)
        # секрет — обычный HTTP-заголовок, поэтому только латиница
        status = post_update(port, {"update_id": 1, "message": {"text": "/start"}},
                             secret="wrong-secret-999")
        check("с неверным секретом — 403", status == 403, status)
        check("отклонённые обновления не дошли до обработчика",
              collector.snapshot() == [], collector.snapshot())

        print("\n5. Вебхук: приём и порядок обновлений")
        first = {"update_id": 100, "message": {"message_id": 1, "text": "/start"}}
        status = post_update(port, first)
        check("валидное обновление принято (200)", status == 200, status)
        check("обработчик получил ровно это обновление",
              wait_for(lambda: collector.snapshot() == [first]), collector.snapshot())

        second = {"update_id": 101, "callback_query": {"id": "cb-1", "data": "g:1"}}
        third = {"update_id": 102, "message": {"message_id": 2, "text": "/today"}}
        post_update(port, second)
        post_update(port, third)
        check("порядок обработки двух обновлений сохранён",
              wait_for(lambda: collector.update_ids() == [100, 101, 102]),
              collector.update_ids())

        without_id = {"message": {"message_id": 3, "text": "/help"}}
        status = post_update(port, without_id)
        check("обновление без update_id принимается без падения",
              status == 200 and wait_for(lambda: len(collector.snapshot()) == 4),
              (status, len(collector.snapshot())))

        print("\n6. Вебхук: ошибки в запросе")
        status, _, body = call(port, "POST", "/telegram", body="{это не json",
                               headers=json_headers())
        check("невалидный JSON — 400", status == 400, status)
        check("после ошибок приём продолжает работать",
              post_update(port, {"update_id": 103}) == 200)
        wait_for(lambda: len(collector.snapshot()) == 5)

        print("\n7. Счётчики stats()")
        counters = app.stats()
        check("принято ровно 5 обновлений", counters["updates_received"] == 5,
              counters["updates_received"])
        check("обработано ровно 5 обновлений", counters["updates_handled"] == 5,
              counters["updates_handled"])
        check("ошибки посчитаны (403 и 400)", counters["errors"] >= 3, counters["errors"])
        check("время запуска заполнено", bool(counters["started_at"]), counters["started_at"])

        status, _, page = call(port, "GET", "/")
        check("страница показывает счётчики обновлений",
              status == 200 and "<td>5</td>" in page, status)

        print("\n8. Маршруты: 404 и 405")
        # в пути допустима только латиница: кириллицу пришлось бы кодировать
        status, _, _ = call(port, "GET", "/no-such-page")
        check("неизвестный путь — 404", status == 404, status)
        status, headers, _ = call(port, "PUT", "/", body="x")
        check("PUT на страницу — 405", status == 405, status)
        check("в ответе 405 есть Allow",
              "GET" in (headers.get("Allow") or ""), headers.get("Allow"))
        status, _, _ = call(port, "DELETE", "/telegram")
        check("DELETE на вебхук — 405", status == 405, status)

        print("\n9. Сломанное хранилище")
        broken = make_app(BrokenStore(), engine=None)
        apps.append(broken)
        broken_port = broken.start(port=0, host="127.0.0.1")
        status, _, body = call(broken_port, "GET", "/")
        check("при падении хранилища страница отдаёт 500", status == 500, status)
        check("500 — короткий текст без внутренностей",
              len(body) < 120 and "Traceback" not in body and "база занята" not in body,
              repr(body))
        status, _, body = call(broken_port, "GET", "/health")
        check("health отвечает ok даже при сломанном хранилище",
              status == 200 and body == "ok", (status, body))
        check("сломанный сервер продолжает работать", broken.running is True)
        broken.stop()

        print("\n10. Хранилище без stats() и без движка")
        minimal = make_app(MinimalStore(), engine=None)
        apps.append(minimal)
        minimal_port = minimal.start(port=0, host="127.0.0.1")
        status, _, page = call(minimal_port, "GET", "/")
        check("страница собирается и без stats()", status == 200, status)
        check("числа взяты из groups_count()/groups_in_use()",
              'class="value">96</div>' in page and 'class="value">17</div>' in page,
              page[:200])
        check("без движка напоминаний страница честно сообщает об этом",
              "не подключён" in page)
        minimal.stop()

        print("\n11. Остановка сервера")
        app.stop()
        check("после stop() приложение не работает", app.running is False, app.running)
        check("stop() освобождает порт", port_is_free(port), port)
        alive = lambda: [item.name for item in threading.enumerate()
                         if item.name.startswith("webapp-")]
        check("потоки сервера завершены", wait_for(lambda: not alive()), alive())
        try:
            app.stop()
            repeated = True
        except Exception as error:
            repeated = "исключение: %s" % error
        check("повторный stop() не падает", repeated is True, repeated)

        again = make_app(FakeStore(), engine=None)
        apps.append(again)
        reused = again.start(port=port, host="127.0.0.1")
        check("освободившийся порт занимается заново", reused == port, reused)
        again.stop()

    finally:
        for instance in apps:
            try:
                instance.stop()
            except Exception:
                pass

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
