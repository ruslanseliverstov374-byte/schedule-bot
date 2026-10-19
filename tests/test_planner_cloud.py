# -*- coding: utf-8 -*-
"""Проверки встроенной «Планёрки»: аккаунты, вход, API, изоляция задач.

Поднимает настоящий HTTP-сервер сервиса ботов (StatusApp) с подключённым
планировщиком и ходит по путям /planner/... как браузер.

Запуск:
    python tests/test_planner_cloud.py
"""

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from planner import core      # noqa: E402
from planner.service import PlannerService      # noqa: E402
from store import Store      # noqa: E402
from webapp import StatusApp      # noqa: E402

OWNER_PASSWORD = "owner-password-123"
DB_BOT = os.path.join(ROOT, "data", "test-planner-bot.db")
DB_AUTO = os.path.join(ROOT, "data", "test-planner-auto.db")

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
    for base in (DB_BOT, DB_AUTO):
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

    def get(self, path):
        request = urllib.request.Request(self.base + path,
                                         headers={"User-Agent": "test"})
        try:
            with self.opener.open(request, timeout=40) as response:
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
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def delete(self, path):
        request = urllib.request.Request(self.base + path, method="DELETE",
                                         headers={"User-Agent": "test"})
        try:
            with self.opener.open(request, timeout=40) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")


def main():
    cleanup()
    store = Store(DB_BOT)
    planner = PlannerService(db_path=DB_BOT, schedule_bot_path=ROOT,
                             password=OWNER_PASSWORD, owner_name="Руслан",
                             default_groups="unifirst:26282",
                             logger=lambda message: None)
    first = planner.ensure_accounts()
    app = StatusApp(store, on_update=None, webhook_secret="",
                    bot_username="test", engine=None,
                    logger=lambda message: None, planner=planner)
    port = app.start(port=0, host="127.0.0.1")
    base = "http://127.0.0.1:%d" % port
    try:
        print("1. Аккаунт владельца")
        check("аккаунт владельца создан", bool(first), first)
        check("пароль владельца из настроек сервиса",
              (first or {}).get("password") == OWNER_PASSWORD, first)
        check("повторно аккаунты не пересоздаются", planner.ensure_accounts() is None)
        owner_account = planner.store.accounts()[0]
        check("у аккаунта есть имя", owner_account["name"] == "Руслан",
              owner_account)
        check("аккаунт помечен владельцем", bool(owner_account["is_owner"]),
              owner_account)

        print("\n2. Доступ и пароль")
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
        status, body = client.post("/planner/login", form={"password": OWNER_PASSWORD})
        check("верный пароль пускает внутрь", status in (200, 303), (status, body[:80]))
        status, body = client.get("/planner/")
        check("интерфейс отдаётся", status == 200 and "Планёрка" in body, status)
        status, body = client.get("/planner/static/app.js")
        check("скрипт интерфейса отдаётся", status == 200 and "loadToday" in body, status)
        status, body = client.get("/planner/static/style.css")
        check("стили отдаются", status == 200 and "--accent" in body, status)

        print("\n3. API планировщика")
        status, body = client.get("/planner/api/state")
        state = json.loads(body)
        check("состояние отдаётся", status == 200 and "today" in state, status)
        check("в состоянии есть аккаунт",
              (state.get("account") or {}).get("name") == "Руслан", state.get("account"))
        check("группа по умолчанию подставлена",
              [item["code"] for item in state.get("groups") or []] == ["26282"],
              state.get("groups"))
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

        print("\n4. Изменение и удаление")
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

        print("\n5. Второй аккаунт (София)")
        status, body = client.post("/planner/api/accounts",
                                   {"name": "Рахимова София"})
        created = json.loads(body).get("account") or {}
        check("аккаунт создан", status == 200 and created.get("name") ==
              "Рахимова София", body[:120])
        sophia_password = created.get("password") or ""
        check("пароль сгенерирован", len(sophia_password) == 12, sophia_password)
        account_id = created.get("id")

        status, body = client.get("/planner/api/accounts")
        accounts = json.loads(body).get("accounts") or []
        names = [item["name"] for item in accounts]
        check("владелец видит оба аккаунта", len(accounts) == 2 and
              "Рахимова София" in names, names)

        sophia = Client(base)
        status, body = sophia.post("/planner/login", form={"password": sophia_password})
        check("София входит со своим паролем", status in (200, 303), status)
        status, body = sophia.get("/planner/api/state")
        sophia_state = json.loads(body)
        check("у Софии своё имя",
              (sophia_state.get("account") or {}).get("name") == "Рахимова София",
              sophia_state.get("account"))
        check("София не владелец",
              not (sophia_state.get("account") or {}).get("is_owner"))
        status, body = sophia.get("/planner/api/tasks")
        check("у Софии свои (пустые) задачи", json.loads(body)["tasks"] == [],
              body[:100])
        status, body = sophia.get("/planner/api/accounts")
        check("София не видит список аккаунтов", status == 403, status)

        status, body = sophia.post("/planner/api/chat",
                                   {"text": "реферат по истории к пятнице"})
        check("София создаёт свою задачу", status == 200 and len(
            json.loads(body).get("task") or {}) > 0, body[:100])
        status, body = sophia.get("/planner/api/tasks")
        check("у Софии появилась её задача", len(json.loads(body)["tasks"]) == 1,
              body[:100])
        status, body = client.get("/planner/api/tasks")
        check("задачи Софии не видны владельцу", json.loads(body)["tasks"] == [],
              body[:100])

        status, body = sophia.post("/planner/api/groups",
                                   {"groups": [{"university": "unifirst",
                                                "code": "26281"}]})
        check("София выбирает свою группу", status == 200 and
              [item["code"] for item in json.loads(body)["selected"]] == ["26281"],
              body[:120])
        status, body = client.get("/planner/api/groups")
        owner_groups = [item["code"] for item in json.loads(body)["selected"]]
        check("группа Софии не влияет на владельца", owner_groups == ["26ЗК01"],
              owner_groups)

        print("\n6. Смена и удаление аккаунта")
        status, body = client.post("/planner/api/accounts/%d/password" % account_id, {})
        new_password = json.loads(body).get("password") or ""
        check("владелец меняет пароль Софии", status == 200 and len(new_password) == 12,
              body[:100])
        status, body = Client(base).post("/planner/login",
                                         form={"password": sophia_password})
        check("старый пароль больше не работает", status == 401, status)
        status, body = Client(base).post("/planner/login",
                                        form={"password": new_password})
        check("новый пароль работает", status in (200, 303), status)

        status, body = client.delete("/planner/api/accounts/%d" % account_id)
        check("аккаунт удаляется", status == 200 and json.loads(body).get("ok"),
              body[:100])
        status, body = client.get("/planner/api/accounts")
        check("остался один аккаунт", len(json.loads(body)["accounts"]) == 1,
              body[:100])
        status, body = Client(base).post("/planner/login",
                                         form={"password": new_password})
        check("удалённый аккаунт не входит", status == 401, status)

        print("\n7. Автоматический пароль при первом запуске")
        auto = PlannerService(db_path=DB_AUTO, schedule_bot_path=ROOT,
                              password=None, owner_name="Владелец",
                              logger=lambda message: None)
        auto_result = auto.ensure_accounts()
        auto_password = (auto_result or {}).get("password") or ""
        check("пароль сгенерирован", len(auto_password) == 12, auto_password)
        check("аккаунт создан", len(auto.store.accounts()) == 1,
              auto.store.accounts())
        check("повторно пароль не меняется", auto.ensure_accounts() is None)
        check("вход с этим паролем работает",
              auto.store.find_account_by_password(auto_password) is not None)
        auto.store.close()

        print("\n8. Боты не сломаны")
        status, body = client.get("/health")
        check("health ботов отвечает", status == 200 and body.strip() == "ok",
              (status, body[:40]))
        status, body = client.get("/service.json")
        check("состояние сервиса отдаётся", status == 200 and "bots" in json.loads(body),
              (status, body[:60]))
        status, body = anonymous.get("/telegram")
        check("вебхук-путь не отдан планировщику", status == 404, status)

        print("\n9. Данные попадают в базу бота")
        bot_rows = store.query_one(
            "SELECT COUNT(*) AS total FROM sqlite_master WHERE name LIKE 'planner_%'")
        check("таблицы планировщика в базе бота", int(bot_rows["total"]) >= 4,
              bot_rows)
        check("аккаунты лежат в общей базе",
              len(planner.store.accounts()) == 1,
              planner.store.accounts())
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
