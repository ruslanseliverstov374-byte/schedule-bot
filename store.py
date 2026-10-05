# -*- coding: utf-8 -*-
"""Хранилище бота: SQLite без внешних зависимостей.

Каждый метод открывает собственное соединение, поэтому база безопасно
используется и из потока опроса Telegram, и из потока напоминаний.
Включены WAL и synchronous=NORMAL — так запись быстрее и не блокирует чтение.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id           INTEGER PRIMARY KEY,
    username        TEXT    DEFAULT '',
    first_name      TEXT    DEFAULT '',
    group_id        INTEGER,
    group_title     TEXT    DEFAULT '',
    subgroup        TEXT    DEFAULT '',
    tz_offset       INTEGER DEFAULT 3,
    evening_enabled INTEGER DEFAULT 1,
    evening_time    TEXT    DEFAULT '20:00',
    before_minutes  INTEGER DEFAULT 0,
    change_alerts   INTEGER DEFAULT 1,
    hw_alerts       INTEGER DEFAULT 1,
    is_admin        INTEGER DEFAULT 0,
    state           TEXT    DEFAULT '',
    state_data      TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT '',
    updated_at      TEXT    DEFAULT '',
    last_seen       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS groups (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    has_subgroups  INTEGER DEFAULT 0,
    subgroups      TEXT    DEFAULT '[]',
    updated_at     TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_groups_name ON groups(name);

CREATE TABLE IF NOT EXISTS timetable (
    group_title TEXT    NOT NULL,
    year        INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    payload     TEXT    DEFAULT '',
    lessons     TEXT    DEFAULT '[]',
    digest      TEXT    DEFAULT '',
    fetched_at  TEXT    DEFAULT '',
    PRIMARY KEY (group_title, year, week)
);

CREATE TABLE IF NOT EXISTS changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_title  TEXT    NOT NULL,
    year         INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    date         TEXT    DEFAULT '',
    kind         TEXT    DEFAULT '',
    summary      TEXT    DEFAULT '',
    created_at   TEXT    DEFAULT '',
    announced    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_group ON changes(group_title, announced);

CREATE TABLE IF NOT EXISTS homework (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_title TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL DEFAULT '',
    task        TEXT    NOT NULL DEFAULT '',
    due_date    TEXT    DEFAULT '',
    due_time    TEXT    DEFAULT '',
    teacher     TEXT    DEFAULT '',
    room        TEXT    DEFAULT '',
    scope       TEXT    DEFAULT 'group',
    owner_id    INTEGER,
    created_by  INTEGER,
    created_at  TEXT    DEFAULT '',
    status      TEXT    DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_homework_group ON homework(group_title, due_date);
CREATE INDEX IF NOT EXISTS idx_homework_owner ON homework(owner_id);

CREATE TABLE IF NOT EXISTS homework_votes (
    hw_id      INTEGER NOT NULL,
    tg_id      INTEGER NOT NULL,
    created_at TEXT    DEFAULT '',
    PRIMARY KEY (hw_id, tg_id)
);

CREATE TABLE IF NOT EXISTS sent (
    tg_id      INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    created_at TEXT    DEFAULT '',
    PRIMARY KEY (tg_id, kind, key)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path="data/bot.db"):
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.init_schema()

    # ---------------------------------------------------------- инфраструктура

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init_schema(self):
        with self._lock, self.connect() as connection:
            connection.executescript(SCHEMA)

    def execute(self, sql, params=()):
        with self._lock, self.connect() as connection:
            cursor = connection.execute(sql, params)
            connection.commit()
            return cursor

    def query(self, sql, params=()):
        with self._lock, self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------- meta

    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    # ------------------------------------------------------------- пользователи

    def get_user(self, tg_id):
        return self.query_one("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    def ensure_user(self, tg_id, username="", first_name=""):
        user = self.get_user(tg_id)
        if user:
            self.execute(
                "UPDATE users SET username=?, first_name=?, last_seen=? WHERE tg_id=?",
                (username or "", first_name or "", now_iso(), tg_id))
            return self.get_user(tg_id)
        is_admin = 1 if self.count_users() == 0 else 0
        self.execute(
            "INSERT INTO users(tg_id, username, first_name, is_admin, state, state_data,"
            " created_at, updated_at, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
            (tg_id, username or "", first_name or "", is_admin, "", "",
             now_iso(), now_iso(), now_iso()))
        return self.get_user(tg_id)

    def update_user(self, tg_id, **fields):
        if not fields:
            return self.get_user(tg_id)
        allowed = {"username", "first_name", "group_id", "group_title", "subgroup",
                   "tz_offset", "evening_enabled", "evening_time", "before_minutes",
                   "change_alerts", "hw_alerts", "is_admin", "state", "state_data"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return self.get_user(tg_id)
        assignments = ", ".join("%s=?" % key for key, _ in pairs)
        params = [value for _, value in pairs] + [now_iso(), now_iso(), tg_id]
        self.execute("UPDATE users SET %s, updated_at=?, last_seen=? WHERE tg_id=?"
                     % assignments, params)
        return self.get_user(tg_id)

    def set_state(self, tg_id, state, data=None):
        self.update_user(tg_id, state=state,
                         state_data=json.dumps(data, ensure_ascii=False) if data else "")

    def state_of(self, user):
        raw = (user or {}).get("state_data") or ""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def all_users(self):
        return self.query("SELECT * FROM users ORDER BY created_at")

    def count_users(self):
        return (self.query_one("SELECT COUNT(*) AS n FROM users") or {}).get("n", 0)

    def users_by_group(self, group_title):
        return self.query("SELECT * FROM users WHERE group_title=? AND group_title<>''",
                          (group_title,))

    def groups_in_use(self):
        return [row["group_title"] for row in self.query(
            "SELECT DISTINCT group_title FROM users WHERE group_title<>'' ORDER BY group_title")]

    # ------------------------------------------------------------------ группы

    def save_groups(self, groups):
        stamp = now_iso()
        with self._lock, self.connect() as connection:
            for group in groups or []:
                connection.execute(
                    "INSERT INTO groups(id, name, has_subgroups, subgroups, updated_at)"
                    " VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                    " name=excluded.name, has_subgroups=excluded.has_subgroups,"
                    " subgroups=excluded.subgroups, updated_at=excluded.updated_at",
                    (int(group.get("id") or 0), str(group.get("name") or ""),
                     1 if group.get("hasSubgroups") else 0,
                     json.dumps(group.get("subgroups") or [], ensure_ascii=False), stamp))
            connection.commit()

    def groups_count(self):
        return (self.query_one("SELECT COUNT(*) AS n FROM groups") or {}).get("n", 0)

    def groups_updated_at(self):
        row = self.query_one("SELECT MAX(updated_at) AS ts FROM groups")
        return (row or {}).get("ts") or ""

    def groups_stale(self, hours=24):
        stamp = self.groups_updated_at()
        if not stamp:
            return True
        try:
            updated = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True
        return datetime.utcnow() - updated > timedelta(hours=hours)

    def list_groups(self, offset=0, limit=10):
        return self.query("SELECT * FROM groups ORDER BY name LIMIT ? OFFSET ?",
                          (limit, offset))

    def search_groups(self, query, limit=10):
        query = (query or "").strip()
        if not query:
            return []
        return self.query(
            "SELECT * FROM groups WHERE name LIKE ? ORDER BY"
            " CASE WHEN name LIKE ? THEN 0 ELSE 1 END, name LIMIT ?",
            ("%" + query + "%", query + "%", limit))

    def group_by_name(self, name):
        return self.query_one("SELECT * FROM groups WHERE name=?", ((name or "").strip(),))

    def group_by_id(self, group_id):
        return self.query_one("SELECT * FROM groups WHERE id=?", (group_id,))

    # -------------------------------------------------------------- расписание

    def save_timetable(self, group_title, year, week, payload, lessons, digest_value):
        self.execute(
            "INSERT INTO timetable(group_title, year, week, payload, lessons, digest,"
            " fetched_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(group_title, year, week) DO UPDATE SET"
            " payload=excluded.payload, lessons=excluded.lessons,"
            " digest=excluded.digest, fetched_at=excluded.fetched_at",
            (group_title, int(year), int(week),
             json.dumps(payload, ensure_ascii=False),
             json.dumps(lessons, ensure_ascii=False), digest_value, now_iso()))

    def get_timetable(self, group_title, year, week):
        row = self.query_one(
            "SELECT * FROM timetable WHERE group_title=? AND year=? AND week=?",
            (group_title, int(year), int(week)))
        if not row:
            return None
        try:
            row["lessons"] = json.loads(row["lessons"] or "[]")
        except ValueError:
            row["lessons"] = []
        try:
            row["payload"] = json.loads(row["payload"] or "{}")
        except ValueError:
            row["payload"] = {}
        return row

    def cached_weeks(self):
        return self.query("SELECT DISTINCT group_title FROM timetable ORDER BY group_title")

    def timetable_age_minutes(self, group_title, year, week):
        row = self.get_timetable(group_title, year, week)
        if not row or not row.get("fetched_at"):
            return None
        try:
            stamp = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return (datetime.utcnow() - stamp).total_seconds() / 60.0

    def drop_old_timetable(self, weeks_keep=12):
        """Чистим кэш прошлых недель, чтобы база не росла."""
        threshold = (datetime.now(timezone.utc) - timedelta(weeks=weeks_keep)).strftime("%Y-%m-%d")
        self.execute("DELETE FROM timetable WHERE date(fetched_at) < ?", (threshold[:10],))

    # ------------------------------------------------------------- изменения

    def add_change(self, group_title, year, week, date_value, kind, summary):
        self.execute(
            "INSERT INTO changes(group_title, year, week, date, kind, summary, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (group_title, int(year), int(week), date_value or "", kind or "",
             summary or "", now_iso()))

    def pending_changes(self, group_title=None):
        if group_title:
            return self.query(
                "SELECT * FROM changes WHERE announced=0 AND group_title=? ORDER BY id",
                (group_title,))
        return self.query("SELECT * FROM changes WHERE announced=0 ORDER BY id")

    def mark_changes_announced(self, ids):
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.execute("UPDATE changes SET announced=1 WHERE id IN (%s)" % marks,
                     tuple(ids))

    def recent_changes(self, group_title, limit=10):
        return self.query(
            "SELECT * FROM changes WHERE group_title=? ORDER BY id DESC LIMIT ?",
            (group_title, limit))

    # ----------------------------------------------------------- дедлайны ДЗ

    def add_homework(self, group_title, subject, task, due_date="", due_time="",
                     teacher="", room="", scope="group", owner_id=None, created_by=None):
        cursor = self.execute(
            "INSERT INTO homework(group_title, subject, task, due_date, due_time, teacher,"
            " room, scope, owner_id, created_by, created_at, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open')",
            (group_title or "", subject or "", task or "", due_date or "", due_time or "",
             teacher or "", room or "", scope, owner_id, created_by, now_iso()))
        return cursor.lastrowid

    def get_homework(self, hw_id):
        return self.query_one("SELECT * FROM homework WHERE id=?", (hw_id,))

    def update_homework(self, hw_id, **fields):
        allowed = {"subject", "task", "due_date", "due_time", "teacher", "room", "status"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return
        assignments = ", ".join("%s=?" % key for key, _ in pairs)
        self.execute("UPDATE homework SET %s WHERE id=?" % assignments,
                     [value for _, value in pairs] + [hw_id])

    def delete_homework(self, hw_id):
        self.execute("DELETE FROM homework_votes WHERE hw_id=?", (hw_id,))
        self.execute("DELETE FROM homework WHERE id=?", (hw_id,))

    def list_homework(self, tg_id, group_title="", scope="all", due_from="", due_to="",
                      only_open=True, limit=50):
        """Список ДЗ: общие для группы + личные заметки пользователя."""
        conditions = []
        params = []
        if scope == "personal":
            conditions.append("scope='personal' AND owner_id=?")
            params.append(tg_id)
        elif scope == "group":
            conditions.append("scope='group' AND group_title=?")
            params.append(group_title or "")
        else:
            conditions.append("((scope='group' AND group_title=?) OR"
                              " (scope='personal' AND owner_id=?))")
            params.extend([group_title or "", tg_id])
        if only_open:
            conditions.append("status='open'")
        if due_from:
            conditions.append("due_date>=?")
            params.append(due_from)
        if due_to:
            conditions.append("due_date<=?")
            params.append(due_to)
        sql = ("SELECT * FROM homework WHERE " + " AND ".join(conditions) +
               " ORDER BY CASE WHEN due_date='' THEN 1 ELSE 0 END, due_date, id"
               " LIMIT ?")
        params.append(limit)
        return self.query(sql, params)

    def vote_homework(self, hw_id, tg_id):
        self.execute(
            "INSERT INTO homework_votes(hw_id, tg_id, created_at) VALUES(?,?,?)"
            " ON CONFLICT(hw_id, tg_id) DO NOTHING", (hw_id, tg_id, now_iso()))

    def unvote_homework(self, hw_id, tg_id):
        self.execute("DELETE FROM homework_votes WHERE hw_id=? AND tg_id=?", (hw_id, tg_id))

    def homework_votes(self, hw_id):
        return self.query("SELECT tg_id FROM homework_votes WHERE hw_id=?", (hw_id,))

    def homework_due(self, due_date):
        return self.query(
            "SELECT * FROM homework WHERE status='open' AND due_date=? AND scope='group'",
            (due_date,))

    # -------------------------------------------------------- отправленные

    def was_sent(self, tg_id, kind, key):
        return bool(self.query_one(
            "SELECT 1 AS ok FROM sent WHERE tg_id=? AND kind=? AND key=?", (tg_id, kind, key)))

    def mark_sent(self, tg_id, kind, key):
        self.execute(
            "INSERT INTO sent(tg_id, kind, key, created_at) VALUES(?,?,?,?)"
            " ON CONFLICT(tg_id, kind, key) DO NOTHING", (tg_id, kind, key, now_iso()))

    def cleanup_sent(self, days=30):
        threshold = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        self.execute("DELETE FROM sent WHERE date(created_at) < ?", (threshold,))

    # ------------------------------------------------------------- статистика

    def stats(self):
        return {
            "users": self.count_users(),
            "groups": self.groups_count(),
            "groups_in_use": len(self.groups_in_use()),
            "timetable_weeks": (self.query_one(
                "SELECT COUNT(*) AS n FROM timetable") or {}).get("n", 0),
            "homework": (self.query_one(
                "SELECT COUNT(*) AS n FROM homework WHERE status='open'") or {}).get("n", 0),
            "changes": (self.query_one("SELECT COUNT(*) AS n FROM changes") or {}).get("n", 0),
        }
