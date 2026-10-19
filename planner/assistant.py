# -*- coding: utf-8 -*-
"""«Ассистент»: понимает задачу из свободного текста и раскладывает её на шаги.

Работает без внешних сервисов и ключей: разбор текста регулярками и шаблонами
по типу работы. Если задать LLM_API_KEY в окружении, тот же интерфейс можно
подключить к модели — точка расширения помечена `_llm_reply`.
"""

import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta

from .planning import WEEKDAYS

WEEKDAY_WORDS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3, "пятница": 4, "пт": 4, "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}

#: Основы названий дней: «к пятнице», «до среды», «в понедельник».
WEEKDAY_STEMS = (
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
    ("пятниц", 4), ("суббот", 5), ("воскресен", 6),
)

#: Дательный падеж — для подписи дедлайна («к пятнице»).
DATIVE_WEEKDAYS = ("понедельнику", "вторнику", "среде", "четвергу", "пятнице",
                   "субботе", "воскресенью")

KIND_WORDS = (
    ("конспект", ("конспект", "переписать", "переписать конспект", "законспектировать",
                  "оформить конспект", "тетрад")),
    ("реферат", ("реферат", "доклад", "эссе", "сочинение")),
    ("презентация", ("презентац", "слайд", "защит")),
    ("лабораторная", ("лаборатор", "лаб", "практикум", "расчетн", "расчётн")),
    ("задачи", ("задач", "задачи", "домашк", "примеры", "упражнени")),
    ("чтение", ("прочитать", "читать", "прочти", "параграф", "главу", "глава")),
    ("подготовка", ("подготов", "выучить", "повторить", "экзамен", "зачёт", "зачет",
                    "билет", "тест", "коллоквиум")),
)

#: Шаблоны разложения: доли времени и подписи шагов.
TEMPLATES = {
    "конспект": (("прочитать и разметить материал", 35),
                 ("выписать главное по разделам", 30),
                 ("оформить конспект", 25),
                 ("вычитать и проверить", 10)),
    "реферат": (("собрать источники и выписать тезисы", 20),
                ("составить план работы", 10),
                ("написать черновик", 45),
                ("оформить и вычитать", 25)),
    "презентация": (("собрать материал и структуру", 25),
                    ("написать текст слайдов", 30),
                    ("оформить слайды", 35),
                    ("отрепетировать выступление", 10)),
    "лабораторная": (("изучить методичку и разобрать пример", 20),
                     ("выполнить расчёты/опыты", 45),
                     ("оформить отчёт", 25),
                     ("подготовиться к защите", 10)),
    "задачи": (("разобрать примеры", 15),
               ("решить первую половину", 35),
               ("решить вторую половину", 35),
               ("сверить ответы и исправить", 15)),
    "чтение": (("прочитать и выписать главное", 60),
               ("составить краткий пересказ", 25),
               ("повторить ключевые места", 15)),
    "подготовка": (("собрать материалы и план", 20),
                   ("проработать теорию", 40),
                   ("прорешать практику", 30),
                   ("прогнать себя по вопросам", 10)),
    "другое": (("разобраться в задаче и спланировать", 25),
               ("сделать основную часть", 50),
               ("проверить и оформить", 25)),
}

#: Сколько минут примерно уходит на одну единицу объёма.
MINUTES_PER_UNIT = {
    "страниц": 10, "страница": 10, "стр": 10,
    "глав": 45, "глава": 45, "главы": 45,
    "задач": 12, "задачи": 12, "задача": 12,
    "слайд": 8, "слайдов": 8,
    "билет": 25, "билетов": 25,
    "слов": 0.02, "тысяч слов": 20,
}


class Assistant:
    """Разбор сообщения и раскладка задачи на шаги."""

    def __init__(self, store, planner, feed=None, logger=print):
        self.store = store
        self.planner = planner
        self.feed = feed
        self.logger = logger if callable(logger) else (lambda message: None)

    # ------------------------------------------------------------- разбор

    def detect_kind(self, text):
        low = " " + text.lower() + " "
        best = ("другое", 0)
        for kind, words in KIND_WORDS:
            for word in words:
                if word in low:
                    score = len(word)
                    if score > best[1]:
                        best = (kind, score)
        return best[0]

    def detect_subject(self, text):
        """Ищет предмет из расписания (с учётом падежей) или после «по …»."""
        low = text.lower()
        best = ""
        best_length = 0
        if self.feed is not None:
            for subject in self.feed.subjects():
                needle = subject.lower()
                matched = needle in low
                if not matched:
                    # Русские падежи: «по анатомии» ↔ «Анатомия человека».
                    # Сравниваем начала значимых слов предмета.
                    for word in re.findall(r"[а-яёa-z]{5,}", needle):
                        if word[:5] in low:
                            matched = True
                            break
                if matched and len(needle) > best_length:
                    best, best_length = subject, len(needle)
        if best:
            return best
        match = re.search(r"\bпо\s+([а-яёa-z][а-яёa-z\s\-]{2,40})", low)
        if match:
            return match.group(1).strip().capitalize()
        return ""

    def detect_deadline(self, text, today=None):
        """Возвращает (iso_deadline, подпись) или ('', '')."""
        today = today or date.today()
        low = text.lower()

        def at(day, hour=23, minute=59):
            return "%s %02d:%02d" % (day.isoformat(), hour, minute)

        if "послезавтра" in low:
            return at(today + timedelta(days=2)), "послезавтра"
        if "завтра" in low:
            return at(today + timedelta(days=1)), "завтра"
        if "сегодня" in low:
            return at(today), "сегодня"
        match = re.search(r"через\s+(\d+)\s*(день|дня|дней|недел\w*)", low)
        if match:
            amount = int(match.group(1))
            delta = timedelta(weeks=amount) if match.group(2).startswith("недел") \
                else timedelta(days=amount)
            return at(today + delta), "через %s" % match.group(1)
        if "концу недели" in low or "конец недели" in low:
            friday = today + timedelta(days=(4 - today.weekday()) % 7)
            return at(friday), "к пятнице"
        if "концу месяца" in low or "конец месяца" in low:
            first_next = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            return at(first_next - timedelta(days=1)), "к концу месяца"
        # «к пятнице», «до среды», «в понедельник» (с падежами: основа + окончание)
        for stem, weekday in WEEKDAY_STEMS:
            if re.search(r"\b(к|до|в|во|на)\s+%s\w*" % stem, low):
                delta = (weekday - today.weekday()) % 7
                if delta == 0:
                    delta = 7
                return (at(today + timedelta(days=delta)),
                        "к " + DATIVE_WEEKDAYS[weekday])
        # Короткие «к пн», «до ср».
        short = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
        for word, weekday in short.items():
            if re.search(r"\b(к|до|в|во|на)\s+%s\b" % word, low):
                delta = (weekday - today.weekday()) % 7
                if delta == 0:
                    delta = 7
                return at(today + timedelta(days=delta)), "к " + word
        # «к 20.10» / «до 20.10»
        match = re.search(r"(?:к|до|на|)\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?",
                          low)
        if match:
            day, month = int(match.group(1)), int(match.group(2))
            year = int(match.group(3) or today.year)
            if year < 100:
                year += 2000
            try:
                return at(date(year, month, day)), "%d.%02d" % (day, month)
            except ValueError:
                pass
        # «к 15:00»
        match = re.search(r"(?:к|до)\s*(\d{1,2}):(\d{2})", low)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            moment = datetime.combine(today, datetime.min.time()).replace(
                hour=hour, minute=minute)
            if moment <= datetime.now():
                moment += timedelta(days=1)
            return moment.strftime("%Y-%m-%d %H:%M"), "к %02d:%02d" % (hour, minute)
        return "", ""

    def detect_volume(self, text):
        """Сколько «единиц» объёма и сколько это примерно минут."""
        low = text.lower()
        match = re.search(r"(\d+)\s*(страниц\w*|стр|глав\w*|задач\w*|слайд\w*|билет\w*)",
                          low)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            for key, minutes in MINUTES_PER_UNIT.items():
                if unit.startswith(key[:4]):
                    return amount, unit, int(amount * minutes)
        match = re.search(r"(\d+)\s*(час\w*|ч)(?!\w)", low)
        if match:
            return int(match.group(1)), "час", int(match.group(1)) * 60
        match = re.search(r"(\d+)\s*(минут\w*|мин)", low)
        if match:
            return int(match.group(1)), "минут", int(match.group(1))
        return 0, "", 0

    def detect_priority(self, text):
        low = text.lower()
        if any(word in low for word in ("срочно", "важно", "горит", "дедлайн")):
            return 1, "высокий"
        if any(word in low for word in ("потом", "не срочно", "когда нибудь")):
            return 3, "низкий"
        return 2, "обычный"

    # --------------------------------------------------------- разложение

    def decompose(self, title, kind, estimate_minutes):
        """Возвращает список шагов [(название, минуты)]."""
        template = TEMPLATES.get(kind) or TEMPLATES["другое"]
        total_weight = sum(weight for _, weight in template)
        estimate = max(int(estimate_minutes or 0), 60)
        steps = []
        remaining = estimate
        for index, (label, weight) in enumerate(template):
            if index == len(template) - 1:
                minutes = max(remaining, 15)
            else:
                minutes = max(int(round(estimate * weight / total_weight / 5.0)) * 5,
                              15)
                remaining -= minutes
            steps.append((label, minutes))
        return steps

    # ------------------------------------------------------------ главное

    def handle(self, text, today=None):
        """Разбирает сообщение пользователя и возвращает ответ и действие."""
        text = (text or "").strip()
        low = text.lower()
        if not text:
            return {"reply": "Напиши задачу: «переписать конспект по анатомии к пятнице».",
                    "task": None, "changed": False}
        if any(phrase in low for phrase in ("план на сегодня", "что сегодня", "план дня")):
            return {"reply": self.today_reply(today), "task": None, "changed": False}
        if any(phrase in low for phrase in ("что дальше", "что сейчас", "чем заняться")):
            return {"reply": self.next_action_reply(today), "task": None, "changed": False}
        if any(phrase in low for phrase in ("статистика", "как успехи", "прогресс")):
            return {"reply": self.stats_reply(), "task": None, "changed": False}
        if any(phrase in low for phrase in ("помощь", "что ты умеешь", "как пользоваться")):
            return {"reply": self.help_reply(), "task": None, "changed": False}

        kind = self.detect_kind(text)
        subject = self.detect_subject(text)
        deadline, deadline_label = self.detect_deadline(text, today)
        amount, unit, volume_minutes = self.detect_volume(text)
        priority, priority_label = self.detect_priority(text)

        base = {"конспект": 120, "реферат": 180, "презентация": 150,
                "лабораторная": 150, "задачи": 120, "чтение": 90,
                "подготовка": 180, "другое": 90}.get(kind, 90)
        estimate = volume_minutes or base
        estimate = max(30, min(int(estimate), 900))

        title = self._clean_title(text)
        steps = self.decompose(title, kind, estimate)
        task_id = self.store.add_task(
            title=title, subject=subject, kind=kind, deadline=deadline,
            priority=priority,
            estimate_minutes=sum(minutes for _, minutes in steps),
            notes="создано из чата: " + text[:300], source="chat")
        for index, (label, minutes) in enumerate(steps):
            self.store.add_subtask(task_id, label, minutes, index)

        plan = self.planner.plan()
        task_info = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        reply = self.task_reply(title, kind, subject, deadline_label, priority_label,
                                steps, task_info, plan)
        return {"reply": reply, "task": {"id": task_id, "title": title,
                                         "steps": [label for label, _ in steps]},
                "changed": True}

    # -------------------------------------------------- тексты ответов

    @staticmethod
    def _clean_title(text):
        """Убирает из названия срок, объём и служебные слова."""
        title = " " + text.strip() + " "
        title = re.sub(r"^\s*(надо|нужно|мне надо|мне нужно|помоги|сделай|напоминай)\s+",
                       "", title, flags=re.I)
        # объём: «20 страниц», «на 3 главы», «2 часа»
        title = re.sub(r",?\s*(?:на\s+|в\s+|во\s+|до\s+|к\s+)?\d+\s*"
                       r"(страниц\w*|стр|глав\w*|задач\w*|слайд\w*|"
                       r"билет\w*|час\w*|минут\w*)\b", "", title, flags=re.I)
        # срок: «к пятнице», «до среды», «в понедельник», «к пн»
        stems = "|".join(stem + r"\w*" for stem, _ in WEEKDAY_STEMS) + "|пн|вт|ср|чт|пт|сб|вс"
        title = re.sub(r",?\s*(к|до|в|во|на)\s+({%s})\b" % stems, "", title, flags=re.I)
        title = re.sub(r",?\s*(к|до)\s+\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?", "",
                       title, flags=re.I)
        title = re.sub(r",?\s*(сегодня|завтра|послезавтра|через\s+\d+\s+\w+)", "",
                       title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" ,.-!?")
        if len(title) > 90:
            title = title[:87] + "..."
        return title[:1].upper() + title[1:] if title else "Задача"

    def task_reply(self, title, kind, subject, deadline_label, priority_label,
                   steps, task_info, plan):
        lines = ["📌 <b>%s</b>" % title]
        details = []
        if subject:
            details.append("предмет: %s" % subject)
        details.append("тип: %s" % kind)
        if deadline_label:
            details.append("срок: %s" % deadline_label)
        details.append("приоритет: %s" % priority_label)
        lines.append(" · ".join(details))
        lines.append("")
        lines.append("<b>Разложил на шаги (%d мин всего):</b>" % sum(m for _, m in steps))
        for index, (label, minutes) in enumerate(steps, start=1):
            lines.append("%d. %s — %d мин" % (index, label, minutes))
        if task_info:
            lines.append("")
            if task_info["risk"]:
                lines.append("⚠️ <b>Не хватает времени:</b> нужно %d мин, а свободного "
                             "времени до срока %d мин (дефицит %d мин)."
                             % (task_info["needed_minutes"],
                                task_info["capacity_minutes"],
                                task_info["deficit_minutes"]))
                lines.append("Можно урезать объём, поднять дневной лимит в настройках "
                             "или начать раньше.")
            else:
                lines.append("✅ Времени хватает: нужно %d мин, свободно до срока %d мин."
                             % (task_info["needed_minutes"],
                                task_info["capacity_minutes"]))
        today_blocks = self._blocks_for_day(date.today())
        if today_blocks:
            lines.append("")
            lines.append("<b>На сегодня:</b>")
            for block in today_blocks[:4]:
                lines.append("• %s–%s %s" % (block["start"], block["end"],
                                             block["task_title"]))
        return "\n".join(lines)

    def _blocks_for_day(self, day):
        plan = self.planner.build_plan(days=1)
        for item in plan["days"]:
            if item["date"] == day.isoformat():
                return item["blocks"]
        return []

    def today_reply(self, today=None):
        today = today or date.today()
        plan = self.planner.build_plan()
        day = next((item for item in plan["days"] if item["is_today"]), None)
        if not day:
            return "На сегодня данных нет."
        lines = ["📅 <b>План на сегодня (%s):</b>" % today.strftime("%d.%m")]
        if day["pairs"]:
            lines.append("Пары: %d" % len(day["pairs"]))
        if not day["blocks"]:
            lines.append("Запланированных шагов нет — добавь задачу.")
        for block in day["blocks"]:
            mark = "✅" if block["done"] else "▫️"
            lines.append("%s %s–%s %s (%d мин)"
                         % (mark, block["start"], block["end"],
                            block["task_title"], block["minutes"]))
        lines.append("")
        lines.append("Свободно сегодня: %d мин." % day["free_minutes"])
        return "\n".join(lines)

    def next_action_reply(self, today=None):
        plan = self.planner.build_plan(days=7)
        for day in plan["days"]:
            for block in day["blocks"]:
                if block["done"]:
                    continue
                return ("▶️ Сейчас по плану: <b>%s</b> (%s–%s).\n"
                        "Шаг: %s (%d мин)."
                        % (block["task_title"], block["start"], block["end"],
                           block["title"], block["minutes"]))
        return "Плана пока нет — напиши задачу, и я разложу её по дням."

    def stats_reply(self):
        counts = self.store.counts()
        lines = ["📊 <b>Как успехи</b>",
                 "Открытых задач: %d, закрыто: %d" % (counts["tasks_open"],
                                                      counts["tasks_done"]),
                 "Шагов: %d из %d закрыто" % (counts["subtasks_done"],
                                              counts["subtasks_total"]),
                 "Запланировано на сегодня: %d мин (%d шагов)"
                 % (counts["today_minutes"], counts["today_steps"]),
                 "Всего в фокус-сессиях: %d мин" % counts["sessions_total_minutes"]]
        return "\n".join(lines)

    @staticmethod
    def help_reply():
        return ("Я умею:\n"
                "• разложить задачу: «переписать конспект по анатомии к пятнице»\n"
                "• посчитать сроки с учётом пар: покажу, хватает ли времени\n"
                "• «план на сегодня», «что дальше», «статистика»\n"
                "• отметить шаг выполненным — кнопкой в списке\n"
                "Типы работ: конспект, реферат, презентация, лабораторная, задачи, "
                "чтение, подготовка к экзамену.")
