# -*- coding: utf-8 -*-
"""Ядро планировщика «Планёрка» для встроенного режима.

Тот же код, что в отдельном приложении study-planner, но таблицы живут в базе
бота (с префиксом planner_), поэтому попадают в закреплённую копию базы и
восстанавливаются вместе с ней после перезапуска сервиса.
"""

import json
import os
import ssl
import threading
import urllib.request
from datetime import date, datetime, timedelta

from .assistant import Assistant
from .planning import WEEKDAYS, Planner
from .schedule_feed import ScheduleFeed, format_hhmm
from .storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "bot.db")
SCHEDULE_BOT_PATH = ROOT


class App:
    """Собирает хранилище, расписание, планировщик и ассистента."""

    #: Где лежат списки групп: (университет, подпись, база бота).
    GROUP_SOURCES = (
        ("unifirst", "Поволжский ГУФКСиТ", ("bot.db",)),
        ("kgasu", "КГАСУ", ("kgasu.db",)),
    )

    def __init__(self, db_path=DB_PATH, schedule_bot_path=SCHEDULE_BOT_PATH,
                 groups=None):
        self.store = Store(db_path)
        raw = self.store.settings()
        groups = groups or [part.strip() for part in
                            str(raw.get("schedule_groups") or "").split(",") if part.strip()]
        group_codes = []
        for entry in groups:
            if ":" in entry:
                group_codes.append(entry.split(":", 1)[1].strip())
            else:
                group_codes.append(entry)
        databases = []
        # Сначала свои копии (их обновляет tools/refresh_schedule.py из облака),
        # затем кэш локально запущенных ботов.
        for name in ("bot.db", "kgasu.db"):
            databases.append(os.path.join(DATA_DIR, name))
        if schedule_bot_path:
            databases.append(os.path.join(schedule_bot_path, "data", "bot.db"))
            databases.append(os.path.join(schedule_bot_path, "data", "kgasu.db"))
        self.feed = ScheduleFeed(database_paths=databases, groups=group_codes,
                                 schedule_bot_path=schedule_bot_path)
        self.planner = Planner(self.store, self.feed)
        self.assistant = Assistant(self.store, self.planner, feed=self.feed)
        self.started = datetime.now()
        self.reminder_thread = None
        self.stop_event = threading.Event()
        self.schedule_bot_path = schedule_bot_path

    # ------------------------------------------------------------- сервис

    def start_background(self):
        self.reminder_thread = threading.Thread(target=self._reminder_loop,
                                                name="reminders", daemon=True)
        self.reminder_thread.start()

    def stop(self):
        self.stop_event.set()

    def _reminder_loop(self):
        """Раз в минуту проверяет: не пора ли отправить план дня в Telegram."""
        while not self.stop_event.is_set():
            try:
                self._maybe_send_reminder()
            except Exception as error:
                print("Напоминание: %s" % error)
            self.stop_event.wait(55)

    def _maybe_send_reminder(self):
        raw = self.store.settings()
        chat_id = str(raw.get("telegram_chat_id") or "0").strip()
        if not chat_id.isdigit() or chat_id == "0":
            return
        tz = int(raw.get("tz_offset") or 3)
        now = datetime.utcnow() + timedelta(hours=tz)
        target = (raw.get("reminder_time") or "20:30").strip()
        if now.strftime("%H:%M") != target:
            return
        today_iso = now.date().isoformat()
        if raw.get("reminder_sent") == today_iso:
            return
        plan = self.planner.build_plan(days=1)
        day = next((item for item in plan["days"] if item["is_today"]), None)
        text = self._telegram_text(day)
        if text and send_telegram(chat_id, text):
            self.store.set_setting("reminder_sent", today_iso)

    @staticmethod
    def _telegram_text(day):
        if not day:
            return ""
        lines = ["📅 <b>План на %s</b>" % day["date"],
                 "Свободно: %d мин" % day["free_minutes"]]
        if day["pairs"]:
            lines.append("Пар: %d" % len(day["pairs"]))
        for block in day["blocks"][:8]:
            mark = "✅" if block["done"] else "▫️"
            lines.append("%s %s–%s %s (%d мин)"
                         % (mark, block["start"], block["end"],
                            block["task_title"], block["minutes"]))
        if not day["blocks"]:
            lines.append("Запланированных шагов нет.")
        return "\n".join(lines)

    # ------------------------------------------------------------- группы

    def _group_databases(self):
        """Базы, где лежат списки групп: (университет, подпись, путь)."""
        result = []
        for university, title, names in self.GROUP_SOURCES:
            for name in names:
                for directory in (DATA_DIR, os.path.join(self.schedule_bot_path or "",
                                                         "data")):
                    path = os.path.join(directory, name)
                    if directory and os.path.exists(path):
                        result.append((university, title, path))
                        break
        return result

    @staticmethod
    def _read_group_names(path):
        import sqlite3
        try:
            connection = sqlite3.connect(
                "file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
            try:
                rows = connection.execute("SELECT name FROM groups ORDER BY name").fetchall()
            finally:
                connection.close()
        except Exception:
            return []
        return [str(row[0]) for row in rows if row and row[0]]

    def selected_groups(self):
        """Выбранные группы: [{'university', 'code'}] из настройки schedule_groups."""
        raw = str(self.store.settings().get("schedule_groups") or "")
        result = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                university, code = entry.split(":", 1)
            else:
                university, code = "unifirst", entry
            result.append({"university": university.strip(), "code": code.strip()})
        return result

    def available_groups(self):
        """Списки групп по университетам — для выбора в интерфейсе."""
        catalog = {}
        for university, title, path in self._group_databases():
            names = self._read_group_names(path)
            if not names:
                continue
            bucket = catalog.setdefault(university, {"university": university,
                                                     "title": title, "groups": []})
            for name in names:
                if name not in bucket["groups"]:
                    bucket["groups"].append(name)
        return {"universities": list(catalog.values()),
                "selected": self.selected_groups()}

    def set_groups(self, entries):
        """Сохраняет выбранные группы и перезагружает расписание."""
        cleaned = []
        for entry in entries or []:
            if isinstance(entry, str):
                if ":" in entry:
                    university, code = entry.split(":", 1)
                else:
                    university, code = "unifirst", entry
            else:
                university = str(entry.get("university") or "unifirst")
                code = str(entry.get("code") or "")
            code = code.strip()
            if not code:
                continue
            item = {"university": university.strip(), "code": code}
            if item not in cleaned:
                cleaned.append(item)
        value = ",".join("%s:%s" % (item["university"], item["code"])
                         for item in cleaned)
        self.store.set_setting("schedule_groups", value)
        self.feed.set_groups([item["code"] for item in cleaned])
        self.feed.refresh(days=21)
        return {"ok": True, "selected": cleaned,
                "pairs_today": self._today_pairs_summary()}

    def _today_pairs_summary(self):
        today = date.today().isoformat()
        pairs = self.feed.pairs(days=1).get(today, [])
        return [{"start": format_hhmm(pair["start"]), "subject": pair["subject"],
                 "group": pair["group"]} for pair in pairs]

    # ------------------------------------------------------------ данные

    def state(self):
        counts = self.store.counts()
        settings = self.planner.settings()
        plan = self.planner.build_plan(days=settings["horizon_days"])
        today = next((item for item in plan["days"] if item["is_today"]), None)
        return {
            "counts": counts,
            "today": today,
            "warnings": plan["warnings"],
            "subjects": self.feed.subjects()[:40],
            "settings": {key: value for key, value in settings["raw"].items()},
            "server_time": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "schedule_groups": settings["raw"].get("schedule_groups", ""),
            "groups": self.selected_groups(),
        }

    def tasks_view(self):
        tasks = []
        for task in self.store.tasks(include_done=True):
            steps = self.store.subtasks(task["id"])
            done_steps = sum(1 for step in steps if step["done"])
            minutes_total = sum(int(step["estimate_minutes"] or 0) for step in steps)
            minutes_done = sum(int(step["estimate_minutes"] or 0)
                               for step in steps if step["done"])
            deadline = task.get("deadline") or ""
            overdue = False
            if deadline and not task["done"]:
                try:
                    moment = datetime.strptime(deadline[:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        moment = datetime.strptime(deadline[:10], "%Y-%m-%d")
                        moment = moment.replace(hour=23, minute=59)
                    except ValueError:
                        moment = None
                if moment and moment < datetime.now():
                    overdue = True
            tasks.append({
                "id": task["id"], "title": task["title"],
                "subject": task.get("subject") or "",
                "kind": task.get("kind") or "другое",
                "deadline": deadline, "priority": task.get("priority") or 2,
                "done": bool(task["done"]), "overdue": overdue,
                "steps": [{"id": step["id"], "title": step["title"],
                           "minutes": int(step["estimate_minutes"] or 0),
                           "planned_date": step.get("planned_date") or "",
                           "planned_start": step.get("planned_start") or "",
                           "planned_end": step.get("planned_end") or "",
                           "done": bool(step["done"])} for step in steps],
                "steps_done": done_steps, "steps_total": len(steps),
                "minutes_total": minutes_total, "minutes_done": minutes_done,
                "progress": int(round(100.0 * done_steps / len(steps))) if steps else 0,
            })
        return tasks

    def stats_view(self):
        """Данные для недельной и месячной диаграмм."""
        settings = self.planner.settings()
        tz = int(settings["raw"].get("tz_offset") or 3)
        today = date.today()
        planned_rows = self.store.all_subtasks(only_planned=True)
        planned_by_day = {}
        for step in planned_rows:
            day_iso = step.get("planned_date") or ""
            if day_iso:
                planned_by_day[day_iso] = planned_by_day.get(day_iso, 0) + \
                    int(step.get("estimate_minutes") or 0)
        sessions = self.store.query(
            "SELECT * FROM planner_sessions ORDER BY started_at")
        done_by_day = {}
        for session in sessions:
            started = str(session.get("started_at") or "")[:10]
            if started:
                done_by_day[started] = done_by_day.get(started, 0) + \
                    int(session.get("minutes") or 0)

        def day_series(days):
            series = []
            for offset in range(days - 1, -1, -1):
                day = today - timedelta(days=offset)
                iso = day.isoformat()
                series.append({"date": iso,
                               "weekday": WEEKDAYS[day.weekday()],
                               "planned": planned_by_day.get(iso, 0),
                               "done": done_by_day.get(iso, 0)})
            return series

        # Предметы: план и факт по задачам и сессиям.
        by_subject = {}
        for step in planned_rows:
            subject = (step.get("subject") or "без предмета").strip()
            by_subject.setdefault(subject, {"planned": 0, "done": 0})
            by_subject[subject]["planned"] += int(step.get("estimate_minutes") or 0)
        task_subjects = {task["id"]: (task.get("subject") or "без предмета").strip()
                         for task in self.store.tasks(include_done=True)}
        for session in sessions:
            subject = task_subjects.get(session.get("task_id")) or "без предмета"
            by_subject.setdefault(subject, {"planned": 0, "done": 0})
            by_subject[subject]["done"] += int(session.get("minutes") or 0)

        # Серия дней с занятиями подряд (мотивация).
        streak = 0
        cursor = today
        while True:
            iso = cursor.isoformat()
            if done_by_day.get(iso, 0) > 0:
                streak += 1
                cursor -= timedelta(days=1)
            elif iso == today.isoformat():
                cursor -= timedelta(days=1)          # сегодня ещё можно успеть
            else:
                break

        return {
            "week": day_series(7),
            "month": day_series(30),
            "subjects": [{"subject": subject, "planned": data["planned"],
                          "done": data["done"]}
                         for subject, data in sorted(by_subject.items(),
                                                     key=lambda item: -item[1]["planned"])][:8],
            "totals": {"planned_today": planned_by_day.get(today.isoformat(), 0),
                       "done_today": done_by_day.get(today.isoformat(), 0),
                       "done_total": sum(done_by_day.values()),
                       "streak": streak,
                       "tz_offset": tz},
        }

    def ics_view(self, days=14):
        """Экспорт плана в календарь (.ics): можно подписаться в телефоне."""
        plan = self.planner.build_plan(days=days)
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
                 "PRODID:-//Planyorka//Study Planner//RU", "CALSCALE:GREGORIAN"]
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        for day in plan["days"]:
            for block in day["blocks"]:
                try:
                    start = datetime.strptime("%s %s" % (day["date"], block["start"]),
                                              "%Y-%m-%d %H:%M")
                    end = datetime.strptime("%s %s" % (day["date"], block["end"]),
                                            "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                lines.extend([
                    "BEGIN:VEVENT",
                    "UID:planner-%s-%s@planyorka" % (day["date"], block["id"]),
                    "DTSTAMP:%s" % stamp,
                    "DTSTART;TZID=Europe/Moscow:%s" % start.strftime("%Y%m%dT%H%M%S"),
                    "DTEND;TZID=Europe/Moscow:%s" % end.strftime("%Y%m%dT%H%M%S"),
                    "SUMMARY:%s" % _ics_escape(block["task_title"]),
                    "DESCRIPTION:%s" % _ics_escape(block["title"]),
                    "END:VEVENT"])
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


def _ics_escape(text):
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\n", "\\n")


# ------------------------------------------------------------------ Telegram

def send_telegram(chat_id, text):
    """Отправляет сообщение через бота расписания.

    В облаке токен приходит переменной окружения BOT_TOKEN; при локальном запуске
    можно положить токен в файл token.txt рядом с проектом.
    """
    token = (os.environ.get("PLANNER_TELEGRAM_TOKEN")
             or os.environ.get("BOT_TOKEN") or "")
    if not token:
        candidate = os.path.join(ROOT, "token.txt")
        if os.path.exists(candidate):
            token = open(candidate, encoding="utf-8").read().strip()
    if not token:
        return False
    payload = json.dumps({"chat_id": int(chat_id), "text": text[:3900],
                          "parse_mode": "HTML"}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "planner"})
    try:
        with urllib.request.urlopen(request, timeout=30,
                                    context=ssl._create_unverified_context()) as response:
            return response.status == 200
    except Exception as error:
        print("Telegram: %s" % error)
        return False


# -------------------------------------------------------------------- сервер

