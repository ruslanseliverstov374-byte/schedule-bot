# -*- coding: utf-8 -*-
"""Предпросмотр экранов бота на живых данных (без Telegram и без токена).

Показывает ровно те сообщения, которые бот отправит студенту: расписание на сегодня
и завтра, неделю, вечерний дайджест и экран домашки. HTML-разметка Telegram убирается,
чтобы текст было удобно читать в консоли или файле.

Запуск:
    python tools/preview.py            # группа 26281
    python tools/preview.py 26282      # другая группа
    python tools/preview.py 26281 --save preview.txt
"""

import argparse
import os
import re
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import texts                                                    # noqa: E402
from schedule import unifirst                                   # noqa: E402


def plain(markup):
    """Убирает HTML-разметку Telegram и лишние пустые строки."""
    text = re.sub(r"<[^>]+>", "", markup or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def main():
    parser = argparse.ArgumentParser(description="Предпросмотр экранов бота")
    parser.add_argument("group", nargs="?", default="26281", help="номер группы")
    parser.add_argument("--save", help="сохранить результат в файл")
    parser.add_argument("--date", help="дата в формате ДД.ММ.ГГГГ (по умолчанию сегодня)")
    arguments = parser.parse_args()

    today = date.today()
    if arguments.date:
        day, month, year = arguments.date.split(".")
        today = date(int(year), int(month), int(day))

    api = unifirst.Unifirst()
    groups = api.groups(limit=500)
    group = unifirst.find_group(groups, arguments.group)
    if not group:
        print("Группа %r не найдена. Примеры: %s"
              % (arguments.group, ", ".join(str(item["name"]) for item in groups[:8])))
        return 1
    title = unifirst.group_title(group)

    year, week = unifirst.iso_year_week(today)
    lessons = unifirst.normalize(api.timetable(title, year, week))
    next_year, next_week = unifirst.iso_year_week(today + timedelta(days=7))
    try:
        next_lessons = unifirst.normalize(api.timetable(title, next_year, next_week))
    except unifirst.UnifirstError:
        next_lessons = []

    def of_day(day):
        return [item for item in lessons if item.get("date") == day.isoformat()]

    tomorrow = today + timedelta(days=1)
    tomorrow_lessons = of_day(tomorrow)
    if not tomorrow_lessons and tomorrow.isocalendar()[1] != week:
        tomorrow_lessons = [item for item in next_lessons
                            if item.get("date") == tomorrow.isoformat()]

    blocks = []
    blocks.append("СЕГОДНЯ\n" + texts.day_schedule(today, of_day(today), title))
    blocks.append("ЗАВТРА\n" + texts.day_schedule(tomorrow, tomorrow_lessons, title))
    blocks.append("НЕДЕЛЯ\n" + texts.week_schedule(lessons, title, year, week))

    user = {"evening_time": "20:00", "tz_offset": 3, "evening_enabled": 1}
    homework = [{
        "subject": (tomorrow_lessons or of_day(today) or [{"subject": ""}])[0].get("subject", ""),
        "task": "§§1-2, вопросы 3-5", "due_date": tomorrow.isoformat(),
        "due_time": "", "teacher": "", "room": "", "scope": "group",
    }]
    blocks.append("ВЕЧЕРНИЙ ДАЙДЖЕСТ (20:00)\n" + texts.evening_digest(
        user, tomorrow, tomorrow_lessons, homework, title))

    blocks.append("КАРТОЧКА ДОМАШКИ\n" + texts.homework_item({
        "subject": "История России", "task": "§§1-2, вопросы 3-5",
        "due_date": (today + timedelta(days=1)).isoformat(), "due_time": "",
        "teacher": "Фан-Юнг Г.Ю.", "room": "E703", "scope": "group"}, votes=3))

    if lessons:
        first = of_day(today)[0] if of_day(today) else lessons[0]
        blocks.append("НАПОМИНАНИЕ ПЕРЕД ПАРОЙ\n"
                      + texts.before_lesson_reminder(30, first))

    output = ("\n\n" + "=" * 64 + "\n\n").join(plain(block) for block in blocks)
    header = ("Предпросмотр бота расписания\nГруппа: %s\n%s\n%s\n"
              % (title, unifirst.week_label(year, week),
                 "Пар на неделе: %d" % len(lessons)))
    result = header + "\n" + "=" * 64 + "\n\n" + output + "\n"

    if arguments.save:
        with open(arguments.save, "w", encoding="utf-8") as handle:
            handle.write(result)
        print("Сохранено в %s" % arguments.save)
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
