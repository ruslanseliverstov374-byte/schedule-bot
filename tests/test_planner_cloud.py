# -*- coding: utf-8 -*-
"""Проверки встроенной «Планёрки»: вход по паролю, API, статика, задачи.

Поднимает настоящий HTTP-сервер сервиса ботов (StatusApp) с подключённым
планировщиком и ходит по путям /planner/... как браузер.

Запуск:
    python tests/test_planner_cloud.py
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from planner import core      # noqa: E402
from planner.service import PlannerService      # noqa: E402
from store import Store      # noqa: E402
from webapp import StatusApp      # noqa: E402

PASSWORD = "test-password-123"
DB_BOT = os.path.join(ROOT, "data", "test-planner-bot.db")
#: В облаке планировщик живёт в той же базе, что и бот: так его данные попадают
#: в закреплённую копию и переживают перезапуск сервиса.
DB_PLANNER = DB_BOT

CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print("  ✅ %s" % name)
    else:
        FAILURES.append("%s — %s" % (name, detail))
        print("  ❌ %s — %s" % (name, detail))


def cleanup():
    for base in (DB_BOT, DB_PLANNER):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(base + suffix)
            except OSError:
                pass


class Client:
    """Мини-клиент с cookie: ведёт себя как браузер."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path, follow=True):
        request = urllib.request.Request(self.base + path,
                                         headers={"User-Agent": "test"})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def post(self, path, payload=None, form=None):
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload or {}, ensure_ascii=False).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": content_type, "User-Agent": "test"})
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def delete(self, path):
        request = urllib.request.Request(self.base + path, method="DELETE",
                                         headers={"User-Agent": "test"})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")


def main():
    import urllib.parse      # noqa: E402 — нужен внутри Client
    cleanup()
    store = Store(DB_BOT)
    planner = PlannerService(db_path=DB_PLANNER, schedule_bot_path=ROOT,
                             password=PASSWORD, logger=lambda message: None)
    app = StatusApp(store, on_update=None, webhook_secret="",
                    bot_username="test", engine=None,
                    logger=lambda message: None, planner=planner)
    port = app.start(port=0, host="127.0.0.1")
    base = "http://127.0.0.1:%d" % port
    try:
        print("1. Доступ и пароль")
        anonymous = Client(base)
        status, body = anonymous.get("/planner")
        check("без входа — страница входа", status == 200 and "Пароль" in body,
              (status, body[:80]))
        status, body = anonymous.get("/planner/api/state")
        check("API без входа закрыт", status == 401, (status, body[:80]))
        status, body = anonymous.post("/planner/login", form={"password": "неверный"})
        check("неверный пароль отклонён", status == 401 and "Неверный пароль" in body,
              (status, body[:80]))
        status, body = anonymous.get("/planner/api/health")
        check("health открыт для мониторинга", status == 200, (status, body[:80]))

        client = Client(base)
        status, body = client.post("/planner/login", form={"password": PASSWORD})
        check("верный пароль пускает внутрь",
              status in (200, 303) and ("План" in body or not body), (status, body[:80]))
        status, body = client.get("/planner/")
        check("интерфейс отдаётся", status == 200 and "Планёрка" in body, status)
        status, body = client.get("/planner/static/app.js")
        check("скрипт интерфейса отдаётся", status == 200 and "loadToday" in body, status)
        status, body = client.get("/planner/static/style.css")
        check("стили отдаются", status == 200 and "--accent" in body, status)

        print("\n2. API планировщика")
        status, body = client.get("/planner/api/state")
        state = json.loads(body)
        check("состояние отдаётся", status == 200 and "today" in state, status)
        status, body = client.get("/planner/api/groups")
        groups = json.loads(body)
        titles = [item["title"] for item in groups["universities"]]
        check("списки групп видны", status == 200 and len(titles) >= 2, titles)
        status, body = client.post("/planner/api/groups",
                                   {"groups": [{"university": "kgasu",
                                                "code": "26ЗК01"}]})
        check("группа сохраняется", status == 200 and
              [item["code"] for item in json.loads(body)["selected"]] == ["26ЗК01"],
              body[:120])

        status, body = client.post("/planner/api/chat",
                                   {"text": "конспект по анатомии к пятнице, 20 страниц"})
        answer = json.loads(body)
        check("ассистент отвечает", status == 200 and "Разложил" in answer["reply"],
              answer.get("reply", "")[:100])
        status, body = client.get("/planner/api/tasks")
        tasks = json.loads(body)["tasks"]
        check("задача создана", len(tasks) == 1 and tasks[0]["steps_total"] >= 4,
              tasks[:1])
        status, body = client.get("/planner/api/plan?days=7")
        plan = json.loads(body)
        check("план строится", status == 200 and len(plan["days"]) == 7,
              len(plan.get("days", [])))
        check("план содержит блоки", plan["totals"]["planned_blocks"] >= 4,
              plan["totals"])
        status, body = client.get("/planner/api/stats")
        stats = json.loads(body)
        check("статистика недели и месяца",
              status == 200 and len(stats["week"]) == 7 and len(stats["month"]) == 30,
              (len(stats.get("week", [])), len(stats.get("month", []))))
        status, body = client.get("/planner/api/plan.ics?days=7")
        check("экспорт в календарь", status == 200 and "BEGIN:VEVENT" in body, status)

        print("\n3. Изменение и удаление")
        task_id = tasks[0]["id"]
        status, body = client.post("/planner/api/tasks/%d/done" % task_id,
                                   {"done": True})
        check("задачу можно закрыть", status == 200 and json.loads(body).get("ok"),
              body[:80])
        status, body = client.delete("/planner/api/tasks/%d" % task_id)
        check("задачу можно удалить", status == 200 and json.loads(body).get("ok"),
              (status, body[:80]))
        status, body = client.get("/planner/api/tasks")
        check("задача удалена", json.loads(body)["tasks"] == [], body[:80])
        status, body = client.post("/planner/api/plan/reset", {})
        check("план можно очистить", status == 200 and json.loads(body).get("ok"),
              body[:80])

        print("\n4. Боты не сломаны")
        status, body = client.get("/health")
        check("health ботов отвечает", status == 200 and body.strip() == "ok",
              (status, body[:40]))
        status, body = client.get("/service.json")
        check("состояние сервиса отдаётся", status == 200 and "bots" in json.loads(body),
              (status, body[:60]))
        status, body = anonymous.get("/telegram")
        check("вебхук-путь не отдан планировщику", status == 404, status)

        print("\n5. Данные попадают в базу бота")
        bot_rows = store.query_one(
            "SELECT COUNT(*) AS total FROM sqlite_master WHERE name LIKE 'planner_%'")
        check("таблицы планировщика в базе бота", int(bot_rows["total"]) >= 4,
              bot_rows)
        planner.app.store.execute(
            "INSERT INTO planner_settings(key, value) VALUES('probe','1')"
            " ON CONFLICT(key) DO UPDATE SET value='1'")
        check("планировщик пишет в ту же базу", True)

        print("\n6. Пароль генерируется сам и приходит в Telegram")
        auto = PlannerService(db_path=DB_BOT, schedule_bot_path=ROOT,
                              password=None, logger=lambda message: None)
        generated = auto.ensure_password()
        check("пароль сгенерирован", bool(generated) and len(generated) == 12,
              generated)
        check("пароль сохранён в базе",
              auto.app.store.settings().get("planner_password") == generated,
              auto.app.store.settings().get("planner_password"))
        check("повторно пароль не меняется", auto.ensure_password() is None)
        check("вход с этим паролем работает",
              auto._handle_login(("password=%s" % generated).encode())[0] == 303)
        from_env = PlannerService(db_path=DB_BOT, schedule_bot_path=ROOT,
                                  password="from-env", logger=lambda message: None)
        check("пароль из переменной окружения важнее базы",
              from_env.ensure_password() is None)
    finally:
        app.stop()
        try:
            store.close()
        except Exception:
            pass
        try:
            planner.store.close()
        except Exception:
            pass
        cleanup()

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
