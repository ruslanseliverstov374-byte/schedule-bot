# -*- coding: utf-8 -*-
"""Проверка ложных «изменений расписания»: два запроса одной недели сравниваются.

Если источник отдаёт одни и те же данные, а движок видит изменения — студентов
будут зря дёргать уведомлениями. Скрипт показывает, стабилен ли разбор.
"""

import sys
from datetime import date

sys.path.insert(0, ".")
from schedule import unifirst      # noqa: E402


def main():
    group = sys.argv[1] if len(sys.argv) > 1 else "26282"
    api = unifirst.Unifirst()
    today = date.today()
    year, week = unifirst.iso_year_week(today)
    print("Группа %s, неделя %d/%d" % (group, year, week))

    payload_one = api.timetable(group, year, week)
    lessons_one = unifirst.normalize(payload_one)
    payload_two = api.timetable(group, year, week)
    lessons_two = unifirst.normalize(payload_two)

    digest_one = unifirst.digest(lessons_one)
    digest_two = unifirst.digest(lessons_two)
    print("пар: %d и %d" % (len(lessons_one), len(lessons_two)))
    print("совпадают ли слепки:", digest_one == digest_two)
    differences = unifirst.diff_lessons(lessons_one, lessons_two)
    print("различий между двумя запросами:", len(differences))
    for item in differences[:5]:
        print("   %s: %s" % (item.get("kind"), (item.get("lesson") or {}).get("subject")))
    print()
    print("пример пар на сегодня:")
    for lesson in [item for item in lessons_one if item.get("date") == today.isoformat()]:
        print("   %s пара %s–%s | %s | %s | %s"
              % (lesson.get("para"), lesson.get("start"), lesson.get("end"),
                 lesson.get("subject"), lesson.get("type"),
                 ", ".join(lesson.get("rooms") or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
