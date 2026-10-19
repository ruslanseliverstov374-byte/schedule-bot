# -*- coding: utf-8 -*-
"""Движок планирования: свободные окна вокруг пар и раскладка шагов по времени.

Логика простая и объяснимая:
  1. Берём окна, в которые студент вообще готов учиться (например 09:00–13:00
     и 14:30–22:00).
  2. Вычитаем пары из расписания (с запасом 15 минут до и после — дойти, отдохнуть).
  3. Вычитаем уже запланированные шаги других задач.
  4. Ограничиваем день лимитом часов и «тяжестью» дня: после пяти пар на учёбу
     остаётся меньше времени, иначе план нереалистичен.
  5. Раскладываем шаги задач по возрастанию дедлайна и приоритета.
Если до дедлайна времени не хватает — честно показываем дефицит, а не делаем
вид, что всё успеть можно.
"""

from datetime import date, datetime, timedelta
import math

from .schedule_feed import format_hhmm, _parse_hhmm

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
PAIR_BUFFER = 15          # минут до и после пары: дорога и переключение
DEFAULT_WINDOWS = [(9 * 60, 13 * 60), (14 * 60 + 30, 22 * 60)]


def parse_windows(text):
    """'09:00-13:00,14:30-22:00' -> [(540, 780), (870, 1320)]."""
    windows = []
    for chunk in str(text or "").split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        left, right = chunk.split("-", 1)
        start, end = _parse_hhmm(left), _parse_hhmm(right)
        if start is None or end is None or end <= start:
            continue
        windows.append((start, end))
    return sorted(windows) or list(DEFAULT_WINDOWS)


def subtract(intervals, busy):
    """Вычитает из интервалов занятые куски и возвращает свободные."""
    result = []
    for start, end in intervals:
        pieces = [(start, end)]
        for busy_start, busy_end in busy:
            fresh = []
            for piece_start, piece_end in pieces:
                if busy_end <= piece_start or busy_start >= piece_end:
                    fresh.append((piece_start, piece_end))
                    continue
                if busy_start > piece_start:
                    fresh.append((piece_start, busy_start))
                if busy_end < piece_end:
                    fresh.append((busy_end, piece_end))
            pieces = fresh
        result.extend(piece for piece in pieces if piece[1] > piece[0])
    return sorted(result)


def split_chunks(total, min_block, max_block):
    """Делит оценку на блоки не длиннее max_block (по возможности равные).

    200 минут при максимуме 90 — это три блока по ~67, а не 90+110: блок
    длиннее максимума бессмыслен, а «хвост» меньше минимума приклеивать к
    предыдущему блоку рискованно (получится переработка).
    """
    total = max(int(total or 0), 0)
    if not total:
        return []
    if total <= max_block:
        return [total]
    count = int(math.ceil(total / float(max_block)))
    base = int(math.ceil(total / float(count)))
    chunks = [base] * (count - 1)
    chunks.append(total - base * (count - 1))
    return [chunk for chunk in chunks if chunk > 0]


class Planner:
    """Считает свободное время и раскладывает задачи по дням."""

    def __init__(self, store, feed, today=None, account_id=1):
        self.store = store
        self.feed = feed
        self._today = today
        self.account_id = int(account_id or 1)

    def today(self):
        return self._today or date.today()

    # --------------------------------------------------------------- настройки

    def settings(self):
        raw = self.store.settings(self.account_id)
        settings = {
            "windows": parse_windows(raw.get("study_windows")),
            "max_daily": int(raw.get("max_daily_minutes") or 300),
            "min_block": int(raw.get("min_block") or 25),
            "max_block": int(raw.get("max_block") or 90),
            "break_minutes": int(raw.get("break_minutes") or 10),
            "safety_days": int(raw.get("safety_days") or 1),
            "days_off": {int(part) for part in str(raw.get("days_off") or "").split(",")
                         if part.strip().isdigit()},
            "horizon_days": int(raw.get("horizon_days") or 14),
            "raw": raw,
        }
        return settings

    # --------------------------------------------------------------- занятость

    def busy_by_date(self, days=21, include_planned=True):
        """Занятость по дням: пары с буфером + уже запланированные шаги."""
        today = self.today()
        pairs = self.feed.pairs(start=today, days=days)
        busy = {}
        for day_iso, items in pairs.items():
            intervals = []
            for item in items:
                start = max(0, item["start"] - PAIR_BUFFER)
                end = item["end"] + PAIR_BUFFER
                intervals.append((start, end))
            busy[day_iso] = intervals
        if include_planned:
            for step in self.store.all_subtasks(self.account_id, only_planned=True):
                day_iso = step.get("planned_date") or ""
                if not day_iso:
                    continue
                start = _parse_hhmm(step.get("planned_start"))
                end = _parse_hhmm(step.get("planned_end"))
                if start is None or end is None:
                    continue
                busy.setdefault(day_iso, []).append((start, end))
        return busy

    def day_capacity(self, day, settings=None, busy=None):
        """Сколько можно учиться в этот день и в какие окна."""
        settings = settings or self.settings()
        busy = busy if busy is not None else self.busy_by_date()
        day_iso = day.isoformat()
        pairs = self._pairs_count(day_iso)
        if day.weekday() in settings["days_off"]:
            return {"date": day_iso, "windows": [], "free_minutes": 0,
                    "pairs": pairs, "reason": "выходной"}
        windows = subtract(settings["windows"], busy.get(day_iso, []))
        # Тяжёлый день: после пяти пар на учёбу остаётся мало сил.
        cap = settings["max_daily"]
        if pairs >= 5:
            cap = min(cap, 90)
        elif pairs == 4:
            cap = min(cap, 150)
        elif pairs == 3:
            cap = min(cap, 180)
        free = []
        total = 0
        for start, end in windows:
            length = end - start
            if length < settings["min_block"]:
                continue
            allowed = min(length, cap - total)
            if allowed < settings["min_block"]:
                continue
            free.append({"start": start, "end": start + allowed,
                         "minutes": allowed})
            total += allowed
        return {"date": day_iso, "windows": free, "free_minutes": total,
                "pairs": pairs, "reason": ""}

    def _pairs_count(self, day_iso):
        pairs = getattr(self, "_pairs_cache", None)
        if pairs is None:
            pairs = self.feed.pairs(start=self.today(), days=28)
            self._pairs_cache = pairs
        return len(pairs.get(day_iso, []))

    def availability(self, days=None):
        """Свободные окна по дням — для календаря и диаграмм."""
        settings = self.settings()
        days = days or settings["horizon_days"]
        today = self.today()
        busy = self.busy_by_date(days=days + 7)
        self._pairs_cache = None
        result = []
        for offset in range(days):
            day = today + timedelta(days=offset)
            info = self.day_capacity(day, settings=settings, busy=busy)
            info["weekday"] = WEEKDAYS[day.weekday()]
            result.append(info)
        return result

    # ------------------------------------------------------------- раскладка

    def _deadline_limit(self, task, settings):
        """Последний день, когда шаг должен быть готов (с запасом)."""
        deadline = self._parse_deadline(task.get("deadline"))
        if deadline is None:
            return None, None
        limit = deadline - timedelta(days=settings["safety_days"])
        return deadline, limit.date() if hasattr(limit, "date") else limit

    @staticmethod
    def _parse_deadline(value):
        text = str(value or "").strip()
        if not text:
            return None
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None

    def _split_long_steps(self, settings):
        """Режет длинные шаги на блоки: одна запись — один слот в календаре."""
        for task in self.store.tasks(self.account_id):
            steps = self.store.subtasks(task["id"], self.account_id)
            for step in steps:
                estimate = int(step["estimate_minutes"] or 0)
                if estimate <= settings["max_block"]:
                    continue
                if "(1/" in str(step["title"]):
                    continue
                chunks = split_chunks(estimate, settings["min_block"],
                                      settings["max_block"])
                if len(chunks) < 2:
                    continue
                total = len(chunks)
                self.store.execute(
                    "UPDATE subtasks SET title=?, estimate_minutes=? WHERE id=?",
                    ("%s (1/%d)" % (step["title"], total), chunks[0], step["id"]))
                for index, chunk in enumerate(chunks[1:], start=2):
                    self.store.add_subtask(task["id"],
                                           "%s (%d/%d)" % (step["title"], index, total),
                                           chunk, ord_value=int(step["ord"]) + index - 1,
                                           account_id=self.account_id)

    def plan(self, horizon_days=None, persist=True):
        """Раскладывает все открытые задачи по свободным окнам.

        Возвращает план и честную оценку рисков по каждому дедлайну.
        """
        settings = self.settings()
        self._split_long_steps(settings)
        if persist:
            self.store.clear_plan(self.account_id)
        days = horizon_days or settings["horizon_days"]
        availability = self.availability(days)
        # Раскладка идёт по изменяемым окнам: занятый блок уменьшает окно дня.
        windows_by_date = {
            item["date"]: [dict(window) for window in item["windows"]]
            for item in availability
        }
        capacity_by_date = {item["date"]: item["free_minutes"] for item in availability}

        tasks = [task for task in self.store.tasks(self.account_id)
                 if not task["done"]]
        enriched = []
        for task in tasks:
            steps = [step for step in self.store.subtasks(task["id"],
                                                        self.account_id)
                     if not step["done"]]
            needed = sum(int(step["estimate_minutes"] or 0) for step in steps)
            deadline, limit_date = self._deadline_limit(task, settings)
            capacity_before = 0
            for day_iso, minutes in capacity_by_date.items():
                if limit_date is None or day_iso <= limit_date.isoformat():
                    capacity_before += minutes
            enriched.append({"task": task, "steps": steps, "needed": needed,
                             "deadline": deadline, "limit": limit_date,
                             "capacity": capacity_before,
                             "deficit": max(0, needed - capacity_before)})
        # Сначала то, что горит: дедлайн, потом приоритет, потом оценка.
        enriched.sort(key=lambda entry: (
            entry["limit"] or date(2099, 1, 1),
            int(entry["task"].get("priority") or 2),
            int(entry["task"]["id"])))

        planned_blocks = 0
        for entry in enriched:
            for step in entry["steps"]:
                minutes = int(step["estimate_minutes"] or settings["min_block"])
                slot = self._find_slot(windows_by_date, minutes, entry["limit"],
                                       settings)
                if slot is None:
                    continue
                day_iso, start, end = slot
                if persist:
                    self.store.plan_subtask(step["id"], day_iso,
                                            format_hhmm(start), format_hhmm(end),
                                            account_id=self.account_id)
                planned_blocks += 1

        result = self.build_plan(days=days, enrich=enriched,
                                 planned_blocks=planned_blocks)
        return result

    def _find_slot(self, windows_by_date, minutes, limit_date, settings):
        """Первое свободное окно, куда влезает блок, не позже ограничения."""
        for day_iso in sorted(windows_by_date):
            if limit_date is not None and day_iso > limit_date.isoformat():
                break
            for window in windows_by_date[day_iso]:
                if window["minutes"] < minutes:
                    continue
                start = window["start"]
                end = start + minutes
                window["start"] = end + settings["break_minutes"]
                window["minutes"] = window["end"] - window["start"]
                windows_by_date[day_iso] = [
                    item for item in windows_by_date[day_iso] if item["minutes"] > 0]
                return day_iso, start, end
        return None

    def build_plan(self, days=None, enrich=None, planned_blocks=0):
        """План для интерфейса: дни, блоки, риски, предупреждения."""
        settings = self.settings()
        days = days or settings["horizon_days"]
        availability = self.availability(days)
        planned = self.store.all_subtasks(self.account_id, only_planned=True)
        by_date = {}
        for step in planned:
            by_date.setdefault(step["planned_date"], []).append({
                "id": step["id"], "task_id": step["task_id"],
                "title": step["title"], "task_title": step["task_title"],
                "subject": step.get("subject") or "",
                "start": step.get("planned_start"), "end": step.get("planned_end"),
                "minutes": int(step.get("estimate_minutes") or 0),
                "done": bool(step.get("done")),
            })
        for items in by_date.values():
            items.sort(key=lambda entry: entry.get("start") or "")
        day_list = []
        today_iso = self.today().isoformat()
        for item in availability:
            blocks = by_date.get(item["date"], [])
            planned_minutes = sum(block["minutes"] for block in blocks)
            day_list.append({
                "date": item["date"],
                "weekday": item["weekday"],
                "pairs": self._pairs_for_ui(item["date"]),
                "free_minutes": item["free_minutes"],
                "planned_minutes": planned_minutes,
                "blocks": blocks,
                "is_today": item["date"] == today_iso,
                "reason": item.get("reason") or "",
            })
        tasks = []
        for entry in enrich or []:
            task = entry["task"]
            tasks.append({
                "id": task["id"], "title": task["title"],
                "subject": task.get("subject") or "",
                "deadline": task.get("deadline") or "",
                "priority": task.get("priority"),
                "needed_minutes": entry["needed"],
                "capacity_minutes": entry["capacity"],
                "deficit_minutes": entry["deficit"],
                "risk": entry["deficit"] > 0,
                "steps_total": len(entry["steps"]),
            })
        warnings = []
        risky = [task for task in tasks if task["risk"]]
        if risky:
            total_deficit = sum(task["deficit_minutes"] for task in risky)
            warnings.append(
                "Не хватает %d мин до дедлайнов: %s. Можно урезать объём, "
                "поднять дневной лимит или начать раньше."
                % (total_deficit, ", ".join(task["title"] for task in risky[:3])))
        overloaded = [day for day in day_list
                      if day["planned_minutes"] > settings["max_daily"]]
        if overloaded:
            warnings.append("Перегруз: %d дн. с планом больше %d мин"
                            % (len(overloaded), settings["max_daily"]))
        return {
            "days": day_list,
            "tasks": tasks,
            "warnings": warnings,
            "totals": {
                "planned_blocks": sum(len(day["blocks"]) for day in day_list)
                if not planned_blocks else planned_blocks,
                "planned_minutes": sum(day["planned_minutes"] for day in day_list),
                "today_minutes": sum(day["planned_minutes"] for day in day_list
                                     if day["is_today"]),
                "free_minutes": sum(day["free_minutes"] for day in day_list),
                "horizon_days": days,
            },
        }

    def _pairs_for_ui(self, day_iso):
        pairs = getattr(self, "_pairs_cache", None)
        if pairs is None:
            pairs = self.feed.pairs(start=self.today(), days=28)
            self._pairs_cache = pairs
        result = []
        for item in pairs.get(day_iso, []):
            result.append({"start": format_hhmm(item["start"]),
                           "end": format_hhmm(item["end"]),
                           "subject": item["subject"],
                           "type": item.get("type") or "",
                           "group": item.get("group") or ""})
        return result
