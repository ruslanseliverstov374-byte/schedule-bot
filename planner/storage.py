# -*- coding: utf-8 -*-
"""Хранилище планировщика: SQLite, только стандартная библиотека.

Таблицы:
    tasks      — задачи («переписать конспект», «сдать реферат»);
    subtasks   — шаги задач с оценкой времени и запланированным слотом;
    sessions   — фактические занятия (фокус-сессии), для диаграмм «план/факт»;
    settings   — настройки в формате ключ-значение.

Все времена в базе — ISO-строки: дата 'ГГГГ-ММ-ДД', время 'ЧЧ:ММ'.
"""

import os
import sqlite3
import threading
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    task_id INTEGER DEFAULT 0,
    subtask_id INTEGER DEFAULT 0,
    minutes INTEGER DEFAULT 0,
    started_at TEXT DEFAULT '',
    ended_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS planner_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_planner_subtasks_date ON planner_subtasks(planned_date);
CREATE INDEX IF NOT EXISTS idx_planner_tasks_done ON planner_tasks(done);
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
    "schedule_groups": "unifirst:26282,kgasu:26ЗК01",
    # Дни без учёбы: 6=воскресенье (0 — понедельник).
    "days_off": "6",
    # Телеграм-напоминания (chat_id владельца, 0 — выключены).
    "telegram_chat_id": "0",
}


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
        self.connection.execute("PRAGMA foreign_keys=ON")
        with self.connection:
            self.connection.executescript(SCHEMA)
        self._seed_settings()

    # ------------------------------------------------------------- служебное

    def _seed_settings(self):
        with self._lock, self.connection:
            for key, value in DEFAULT_SETTINGS.items():
                self.connection.execute(
                    "INSERT OR IGNORE INTO planner_settings(key, value) VALUES(?, ?)", (key, value))

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

    # -------------------------------------------------------------- настройки

    def settings(self):
        rows = self.query("SELECT key, value FROM planner_settings")
        return {row["key"]: row["value"] for row in rows}

    def set_setting(self, key, value):
        self.execute("INSERT INTO planner_settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    # ----------------------------------------------------------------- задачи

    def add_task(self, title, subject="", kind="другое", deadline="", priority=2,
                 estimate_minutes=0, notes="", source=""):
        return self.execute(
            "INSERT INTO planner_tasks(title, subject, kind, deadline, priority,"
            " estimate_minutes, created_at, notes, source)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (title.strip(), subject.strip(), kind, deadline, int(priority),
             int(estimate_minutes), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             notes, source))

    def task(self, task_id):
        return self.query_one("SELECT * FROM planner_tasks WHERE id=?", (int(task_id),))

    def tasks(self, include_done=False):
        where = "" if include_done else " WHERE done=0"
        return self.query("SELECT * FROM planner_tasks%s ORDER BY done ASC,"
                          " CASE WHEN deadline='' THEN 1 ELSE 0 END, deadline ASC,"
                          " priority ASC, id ASC" % where)

    def update_task(self, task_id, **fields):
        allowed = {"title", "subject", "kind", "deadline", "priority",
                   "estimate_minutes", "done", "notes"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return
        assignments = ", ".join("%s=?" % key for key, _ in pairs)
        self.execute("UPDATE planner_tasks SET %s WHERE id=?" % assignments,
                     tuple(value for _, value in pairs) + (int(task_id),))

    def delete_task(self, task_id):
        self.execute("DELETE FROM planner_subtasks WHERE task_id=?", (int(task_id),))
        self.execute("DELETE FROM planner_tasks WHERE id=?", (int(task_id),))

    # ------------------------------------------------------------------- шаги

    def add_subtask(self, task_id, title, estimate_minutes=30, ord_value=0):
        return self.execute(
            "INSERT INTO planner_subtasks(task_id, title, estimate_minutes, ord)"
            " VALUES(?,?,?,?)",
            (int(task_id), title.strip(), int(estimate_minutes), int(ord_value)))

    def subtasks(self, task_id):
        return self.query("SELECT * FROM planner_subtasks WHERE task_id=? ORDER BY ord, id",
                          (int(task_id),))

    def all_subtasks(self, only_planned=False):
        where = " WHERE planned_date<>''" if only_planned else ""
        return self.query("SELECT s.*, t.title AS task_title, t.subject, t.kind,"
                          " t.deadline, t.priority, t.done AS task_done"
                          " FROM planner_subtasks s JOIN planner_tasks t ON t.id=s.task_id%s"
                          " ORDER BY s.planned_date, s.planned_start, s.ord" % where)

    def plan_subtask(self, subtask_id, planned_date, planned_start, planned_end):
        self.execute("UPDATE planner_subtasks SET planned_date=?, planned_start=?, planned_end=?"
                     " WHERE id=?",
                     (planned_date, planned_start, planned_end, int(subtask_id)))

    def clear_plan(self, task_id=None):
        if task_id is None:
            self.execute("UPDATE planner_subtasks SET planned_date='', planned_start='',"
                         " planned_end=''")
        else:
            self.execute("UPDATE planner_subtasks SET planned_date='', planned_start='',"
                         " planned_end='' WHERE task_id=?", (int(task_id),))

    def set_subtask_done(self, subtask_id, done=True):
        self.execute("UPDATE planner_subtasks SET done=? WHERE id=?",
                     (1 if done else 0, int(subtask_id),))

    def delete_subtasks(self, task_id):
        self.execute("DELETE FROM planner_subtasks WHERE task_id=?", (int(task_id),))

    # -------------------------------------------------------------- сессии

    def add_session(self, task_id, subtask_id, minutes, started_at="", ended_at=""):
        return self.execute(
            "INSERT INTO planner_sessions(task_id, subtask_id, minutes, started_at, ended_at)"
            " VALUES(?,?,?,?,?)",
            (int(task_id or 0), int(subtask_id or 0), int(minutes), started_at, ended_at))

    def sessions_between(self, start_iso, end_iso):
        return self.query(
            "SELECT * FROM planner_sessions WHERE started_at >= ? AND started_at < ?",
            (start_iso, end_iso))

    def sessions_total(self):
        row = self.query_one("SELECT COALESCE(SUM(minutes),0) AS total FROM planner_sessions")
        return int((row or {}).get("total") or 0)

    # --------------------------------------------------------------- сводка

    def counts(self):
        """Сколько задач и шагов в каком состоянии — для шапки интерфейса."""
        tasks = self.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN done=0 THEN 1 ELSE 0 END) AS open,"
            " SUM(CASE WHEN done=1 THEN 1 ELSE 0 END) AS closed FROM planner_tasks") or {}
        subtasks = self.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN done=0 THEN 1 ELSE 0 END) AS open,"
            " SUM(CASE WHEN done=1 THEN 1 ELSE 0 END) AS closed FROM planner_subtasks") or {}
        today = date.today().isoformat()
        planned_today = self.query_one(
            "SELECT COALESCE(SUM(estimate_minutes),0) AS minutes, COUNT(*) AS steps"
            " FROM planner_subtasks WHERE planned_date=? AND done=0", (today,)) or {}
        return {
            "tasks_total": int(tasks.get("total") or 0),
            "tasks_open": int(tasks.get("open") or 0),
            "tasks_done": int(tasks.get("closed") or 0),
            "subtasks_total": int(subtasks.get("total") or 0),
            "subtasks_done": int(subtasks.get("closed") or 0),
            "today_minutes": int(planned_today.get("minutes") or 0),
            "today_steps": int(planned_today.get("steps") or 0),
            "sessions_total_minutes": self.sessions_total(),
        }
