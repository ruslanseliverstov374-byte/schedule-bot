# -*- coding: utf-8 -*-
"""HTTP-слой «Планёрки» внутри сервиса ботов: вход по паролю, API и статика.

Сервис ботов уже держит один HTTP-сервер на порту Render. Планировщик
подключается к нему по путям `/planner/...`, поэтому отдельный хостинг не нужен:
сайт живёт на том же адресе, что и боты, и просыпается вместе с ними.

Вход — по паролю из переменной окружения PLANNER_PASSWORD (cookie подписана
HMAC). Пока пароль не задан, вместо приложения показывается подсказка, что его
нужно задать в настройках Render.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

from . import core

COOKIE_NAME = "planner_session"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SETUP_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Планёрка — настройка</title>
<style>body{font:16px/1.6 "Segoe UI",Arial,sans-serif;max-width:640px;margin:80px auto;
padding:0 20px;color:#1c2333}code{background:#eef2ff;padding:2px 6px;border-radius:6px}</style>
</head><body><h1>📚 Планёрка</h1>
<p>Приложение уже установлено в сервисе, но ещё не защищено паролем.</p>
<p>Добавьте в Render → Environment переменную <code>PLANNER_PASSWORD</code>
с любым паролем, и сервис перезапустится сам. После этого откройте
<code>/planner</code> и войдите.</p>
</body></html>"""

LOGIN_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Планёрка — вход</title>
<style>
body{font:16px/1.6 "Segoe UI",Arial,sans-serif;background:#f4f6fb;color:#1c2333;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#fff;border-radius:16px;padding:32px 36px;box-shadow:0 12px 40px rgba(20,30,60,.12);
width:min(360px,90vw)}
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
<h1>📚 Планёрка</h1><p>Личный планировщик учёбы. Введите пароль.</p>
%s
<form method="post" action="/planner/login">
<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">Войти</button>
</form></div></body></html>"""


class PlannerService:
    """Планировщик, подключённый к общему HTTP-серверу сервиса."""

    def __init__(self, db_path=None, schedule_bot_path=None, groups=None,
                 password=None, logger=print):
        self.app = core.App(db_path=db_path or core.DB_PATH,
                            schedule_bot_path=schedule_bot_path or core.ROOT,
                            groups=groups)
        self.password = password if password is not None else os.environ.get(
            "PLANNER_PASSWORD", "")
        self.logger = logger if callable(logger) else (lambda message: None)
        self._stop = threading.Event()
        self._thread = None
        self._last_cookie = ""

    def ensure_password(self):
        """Пароль для входа: из окружения, из базы или новый (сгенерированный).

        Возвращает сгенерированный пароль, если его только что создали: его
        нужно отправить владельцу в Telegram. Иначе None — пароль уже был.
        """
        if self.password:
            return None
        saved = str(self.app.store.settings().get("planner_password") or "").strip()
        if saved:
            self.password = saved
            return None
        alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
        self.password = "".join(secrets.choice(alphabet) for _ in range(12))
        self.app.store.set_setting("planner_password", self.password)
        return self.password

    # ------------------------------------------------------------------ вход

    def _secret(self):
        return hashlib.sha256(("planner:" + self.password).encode()).hexdigest()

    def session_token(self):
        return hmac.new(self._secret().encode(), b"owner", hashlib.sha256).hexdigest()

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

    def authenticated(self, headers):
        if not self.password:
            return False
        cookie = self._cookies(headers).get(COOKIE_NAME, "")
        return bool(cookie) and hmac.compare_digest(cookie, self.session_token())

    def _login_response(self, status=200, error=""):
        block = '<div class="error">%s</div>' % error if error else ""
        # В шаблоне есть CSS с процентами, поэтому подставляем заменой, а не %.
        body = LOGIN_PAGE.replace("%s", block, 1)
        return (status, "text/html; charset=utf-8", body.encode("utf-8"),
                {"Cache-Control": "no-store"})

    @staticmethod
    def _redirect(location, headers=None):
        extra = {"Location": location, "Cache-Control": "no-store"}
        if headers:
            extra.update(headers)
        return (303, "text/html; charset=utf-8", b"", extra)

    # --------------------------------------------------------------- маршруты

    def handle(self, method, path, query_string, headers, body):
        """Обрабатывает запрос, если он про планировщик, иначе возвращает None."""
        if path != "/planner" and not path.startswith("/planner/"):
            return None
        query = urllib.parse.parse_qs(query_string or "")
        if not self.password:
            return (503, "text/html; charset=utf-8", SETUP_PAGE.encode("utf-8"),
                    {"Cache-Control": "no-store"})
        if path == "/planner/login":
            if method == "POST":
                return self._handle_login(body)
            return self._login_response()
        if path == "/planner/api/health":
            # Открыт без пароля: по нему внешний мониторинг проверяет, что
            # встроенное приложение живо (данных не отдаёт).
            return self._json({"ok": True,
                               "time": datetime.now().isoformat(timespec="seconds")})
        if path == "/planner/logout":
            return self._redirect("/planner/login",
                                  {"Set-Cookie": "%s=; Path=/planner; Max-Age=0; "
                                                 "HttpOnly; SameSite=Lax" % COOKIE_NAME})
        if not self.authenticated(headers):
            if path.startswith("/planner/api/"):
                return (401, "application/json; charset=utf-8",
                        b'{"error": "unauthorized"}', {"Cache-Control": "no-store"})
            return self._redirect("/planner/login")
        try:
            return self._route(method, path, query, body)
        except Exception as error:      # приложение не должно ронять сервис ботов
            self.logger("Планёрка: ошибка %s" % error)
            message = json.dumps({"error": str(error)[:300]}, ensure_ascii=False)
            return (500, "application/json; charset=utf-8", message.encode("utf-8"),
                    {"Cache-Control": "no-store"})

    def _handle_login(self, body):
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace") if body else "")
        password = (fields.get("password") or [""])[0]
        # Сравниваем байты: compare_digest не принимает не-ASCII строки (а пароль
        # может быть с русскими буквами).
        valid = bool(password) and hmac.compare_digest(
            password.encode("utf-8"), self.password.encode("utf-8"))
        if not valid:
            return self._login_response(401, "Неверный пароль")
        cookie = "%s=%s; Path=/planner; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            COOKIE_NAME, self.session_token(), 60 * 60 * 24 * 30)
        return self._redirect("/planner/", {"Set-Cookie": cookie})

    def _route(self, method, path, query, body):
        # ---- статика
        if path in ("/planner", "/planner/", "/planner/index.html"):
            return self._static("index.html")
        if path.startswith("/planner/static/"):
            return self._static(path[len("/planner/static/"):])

        # ---- данные
        app = self.app
        if method == "GET":
            if path == "/planner/api/state":
                return self._json(app.state())
            if path == "/planner/api/tasks":
                return self._json({"tasks": app.tasks_view()})
            if path == "/planner/api/plan":
                days = int((query.get("days") or ["14"])[0])
                return self._json(app.planner.build_plan(days=max(1, min(days, 60))))
            if path == "/planner/api/schedule":
                days = int((query.get("days") or ["7"])[0])
                return self._json({"pairs": app.feed.pairs(days=max(1, min(days, 30)))})
            if path == "/planner/api/stats":
                return self._json(app.stats_view())
            if path == "/planner/api/subjects":
                return self._json({"subjects": app.feed.subjects()})
            if path == "/planner/api/groups":
                return self._json(app.available_groups())
            if path == "/planner/api/plan.ics":
                days = int((query.get("days") or ["14"])[0])
                return (200, "text/calendar; charset=utf-8",
                        app.ics_view(days=max(1, min(days, 60))).encode("utf-8"),
                        {"Content-Disposition": 'attachment; filename="planyorka.ics"'})
            return self._json({"error": "not found"}, status=404)

        payload = self._payload(body)
        if path == "/planner/api/chat":
            return self._json(app.assistant.handle(payload.get("text") or ""))
        if path == "/planner/api/tasks":
            return self._json(self._create_task(payload))
        if path == "/planner/api/plan":
            return self._json(app.planner.plan())
        if path == "/planner/api/plan/reset":
            app.store.clear_plan()
            return self._json({"ok": True, "plan": app.planner.build_plan()})
        if path == "/planner/api/groups":
            return self._json(app.set_groups(payload.get("groups") or []))
        if path == "/planner/api/settings":
            allowed = ("study_windows", "max_daily_minutes", "min_block",
                       "max_block", "break_minutes", "safety_days", "days_off",
                       "horizon_days", "reminder_time", "telegram_chat_id")
            for key, value in payload.items():
                if key in allowed:
                    app.store.set_setting(key, value)
            return self._json({"ok": True, "settings": app.store.settings()})
        if path == "/planner/api/remind":
            plan = app.planner.build_plan(days=1)
            day = next((item for item in plan["days"] if item["is_today"]), None)
            chat_id = str(app.store.settings().get("telegram_chat_id") or "0")
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
            app.store.add_session(int(payload.get("task_id") or 0),
                                  int(payload.get("subtask_id") or 0),
                                  minutes, started, ended)
            return self._json({"ok": True, "minutes": minutes})
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["planner", "api", "subtasks"] and parts[4] == "done":
            app.store.set_subtask_done(int(parts[3]), bool(payload.get("done", True)))
            return self._json({"ok": True})
        if len(parts) == 5 and parts[:3] == ["planner", "api", "tasks"] and parts[4] == "done":
            app.store.update_task(int(parts[3]), done=1 if payload.get("done") else 0)
            return self._json({"ok": True})
        if len(parts) == 4 and parts[:3] == ["planner", "api", "tasks"] and method == "DELETE":
            app.store.delete_task(int(parts[3]))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, status=404)

    def _create_task(self, payload):
        title = (payload.get("title") or "").strip()
        if not title:
            return {"error": "Пустое название задачи"}
        kind = payload.get("kind") or "другое"
        estimate = int(payload.get("estimate_minutes") or 0)
        if not estimate:
            steps = self.app.assistant.decompose(title, kind, 90)
            estimate = sum(minutes for _, minutes in steps)
        task_id = self.app.store.add_task(
            title=title, subject=payload.get("subject") or "", kind=kind,
            deadline=payload.get("deadline") or "",
            priority=int(payload.get("priority") or 2), estimate_minutes=estimate,
            source="ui")
        if payload.get("auto_steps", True):
            steps = self.app.assistant.decompose(title, kind, estimate)
            for index, (label, minutes) in enumerate(steps):
                self.app.store.add_subtask(task_id, label, minutes, index)
        plan = self.app.planner.plan()
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
        """Фоновая отправка вечернего плана в Telegram (если chat_id задан)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._reminder_loop,
                                        name="planner-reminder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _reminder_loop(self):
        while not self._stop.is_set():
            try:
                self._maybe_send_reminder()
            except Exception as error:
                self.logger("Планёрка: напоминание — %s" % error)
            self._stop.wait(55)

    def _maybe_send_reminder(self):
        raw = self.app.store.settings()
        chat_id = str(raw.get("telegram_chat_id") or "0").strip()
        if not chat_id.isdigit() or chat_id == "0":
            return
        tz = int(raw.get("tz_offset") or 3)
        now = datetime.utcnow() + timedelta(hours=tz)
        if now.strftime("%H:%M") != (raw.get("reminder_time") or "20:30").strip():
            return
        today = now.date().isoformat()
        if raw.get("reminder_sent") == today:
            return
        plan = self.app.planner.build_plan(days=1)
        day = next((item for item in plan["days"] if item["is_today"]), None)
        text = self.app._telegram_text(day)
        if text and core.send_telegram(chat_id, text):
            self.app.store.set_setting("reminder_sent", today)
