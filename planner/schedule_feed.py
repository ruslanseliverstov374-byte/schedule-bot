# -*- coding: utf-8 -*-
"""Расписание занятий для планировщика.

Источник — базы уже работающих ботов расписания (таблица ``timetable``): там
лежит разобранное расписание по неделям, его не нужно запрашивать заново.
Если неделя ещё не закэширована, для ПГУФКСиТ можно сходить в публичный API
(модуль берётся из соседнего проекта schedule-bot, если он рядом).

Наружу отдаётся простой вид: {дата: [{start, end, subject, para, group}, ...]}.
"""

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))


def _parse_hhmm(value):
    """'8:30' / '08:30' / '08:30:00' -> минуты от полуночи (или None)."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def format_hhmm(total_minutes):
    total_minutes = int(total_minutes)
    return "%02d:%02d" % (total_minutes // 60, total_minutes % 60)


class ScheduleFeed:
    """Расписание групп из баз ботов (и из API как запасной путь)."""

    def __init__(self, database_paths=None, groups=None, cache_minutes=20,
                 schedule_bot_path=None, logger=print):
        self.database_paths = [path for path in (database_paths or []) if path]
        self.groups = [group.strip() for group in (groups or []) if group.strip()]
        self.cache_minutes = int(cache_minutes)
        self.schedule_bot_path = schedule_bot_path or os.path.join(
            os.path.dirname(ROOT), "schedule-bot")
        self.logger = logger if callable(logger) else (lambda message: None)
        self._cache = {}
        self._cache_at = None
        self._api = None

    # ------------------------------------------------------------------ сбор

    def _read_databases(self):
        """Читает закэшированные недели из баз ботов: date -> пары."""
        by_date = {}
        for path in self.database_paths:
            if not os.path.exists(path):
                continue
            try:
                connection = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                                             uri=True, timeout=5)
                connection.row_factory = sqlite3.Row
                try:
                    rows = connection.execute(
                        "SELECT group_title, lessons FROM timetable").fetchall()
                finally:
                    connection.close()
            except sqlite3.Error as error:
                self.logger("Расписание: база %s не читается (%s)" % (path, error))
                continue
            for row in rows:
                group = str(row["group_title"] or "")
                if self.groups and group not in self.groups:
                    continue
                try:
                    lessons = json.loads(row["lessons"] or "[]")
                except ValueError:
                    continue
                for lesson in lessons:
                    day = str(lesson.get("date") or "")
                    start = _parse_hhmm(lesson.get("start"))
                    end = _parse_hhmm(lesson.get("end"))
                    if not day or start is None or end is None:
                        continue
                    item = {"start": start, "end": end,
                            "subject": str(lesson.get("subject") or ""),
                            "type": str(lesson.get("type") or ""),
                            "para": lesson.get("para"),
                            "group": group}
                    bucket = by_date.setdefault(day, [])
                    key = (start, end, item["subject"], group)
                    if not any((existing["start"], existing["end"],
                                existing["subject"], existing["group"]) == key
                               for existing in bucket):
                        bucket.append(item)
        for items in by_date.values():
            items.sort(key=lambda entry: (entry["start"], entry["end"]))
        return by_date

    def _fetch_from_api(self, target_dates):
        """Добирает недостающие недели через API ПГУФКСиТ (если проект рядом)."""
        if not self.schedule_bot_path or not os.path.isdir(self.schedule_bot_path):
            return {}
        if self.schedule_bot_path not in sys.path:
            sys.path.insert(0, self.schedule_bot_path)
        try:
            from schedule import unifirst      # из проекта schedule-bot
        except Exception as error:
            self.logger("Расписание: API недоступен (%s)" % error)
            return {}
        if self._api is None:
            self._api = unifirst.Unifirst()
        result = {}
        weeks = set()
        for day in target_dates:
            year, week = unifirst.iso_year_week(day)
            weeks.add((year, week))
        for group in self.groups:
            for year, week in sorted(weeks):
                try:
                    payload = self._api.timetable(group, year, week)
                    lessons = unifirst.normalize(payload)
                except Exception as error:
                    self.logger("Расписание: %s %s/%s — %s"
                                % (group, year, week, str(error)[:80]))
                    continue
                for lesson in lessons:
                    start = _parse_hhmm(lesson.get("start"))
                    end = _parse_hhmm(lesson.get("end"))
                    day = str(lesson.get("date") or "")
                    if not day or start is None or end is None:
                        continue
                    result.setdefault(day, []).append({
                        "start": start, "end": end,
                        "subject": str(lesson.get("subject") or ""),
                        "type": str(lesson.get("type") or ""),
                        "para": lesson.get("para"), "group": group})
        return result

    # ------------------------------------------------------------------ API

    def refresh(self, start=None, days=21):
        """Обновляет кэш расписания на указанный диапазон дней."""
        start = start or date.today()
        target_dates = [start + timedelta(days=offset) for offset in range(days)]
        by_date = self._read_databases()
        missing = [day for day in target_dates if day.isoformat() not in by_date]
        if missing:
            fresh = self._fetch_from_api(missing)
            for day, items in fresh.items():
                if self.groups:
                    items = [item for item in items if item["group"] in self.groups]
                by_date.setdefault(day, []).extend(items)
            for items in by_date.values():
                items.sort(key=lambda entry: (entry["start"], entry["end"]))
        self._cache = by_date
        self._cache_at = datetime.now()
        return by_date

    def _ensure_cache(self, start=None, days=21):
        if self._cache_at is None or \
                (datetime.now() - self._cache_at).total_seconds() > self.cache_minutes * 60:
            self.refresh(start=start, days=days)

    def set_groups(self, groups):
        """Меняет набор групп и сбрасывает кэш — после выбора группы в интерфейсе."""
        self.groups = [str(group).strip() for group in (groups or []) if str(group).strip()]
        self._cache = {}
        self._cache_at = None
        return self.groups

    def pairs(self, start=None, days=21):
        """{дата ISO: [пары]} на диапазон дней."""
        start = start or date.today()
        self._ensure_cache(start=start, days=days)
        result = {}
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            result[day] = [dict(item) for item in self._cache.get(day, [])]
        return result

    def subjects(self):
        """Названия предметов из расписания — для подсказок и разбора задач."""
        self._ensure_cache()
        names = set()
        for items in self._cache.values():
            for item in items:
                subject = (item.get("subject") or "").strip()
                if len(subject) > 2:
                    names.add(subject)
        return sorted(names)

    def day_pairs(self, day):
        return self.pairs(start=day, days=1).get(day.isoformat(), [])
