# -*- coding: utf-8 -*-
"""Разведка: как в API ПГУФКСиТ выглядят пары, которые делятся на подгруппы.

Запуск:
    python tools/probe_subgroups.py 26282
"""

import json
import sys
from datetime import date

sys.path.insert(0, ".")
from schedule import unifirst      # noqa: E402


def main():
    group = sys.argv[1] if len(sys.argv) > 1 else "26282"
    api = unifirst.Unifirst()
    year, week = unifirst.iso_year_week(date.today())
    payload = api.timetable(group, year, week)
    lessons = unifirst.normalize(payload)

    print("группа %s, неделя %d/%d: пар %d" % (group, year, week, len(lessons)))
    print("\nполя пары (пример):")
    if lessons:
        print(json.dumps(lessons[0], ensure_ascii=False, indent=2)[:600])

    print("\nпары, где в одном слоте больше одного варианта (подгруппы):")
    blocks = unifirst.group_by_slot(lessons)
    found = 0
    for block in blocks:
        if len(block["lessons"]) < 2:
            continue
        found += 1
        print("\n  %s, %s пара, %s–%s:"
              % (block.get("date"), block.get("para"), block.get("start"), block.get("end")))
        for lesson in block["lessons"]:
            print("     • %-46s | %-30s | %s | %s"
                  % (lesson.get("subject"), ", ".join(lesson.get("teachers") or []),
                     ", ".join(lesson.get("rooms") or []), lesson.get("type")))
        print("     сырые поля варианта:",
              json.dumps({key: block["lessons"][0].get(key)
                          for key in ("subgroup", "groups", "group", "stream", "uid")},
                         ensure_ascii=False))
    if not found:
        print("  нет — в этой неделе подгрупп не встретилось")

    print("\nвсе поля, которые вообще встречаются у пар:")
    fields = {}
    for lesson in lessons:
        for key, value in lesson.items():
            fields.setdefault(key, set()).add(type(value).__name__)
    for key in sorted(fields):
        print("   %-14s %s" % (key, ", ".join(sorted(fields[key]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
