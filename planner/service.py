# -*- coding: utf-8 -*-
"""HTTP-слой «Планёрки» внутри сервиса ботов: вход по паролю, API и статика.

Сервис ботов уже держит один HTTP-сервер на порту Render. Планировщик
подключается к нему по путям `/planner/...`, поэтому отдельный хостинг не нужен:
сайт живёт на том же адресе, что и боты, и просыпается вместе с ними.

Аккаунтов может быть несколько: у каждого свой пароль, свои задачи и настройки.
При входе по паролю определяется аккаунт, дальше все запросы идут в его рамках.
Владелец (is_owner) видит дополнительный раздел с управлением аккаунтами.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

from . import core

COOKIE_NAME = "planner_session"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
#: Защита от подбора пароля: столько неудачных попыток с одного адреса за окно.
LOGIN_FAILURES_LIMIT = 10
LOGIN_FAILURES_WINDOW = 300

SETUP_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Планёрка — настройка</title>
<style>body{font:16px/1.6 "Segoe UI",Arial,sans-serif;max-width:640px;margin:80px auto;
padding:0 20px;color:#1c2333}code{background:#eef2ff;padding:2px 6px;border-radius:6px}</style>
</head><body><h1>📚 Планёрка</h1>
<p>Приложение установлено, но аккаунты ещё не созданы.</p>
<p>Добавьте в Render → Environment переменную <code>PLANNER_PASSWORD</code>
с паролем владельца, и сервис перезапустится сам. После этого откройте
<code>/planner</code>.</p>
</body></html>"""

LOGIN_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Планёрка — вход</title>
<style>
body{font:16px/1.6 "Segoe UI",Arial,sans-serif;background:#f4f6fb;color:#1c2333;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#fff;border-radius:16px;padding:32px 36px;box-shadow:0 12px 40px rgba(20,30,60,.12);
width:min(380px,90vw)}
h1{margin:0 0 6px;font-size:24px}
p{color:#6b7488;margin:0 0 18px;font-size:14px}
input{width:100%;padding:11px 14px;border:1px solid #e4e8f0;border-radius:10px;font:inherit;
box-sizing:border-box;margin-bottom:12px}
input:focus{outline:none;border-color:#3f6ef2;box-shadow:0 0 0 3px #e9efff}
button{width:100%;padding:11px;border:0;border-radius:10px;background:#3f6ef2;color:#fff;
font:inherit;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.05)}
.error{background:#ffe9e9;color:#cf3f3f;border-radius:8px;padding:8px 12px;margin-bottom:12px;
font-size:14px}
</style></head><body><div class="card">
<h1>📚 Планёрка</h1><p>Личный планировщик учёбы. Введите свой пароль.</p>
%s
<form method="post" action="/planner/login">
<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">Войти</button>
</form></div></body></html>"""


class PlannerService:
    """Планировщик с аккаунтами, подключённый к общему HTTP-серверу сервиса."""

    def __init__(self, db_path=None, schedule_bot_path=None, groups=None,
                 password=None, logger=print, owner_name="Владелец",
                 default_groups=""):
        self.app = core.App(db_path=db_path or core.DB_PATH,
                            schedule_bot_path=schedule_bot_path or core.ROOT,
                            groups=groups, default_groups=default_groups)
        self.env_password = (password if password is not None
                             else os.environ.get("PLANNER_PASSWORD", "")).strip()
        self.owner_name = (owner_name or "Владелец").strip()
        self.logger = logger if callable(logger) else (lambda message: None)
        self._stop = threading.Event()
        self._thread = None
        self._login_failures = {}

    @property
    def store(self):
        return self.app.store

    # ------------------------------------------------------------- аккаунты

    def ensure_accounts(self):
        """Создаёт аккаунт владельца при первом запуске.

        Возвращает {password, name}, если владельческому аккаунту только что
        задали пароль (его нужно отправить владельцу в Telegram), иначе None.
        """
        accounts = self.store.accounts()
        if not accounts:
            password = (self.env_password
                        or self.app.legacy_owner_password()
                        or core.generate_password())
            self.app.create_account(self.owner_name, password, is_owner=True)
            self.store.migrate_legacy_settings()
            self.logger("Планёрка: создан аккаунт владельца")
            return {"password": password, "name": self.owner_name}
        owner = next((item for item in accounts if item.get("is_owner")), None)
        if owner is None:
            self.store.execute("UPDATE planner_accounts SET is_owner=1 WHERE id=?",
                               (accounts[0]["id"],))
            owner = accounts[0]
        if self.env_password:
            known = self.store.find_account_by_password(self.env_password)
            if not known:
                # PLANNER_PASSWORD задаёт пароль владельца (переопределяет базу).
                self.store.set_account_password(owner["id"], self.env_password)
                return {"password": self.env_password, "name": owner["name"]}
        return None

    def _owner_id(self):
        return self.store.owner_account_id()

    def is_owner(self, account):
        return bool(account and account.get("is_owner"))

    # --------------------------------------------------------------- сессии

    @staticmethod
    def _session_token(password_hash, account_id):
        secret = hashlib.sha256(("planner:" + str(password_hash)).encode()).hexdigest()
        return hmac.new(secret.encode(), ("session:%d" % int(account_id)).encode(),
                        hashlib.sha256).hexdigest()

    def _cookies(self, headers):
        raw = ""
        for key, value in (headers or {}).items():
            if key.lower() == "cookie":
                raw = value
                break
        result = {}
        for part in raw.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                result[name.strip()] = value.strip()
        return result

    def current_account(self, headers):
        """Аккаунт по cookie; None, если сессии нет или она устарела."""
        cookie = self._cookies(headers).get(COOKIE_NAME, "")
        if ":" not in cookie:
            return None
        raw_id, token = cookie.split(":", 1)
        if not raw_id.isdigit():
            return None
        account = self.store.account_auth(int(raw_id))
        if not account:
            return None
        expected = self._session_token(account["password_hash"], account["id"])
        if not hmac.compare_digest(token, expected):
            return None
        return {"id": int(account["id"]), "name": account["name"],
                "is_owner": int(account["is_owner"] or 0)}

    def _cookie_header(self, account_id, password_hash, max_age=60 * 60 * 24 * 30):
        token = self._session_token(password_hash, account_id)
        return "%s=%d:%s; Path=/planner; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            COOKIE_NAME, int(account_id), token, max_age)

    def _login_response(self, status=200, error=""):
        block = '<div class="error">%s</div>' % error if error else ""
        body = LOGIN_PAGE.replace("%s", block, 1)
        return (status, "text/html; charset=utf-8", body.encode("utf-8"),
                {"Cache-Control": "no-store"})

    @staticmethod
    def _redirect(location, headers=None):
        extra = {"Location": location, "Cache-Control": "no-store"}
        if headers:
            extra.update(headers)
        return (303, "text/html; charset=utf-8", b"", extra)

    def _client_address(self, headers):
        for name, value in (headers or {}).items():
            if name.lower() in ("x-forwarded-for", "x-real-ip"):
                return str(value).split(",")[0].strip()
        return "unknown"

    def _too_many_failures(self, address):
        now = time.time()
        attempts = [moment for moment in self._login_failures.get(address, [])
                    if now - moment < LOGIN_FAILURES_WINDOW]
        self._login_failures[address] = attempts
        return len(attempts) >= LOGIN_FAILURES_LIMIT

    def _remember_failure(self, address):
        self._login_failures.setdefault(address, []).append(time.time())

    # --------------------------------------------------------------- маршруты

    def handle(self, method, path, query_string, headers, body):
        """Обрабатывает запрос, если он про планировщик, иначе возвращает None."""
        if path != "/planner" and not path.startswith("/planner/"):
            return None
        query = urllib.parse.parse_qs(query_string or "")
        if not self.store.accounts():
            return (503, "text/html; charset=utf-8", SETUP_PAGE.encode("utf-8"),
                    {"Cache-Control": "no-store"})
        if path == "/planner/login":
            if method == "POST":
                return self._handle_login(body, headers)
            return self._login_response()
        if path == "/planner/api/health":
            return self._json({"ok": True,
                               "time": datetime.now().isoformat(timespec="seconds")})
        if path == "/planner/logout":
            return self._redirect("/planner/login",
                                  {"Set-Cookie": "%s=; Path=/planner; Max-Age=0; "
                                                 "HttpOnly; SameSite=Lax" % COOKIE_NAME})
        account = self.current_account(headers)
        if account is None:
            if path.startswith("/planner/api/"):
                return (401, "application/json; charset=utf-8",
                        b'{"error": "unauthorized"}', {"Cache-Control": "no-store"})
            return self._redirect("/planner/login")
        try:
            return self._route(method, path, query, body, account)
        except Exception as error:      # приложение не должно ронять сервис ботов
            self.logger("Планёрка: ошибка %s" % error)
            message = json.dumps({"error": str(error)[:300]}, ensure_ascii=False)
            return (500, "application/json; charset=utf-8", message.encode("utf-8"),
                    {"Cache-Control": "no-store"})

    def _handle_login(self, body, headers):
        address = self._client_address(headers)
        if self._too_many_failures(address):
            return self._login_response(
                429, "Слишком много попыток. Подождите 5 минут.")
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace") if body else "")
        password = (fields.get("password") or [""])[0]
        account = self.store.find_account_by_password(password) if password else None
        if not account:
            self._remember_failure(address)
            return self._login_response(401, "Неверный пароль")
        auth = self.store.account_auth(account["id"])
        self.store.touch_account(account["id"])
        self.logger("Планёрка: вход — %s" % account["name"])
        return self._redirect("/planner/", {"Set-Cookie": self._cookie_header(
            account["id"], auth["password_hash"])})

    def _route(self, method, path, query, body, account):
        account_id = account["id"]
        app = self.app
        # ---- статика
        if path in ("/planner", "/planner/", "/planner/index.html"):
            return self._static("index.html")
        if path.startswith("/planner/static/"):
            return self._static(path[len("/planner/static/"):])

        # ---- данные аккаунта
        if method == "GET":
            if path == "/planner/api/state":
                return self._json(app.state(account_id))
            if path == "/planner/api/tasks":
                return self._json({"tasks": app.tasks_view(account_id)})
            if path == "/planner/api/plan":
                days = int((query.get("days") or ["14"])[0])
                return self._json(app.planner(account_id).build_plan(
                    days=max(1, min(days, 60))))
            if path == "/planner/api/schedule":
                days = int((query.get("days") or ["7"])[0])
                return self._json({"pairs": app.feed(account_id).pairs(
                    days=max(1, min(days, 30)))})
            if path == "/planner/api/stats":
                return self._json(app.stats_view(account_id))
            if path == "/planner/api/subjects":
                return self._json({"subjects": app.feed(account_id).subjects()})
            if path == "/planner/api/groups":
                return self._json(app.available_groups(account_id))
            if path == "/planner/api/accounts":
                if not self.is_owner(account):
                    return self._json({"error": "forbidden"}, status=403)
                return self._json({"accounts": app.accounts_view(),
                                   "me": account_id})
            if path == "/planner/api/plan.ics":
                days = int((query.get("days") or ["14"])[0])
                return (200, "text/calendar; charset=utf-8",
                        app.ics_view(account_id, days=max(1, min(days, 60))).encode("utf-8"),
                        {"Content-Disposition": 'attachment; filename="planyorka.ics"'})
            return self._json({"error": "not found"}, status=404)

        payload = self._payload(body)
        if path == "/planner/api/chat":
            return self._json(app.assistant(account_id).handle(payload.get("text") or ""))
        if path == "/planner/api/tasks":
            return self._json(self._create_task(payload, account_id))
        if path == "/planner/api/plan":
            return self._json(app.planner(account_id).plan())
        if path == "/planner/api/plan/reset":
            app.store.clear_plan(account_id)
            return self._json({"ok": True,
                               "plan": app.planner(account_id).build_plan()})
        if path == "/planner/api/groups":
            return self._json(app.set_groups(payload.get("groups") or [], account_id))
        if path == "/planner/api/settings":
            allowed = ("study_windows", "max_daily_minutes", "min_block",
                       "max_block", "break_minutes", "safety_days", "days_off",
                       "horizon_days", "reminder_time", "telegram_chat_id")
            for key, value in payload.items():
                if key in allowed:
                    app.store.set_setting(account_id, key, value)
            return self._json({"ok": True,
                               "settings": app.store.settings(account_id)})
        if path == "/planner/api/remind":
            plan = app.planner(account_id).build_plan(days=1)
            day = next((item for item in plan["days"] if item["is_today"]), None)
            chat_id = str(app.store.settings(account_id).get("telegram_chat_id") or "0")
            if not chat_id.isdigit() or chat_id == "0":
                return self._json({"ok": False,
                                   "error": "Укажи Telegram chat_id в настройках"})
            return self._json({"ok": core.send_telegram(chat_id,
                                                        app._telegram_text(day))})
        if path == "/planner/api/focus":
            minutes = int(payload.get("minutes") or 25)
            started = datetime.now().isoformat(timespec="seconds")
            ended = (datetime.now() + timedelta(minutes=minutes)).isoformat(
                timespec="seconds")
            app.store.add_session(account_id, int(payload.get("task_id") or 0),
                                  int(payload.get("subtask_id") or 0),
                                  minutes, started, ended)
            return self._json({"ok": True, "minutes": minutes})

        # ---- управление аккаунтами (только владелец)
        if path.startswith("/planner/api/accounts"):
            if not self.is_owner(account):
                return self._json({"error": "forbidden"}, status=403)
            return self._route_accounts(method, path, payload, account)

        # ---- отметки задач и шагов
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["planner", "api", "subtasks"] and parts[4] == "done":
            app.store.set_subtask_done(int(parts[3]), bool(payload.get("done", True)),
                                       account_id)
            return self._json({"ok": True})
        if len(parts) == 5 and parts[:3] == ["planner", "api", "tasks"] and parts[4] == "done":
            app.store.update_task(int(parts[3]), account_id,
                                  done=1 if payload.get("done") else 0)
            return self._json({"ok": True})
        if len(parts) == 4 and parts[:3] == ["planner", "api", "tasks"] and method == "DELETE":
            app.store.delete_task(int(parts[3]), account_id)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, status=404)

    def _route_accounts(self, method, path, payload, account):
        app = self.app
        parts = path.strip("/").split("/")
        if method == "POST" and path == "/planner/api/accounts":
            name = str(payload.get("name") or "").strip()
            if not name:
                return self._json({"error": "Укажите имя"}, status=400)
            created = app.create_account(name, payload.get("password") or None)
            self.logger("Планёрка: создан аккаунт %s" % created["name"])
            return self._json({"ok": True, "account": created})
        if method == "POST" and len(parts) == 5 and parts[4] == "password":
            target = int(parts[3])
            if app.store.account(target) is None:
                return self._json({"error": "нет такого аккаунта"}, status=404)
            reset = app.reset_password(target)
            return self._json({"ok": True, **reset})
        if method == "POST" and len(parts) == 5 and parts[4] == "name":
            target = int(parts[3])
            return self._json(app.rename_account(target,
                                                 str(payload.get("name") or "")))
        if method == "DELETE" and len(parts) == 4:
            target = int(parts[3])
            if target == int(account["id"]):
                return self._json({"error": "Нельзя удалить свой аккаунт"},
                                  status=400)
            if target == self._owner_id():
                return self._json({"error": "Нельзя удалить аккаунт владельца"},
                                  status=400)
            return self._json(app.delete_account(target))
        return self._json({"error": "not found"}, status=404)

    def _create_task(self, payload, account_id):
        title = (payload.get("title") or "").strip()
        if not title:
            return {"error": "Пустое название задачи"}
        app = self.app
        kind = payload.get("kind") or "другое"
        estimate = int(payload.get("estimate_minutes") or 0)
        if not estimate:
            steps = app.assistant(account_id).decompose(title, kind, 90)
            estimate = sum(minutes for _, minutes in steps)
        task_id = app.store.add_task(account_id, title=title,
                                     subject=payload.get("subject") or "",
                                     kind=kind, deadline=payload.get("deadline") or "",
                                     priority=int(payload.get("priority") or 2),
                                     estimate_minutes=estimate, source="ui")
        if payload.get("auto_steps", True):
            steps = app.assistant(account_id).decompose(title, kind, estimate)
            for index, (label, minutes) in enumerate(steps):
                app.store.add_subtask(task_id, label, minutes, index,
                                      account_id=account_id)
        plan = app.planner(account_id).plan()
        return {"ok": True, "task_id": task_id, "plan": plan}

    # -------------------------------------------------------------- ответы

    @staticmethod
    def _json(payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return (status, "application/json; charset=utf-8", body,
                {"Cache-Control": "no-store"})

    @staticmethod
    def _payload(body):
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _static(self, name):
        safe = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not safe.startswith(STATIC_DIR) or not os.path.exists(safe):
            return (404, "text/plain; charset=utf-8", b"not found", {})
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        with open(safe, "rb") as handle:
            data = handle.read()
        return (200, content_type, data, {"Cache-Control": "no-cache"})

    # ------------------------------------------------------- напоминания

    def start_background(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._reminder_loop,
                                        name="planner-reminder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _reminder_loop(self):
        while not self._stop.is_set():
            try:
                self.app.send_reminders_once()
            except Exception as error:
                self.logger("Планёрка: напоминание — %s" % error)
            self._stop.wait(55)
