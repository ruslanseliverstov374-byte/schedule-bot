# -*- coding: utf-8 -*-
"""Проверка разбора расписания КГАСУ на живых данных.

Запуск:
    python tools/kgasu_preview.py 26РП01
    python tools/kgasu_preview.py 26РП01 --date 12.10.2026
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from schedule import kgasu      # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Предпросмотр расписания КГАСУ")
    parser.add_argument("group", nargs="?", default="26РП01")
    parser.add_argument("--date", help="дата в формате ДД.ММ.ГГГГ")
    parser.add_argument("--days", type=int, default=3, help="сколько дней показать")
    parser.add_argument("--subgroup", default="", help="подгруппа, например 'Подгруппа 1'")
    arguments = parser.parse_args()

    today = date.today()
    if arguments.date:
        day, month, year = (int(part) for part in arguments.date.split("."))
        today = date(year, month, day)

    print("1. Получаю список групп КГАСУ...")
    all_groups = kgasu.groups()
    print("   групп: %d" % len(all_groups))
    group = kgasu.find_group(all_groups, arguments.group)
    if not group:
        print("   группа %r не найдена" % arguments.group)
        return 1
    print("   выбрана: %s (файл %s, формат %s)"
          % (group["name"], group["file_name"], group["file_ext"]))

    print("\n2. Скачиваю и разбираю файл расписания...")
    blob, path = kgasu.download(group["file_url"])
    print("   файл: %d байт, кэш: %s" % (len(blob), path))
    grid = kgasu.prepare(blob, group["file_name"])
    print("   тип разбора: %s" % grid["kind"])
    print("   столбцы занятий: %s" % ", ".join(
        "%s/%s" % (column["group"], column["subgroup"] or "—")
        for column in (grid.get("columns") or [])[:8]))
    anchor_monday, anchor_label = grid.get("anchor") or (None, "")
    print("   опорная неделя: %s -> %s"
          % (anchor_monday.strftime("%d.%m.%Y") if anchor_monday else "не определена",
             anchor_label))
    print("   строк в таблице: %d" % len(grid.get("rows") or []))

    print("\n3. Пары на ближайшие дни:")
    for offset in range(arguments.days):
        day = today + timedelta(days=offset)
        lessons = kgasu.lessons_for_day(grid, group["name"], day, arguments.subgroup)
        parity = kgasu.parity_of(day, anchor_monday, anchor_label)
        print("\n   %s (%s, неделя %s): пар %d"
              % (day.strftime("%d.%m.%Y"), kgasu.WEEKDAYS[day.weekday()], parity, len(lessons)))
        for lesson in lessons:
            print("      %s пара %s-%s | %s%s | %s | %s"
                  % (lesson["para"], lesson["start"], lesson["end"], lesson["subject"],
                     (" (%s)" % lesson["type"]) if lesson["type"] else "",
                     ", ".join(lesson["teachers"]) or "преподаватель не указан",
                     ", ".join(lesson["rooms"]) or "аудитория не указана"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
