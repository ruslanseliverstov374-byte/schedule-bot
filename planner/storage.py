# -*- coding: utf-8 -*-
"""Хранилище планировщика: SQLite, только стандартная библиотека.

Одна база — несколько аккаунтов. У каждого аккаунта свои задачи, шаги, сессии
и настройки, поэтому разные люди (например, парень и девушка) не видят задачи
друг друга. Все запросы фильтруются по account_id.

Таблицы:
    planner_accounts          — аккаунты: имя, хеш пароля, владелец;
    planner_tasks             — задачи, привязаны к аккаунту;
    planner_subtasks          — шаги задач с оценкой времени и слотом в календаре;
    planner_sessions          — фокус-сессии для диаграмм «план/факт»;
    planner_account_settings  — настройки в формате ключ-значение на аккаунт.

Все времена в базе — ISO-строки: дата 'ГГГГ-ММ-ДД', время 'ЧЧ:ММ'.
"""

import hashlib
import hmac
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    password_hash TEXT DEFAULT '',
    salt TEXT DEFAULT '',
    is_owner INTEGER DEFAULT 0,
    telegram_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '',
    last_seen TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS planner_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER DEFAULT 1,
    title TEXT NOT NULL,
    subject TEXT DEFAULT '',
    kind TEXT DEFAULT 'другое',
    deadline TEXT DEFAULT '',          -- 'ГГГГ-ММ-ДД ЧЧ:ММ' или 'ГГГГ-ММ-ДД'
    priority INTEGER DEFAULT 2,        -- 1 высокий, 2 обычный, 3 низкий
    estimate_minutes INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS planner_subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    estimate_minutes INTEGER DEFAULT 30,
    planned_date TEXT DEFAULT '',
    planned_start TEXT DEFAULT '',
    planned_end TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    ord INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS planner_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER DEFAULT 1,
    task_id INTEGER DEFAULT 0,
    subtask_id INTEGER DEFAULT 0,
    minutes INTEGER DEFAULT 0,
    started_at TEXT DEFAULT '',
    ended_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS planner_account_settings (
    account_id INTEGER DEFAULT 1,
    key TEXT NOT NULL,
    value TEXT DEFAULT '',
    PRIMARY KEY (account_id, key)
);
CREATE INDEX IF NOT EXISTS idx_planner_subtasks_date ON planner_subtasks(planned_date);
"""

DEFAULT_SETTINGS = {
    # Когда вообще можно учиться (пересечение с парами вычитается).
    "study_windows": "09:00-13:00,14:30-22:00",
    # Сколько максимум минут учёбы в день (защита от перегруза).
    "max_daily_minutes": "300",
    # Минимальный и максимальный блок работы; перерыв между блоками.
    "min_block": "25",
    "max_block": "90",
    "break_minutes": "10",
    # За сколько дней до дедлайна заканчивать (страховка на проверку).
    "safety_days": "1",
    # Часовой пояс и время вечернего напоминания.
    "tz_offset": "3",
    "reminder_time": "20:30",
    # На сколько дней вперёд планируем.
    "horizon_days": "14",
    # Группы, расписание которых учитываем: "университет:код,университет:код".
    "schedule_groups": "",
    # Дни без учёбы: 6=воскресенье (0 — понедельник).
    "days_off": "6",
    # Телеграм-напоминания (chat_id, 0 — выключены).
    "telegram_chat_id": "0",
}

DEFAULT_ACCOUNT_NAME = "Владелец"


def hash_password(password, salt=None):
    """Хеш пароля с солью: в базе открытого пароля нет."""
    salt = salt or uuid.uuid4().hex[:16]
    digest = hashlib.sha256((salt + str(password)).encode("utf-8")).hexdigest()
    return digest, salt


def check_password(password, password_hash, salt):
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, str(password_hash or ""))


class Store:
    """БД планировщика: потокобезопасные короткие транзакции."""

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.executescript(SCHEMA)
        self._migrate()

    # ------------------------------------------------------------- служебное

    def _columns(self, table):
        try:
            return {row[1] for row in self.connection.execute(
                "PRAGMA table_info(%s)" % table)}
        except sqlite3.Error:
            return set()

    def _has_table(self, name):
        row = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _migrate(self):
        """Догоняет схему для баз, созданных до появления аккаунтов."""
        with self._lock, self.connection:
            if "account_id" not in self._columns("planner_tasks"):
                self.connection.execute(
                    "ALTER TABLE planner_tasks ADD COLUMN account_id INTEGER DEFAULT 1")
            if "account_id" not in self._columns("planner_sessions"):
                self.connection.execute(
                    "ALTER TABLE planner_sessions ADD COLUMN account_id INTEGER DEFAULT 1")
            # Индексы по новым столбцам создаём только после миграции: иначе на
            # старой базе (без account_id) CREATE INDEX падает при запуске.
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_planner_tasks_done ON planner_tasks(done)")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_planner_tasks_account"
                " ON planner_tasks(account_id)")
        self.migrate_legacy_settings()

    def migrate_legacy_settings(self):
        """Переносит старые настройки (одна на всех) в аккаунт владельца.

        Вызывается повторно после создания первого аккаунта: до этого момента
        переносить некуда. Таблицу не удаляем, а переименовываем — так ничего не
        теряется, даже если миграция понадобится ещё раз.
        """
        with self._lock, self.connection:
            if not self._has_table("planner_settings"):
                return
            owner = self.connection.execute(
                "SELECT id FROM planner_accounts ORDER BY id LIMIT 1").fetchone()
            if owner is None:
                return
            legacy = self.connection.execute(
                "SELECT key, value FROM planner_settings").fetchall()
            for key, value in legacy:
                # Значения из старой базы важнее умолчаний: это настройки,
                # которые человек уже выбирал (например, свои группы).
                self.connection.execute(
                    "INSERT INTO planner_account_settings(account_id, key, value)"
                    " VALUES(?,?,?) ON CONFLICT(account_id, key) DO UPDATE SET"
                    " value=excluded.value", (owner[0], key, value))
            self.connection.execute(
                "ALTER TABLE planner_settings RENAME TO planner_settings_legacy")

    def close(self):
        with self._lock:
            self.connection.close()

    def query(self, sql, params=()):
        with self._lock:
            cursor = self.connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self._lock, self.connection:
            cursor = self.connection.execute(sql, params)
            return cursor.lastrowid

    # -------------------------------------------------------------- аккаунты

    def accounts(self):
        return self.query("SELECT id, name, is_owner, telegram_id, created_at"
                          " FROM planner_accounts ORDER BY id")

    def account(self, account_id):
        return self.query_one(
            "SELECT id, name, is_owner, telegram_id, created_at, last_seen"
            " FROM planner_accounts WHERE id=?", (int(account_id),))

    def account_name(self, account_id):
        row = self.account(account_id)
        return (row or {}).get("name") or ""

    def first_account_id(self):
        row = self.query_one("SELECT id FROM planner_accounts ORDER BY id LIMIT 1")
        return int(row["id"]) if row else 0

    def owner_account_id(self):
        row = self.query_one(
            "SELECT id FROM planner_accounts WHERE is_owner=1 ORDER BY id LIMIT 1")
        return int(row["id"]) if row else self.first_account_id()

    def create_account(self, name, password, is_owner=False, telegram_id=0):
        """Создаёт аккаунт; пароль хранится хешем. Возвращает id."""
        password_hash, salt = hash_password(password)
        account_id = self.execute(
            "INSERT INTO planner_accounts(name, password_hash, salt, is_owner,"
            " telegram_id, created_at) VALUES(?,?,?,?,?,?)",
            (str(name).strip() or "Аккаунт", password_hash, salt,
             1 if is_owner else 0, int(telegram_id or 0),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        for key, value in DEFAULT_SETTINGS.items():
            self.execute(
                "INSERT OR IGNORE INTO planner_account_settings(account_id, key, value)"
                " VALUES(?,?,?)", (account_id, key, value))
        return account_id

    def set_account_password(self, account_id, password):
        password_hash, salt = hash_password(password)
        self.execute("UPDATE planner_accounts SET password_hash=?, salt=? WHERE id=?",
                     (password_hash, salt, int(account_id)))

    def rename_account(self, account_id, name):
        self.execute("UPDATE planner_accounts SET name=? WHERE id=?",
                     (str(name).strip() or "Аккаунт", int(account_id)))

    def delete_account(self, account_id):
        """Удаляет аккаунт вместе с его задачами и настройками."""
        account_id = int(account_id)
        self.execute("DELETE FROM planner_subtasks WHERE task_id IN"
                     " (SELECT id FROM planner_tasks WHERE account_id=?)",
                     (account_id,))
        self.execute("DELETE FROM planner_tasks WHERE account_id=?", (account_id,))
        self.execute("DELETE FROM planner_sessions WHERE account_id=?", (account_id,))
        self.execute("DELETE FROM planner_account_settings WHERE account_id=?",
                     (account_id,))
        self.execute("DELETE FROM planner_accounts WHERE id=?", (account_id,))

    def find_account_by_password(self, password):
        """Аккаунт по паролю: вход без логина, пароли уникальны по смыслу."""
        if not password:
            return None
        rows = self.query("SELECT id, name, is_owner, password_hash, salt"
                          " FROM planner_accounts ORDER BY id")
        for row in rows:
            if check_password(password, row["password_hash"], row["salt"]):
                return {"id": int(row["id"]), "name": row["name"],
                        "is_owner": int(row["is_owner"] or 0)}
        return None

    def touch_account(self, account_id):
        self.execute("UPDATE planner_accounts SET last_seen=? WHERE id=?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(account_id)))

    def account_auth(self, account_id):
        """Данные для проверки сессии: id, имя, владелец, хеш и соль пароля."""
        return self.query_one(
            "SELECT id, name, is_owner, password_hash, salt FROM planner_accounts"
            " WHERE id=?", (int(account_id),))

    def set_account_telegram(self, account_id, telegram_id):
        self.execute("UPDATE planner_accounts SET telegram_id=? WHERE id=?",
                     (int(telegram_id or 0), int(account_id)))

    def legacy_settings(self):
        """Старые настройки до появления аккаунтов (для миграции пароля)."""
        with self._lock:
            if not self._has_table("planner_settings"):
                return {}
            try:
                rows = self.connection.execute(
                    "SELECT key, value FROM planner_settings").fetchall()
            except sqlite3.Error:
                return {}
        return {row[0]: row[1] for row in rows}

    # -------------------------------------------------------------- настройки

    def settings(self, account_id=1):
        """Настройки аккаунта с подстановкой значений по умолчанию."""
        rows = self.query(
            "SELECT key, value FROM planner_account_settings WHERE account_id=?",
            (int(account_id),))
        settings = dict(DEFAULT_SETTINGS)
        settings.update({row["key"]: row["value"] for row in rows})
        return settings

    def set_setting(self, account_id, key, value):
        self.execute(
            "INSERT INTO planner_account_settings(account_id, key, value)"
            " VALUES(?,?,?) ON CONFLICT(account_id, key) DO UPDATE SET"
            " value=excluded.value",
            (int(account_id), str(key), str(value)))

    # ----------------------------------------------------------------- задачи

    def add_task(self, account_id=1, title="", subject="", kind="другое",
                 deadline="", priority=2, estimate_minutes=0, notes="", source=""):
        return self.execute(
            "INSERT INTO planner_tasks(account_id, title, subject, kind, deadline,"
            " priority, estimate_minutes, created_at, notes, source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(account_id), title.strip(), subject.strip(), kind, deadline,
             int(priority), int(estimate_minutes),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), notes, source))

    def task(self, task_id, account_id=1):
        return self.query_one(
            "SELECT * FROM planner_tasks WHERE id=? AND account_id=?",
            (int(task_id), int(account_id)))

    def tasks(self, account_id=1, include_done=False):
        where = "" if include_done else " AND done=0"
        return self.query(
            "SELECT * FROM planner_tasks WHERE account_id=?%s"
            " ORDER BY done ASC, CASE WHEN deadline='' THEN 1 ELSE 0 END,"
            " deadline ASC, priority ASC, id ASC" % where, (int(account_id),))

    def update_task(self, task_id, account_id=1, **fields):
        allowed = {"title", "subject", "kind", "deadline", "priority",
                   "estimate_minutes", "done", "notes"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return
        assignments = ", ".join("%s=?" % key for key, _ in pairs)
        self.execute("UPDATE planner_tasks SET %s WHERE id=? AND account_id=?" % assignments,
                     tuple(value for _, value in pairs) + (int(task_id),
                                                           int(account_id)))

    def delete_task(self, task_id, account_id=1):
        self.execute("DELETE FROM planner_subtasks WHERE task_id IN"
                     " (SELECT id FROM planner_tasks WHERE id=? AND account_id=?)",
                     (int(task_id), int(account_id)))
        self.execute("DELETE FROM planner_tasks WHERE id=? AND account_id=?",
                     (int(task_id), int(account_id)))

    # ------------------------------------------------------------------- шаги

    def add_subtask(self, task_id, title, estimate_minutes=30, ord_value=0,
                    account_id=1):
        owned = self.task(task_id, account_id)
        if owned is None:
            return 0
        return self.execute(
            "INSERT INTO planner_subtasks(task_id, title, estimate_minutes, ord)"
            " VALUES(?,?,?,?)",
            (int(task_id), title.strip(), int(estimate_minutes), int(ord_value)))

    def subtasks(self, task_id, account_id=1):
        if self.task(task_id, account_id) is None:
            return []
        return self.query("SELECT * FROM planner_subtasks WHERE task_id=?"
                          " ORDER BY ord, id", (int(task_id),))

    def all_subtasks(self, account_id=1, only_planned=False):
        where = " AND s.planned_date<>''" if only_planned else ""
        return self.query(
            "SELECT s.*, t.title AS task_title, t.subject, t.kind, t.deadline,"
            " t.priority, t.done AS task_done"
            " FROM planner_subtasks s JOIN planner_tasks t ON t.id=s.task_id"
            " WHERE t.account_id=?%s"
            " ORDER BY s.planned_date, s.planned_start, s.ord" % where,
            (int(account_id),))

    def plan_subtask(self, subtask_id, planned_date, planned_start, planned_end,
                     account_id=1):
        self.execute(
            "UPDATE planner_subtasks SET planned_date=?, planned_start=?,"
            " planned_end=? WHERE id=? AND task_id IN"
            " (SELECT id FROM planner_tasks WHERE account_id=?)",
            (planned_date, planned_start, planned_end, int(subtask_id),
             int(account_id)))

    def clear_plan(self, account_id=1, task_id=None):
        if task_id is None:
            self.execute(
                "UPDATE planner_subtasks SET planned_date='', planned_start='',"
                " planned_end='' WHERE task_id IN"
                " (SELECT id FROM planner_tasks WHERE account_id=?)",
                (int(account_id),))
        else:
            self.execute(
                "UPDATE planner_subtasks SET planned_date='', planned_start='',"
                " planned_end='' WHERE task_id=? AND task_id IN"
                " (SELECT id FROM planner_tasks WHERE account_id=?)",
                (int(task_id), int(account_id)))

    def set_subtask_done(self, subtask_id, done=True, account_id=1):
        self.execute(
            "UPDATE planner_subtasks SET done=? WHERE id=? AND task_id IN"
            " (SELECT id FROM planner_tasks WHERE account_id=?)",
            (1 if done else 0, int(subtask_id), int(account_id)))

    def delete_subtasks(self, task_id, account_id=1):
        self.execute("DELETE FROM planner_subtasks WHERE task_id=? AND task_id IN"
                     " (SELECT id FROM planner_tasks WHERE account_id=?)",
                     (int(task_id), int(account_id)))

    # -------------------------------------------------------------- сессии

    def add_session(self, account_id=1, task_id=0, subtask_id=0, minutes=0,
                    started_at="", ended_at=""):
        return self.execute(
            "INSERT INTO planner_sessions(account_id, task_id, subtask_id, minutes,"
            " started_at, ended_at) VALUES(?,?,?,?,?,?)",
            (int(account_id), int(task_id or 0), int(subtask_id or 0), int(minutes),
             started_at, ended_at))

    def sessions(self, account_id=1):
        return self.query("SELECT * FROM planner_sessions WHERE account_id=?"
                          " ORDER BY started_at", (int(account_id),))

    def sessions_between(self, start_iso, end_iso, account_id=1):
        return self.query(
            "SELECT * FROM planner_sessions WHERE account_id=?"
            " AND started_at >= ? AND started_at < ?",
            (int(account_id), start_iso, end_iso))

    def sessions_total(self, account_id=1):
        row = self.query_one(
            "SELECT COALESCE(SUM(minutes),0) AS total FROM planner_sessions"
            " WHERE account_id=?", (int(account_id),))
        return int((row or {}).get("total") or 0)

    # --------------------------------------------------------------- сводка

    def counts(self, account_id=1):
        """Сколько задач и шагов в каком состоянии — для шапки интерфейса."""
        tasks = self.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN done=0 THEN 1 ELSE 0 END) AS open,"
            " SUM(CASE WHEN done=1 THEN 1 ELSE 0 END) AS closed"
            " FROM planner_tasks WHERE account_id=?", (int(account_id),)) or {}
        subtasks = self.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN s.done=0 THEN 1 ELSE 0 END) AS open,"
            " SUM(CASE WHEN s.done=1 THEN 1 ELSE 0 END) AS closed"
            " FROM planner_subtasks s JOIN planner_tasks t ON t.id=s.task_id"
            " WHERE t.account_id=?", (int(account_id),)) or {}
        today = date.today().isoformat()
        planned_today = self.query_one(
            "SELECT COALESCE(SUM(s.estimate_minutes),0) AS minutes,"
            " COUNT(*) AS steps FROM planner_subtasks s"
            " JOIN planner_tasks t ON t.id=s.task_id"
            " WHERE t.account_id=? AND s.planned_date=? AND s.done=0",
            (int(account_id), today)) or {}
        return {
            "tasks_total": int(tasks.get("total") or 0),
            "tasks_open": int(tasks.get("open") or 0),
            "tasks_done": int(tasks.get("closed") or 0),
            "subtasks_total": int(subtasks.get("total") or 0),
            "subtasks_done": int(subtasks.get("closed") or 0),
            "today_minutes": int(planned_today.get("minutes") or 0),
            "today_steps": int(planned_today.get("steps") or 0),
            "sessions_total_minutes": self.sessions_total(account_id),
        }
