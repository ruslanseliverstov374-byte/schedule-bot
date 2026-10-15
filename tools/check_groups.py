# -*- coding: utf-8 -*-
"""Проверка списка групп ПГУФКСиТ: что отдаёт сайт и не пропали ли группы студентов.

Запуск:
    python tools/check_groups.py
    python tools/check_groups.py --students 26282 26281 26351 26101
"""

import argparse
import sys

sys.path.insert(0, ".")
from schedule import unifirst      # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Что сейчас в списке групп ПГУФКСиТ")
    parser.add_argument("--students", nargs="*", default=[],
                        help="группы, где есть студенты")
    parser.add_argument("--limit", type=int, default=500)
    arguments = parser.parse_args()

    api = unifirst.Unifirst()
    groups = api.groups(limit=arguments.limit)
    names = [unifirst.group_title(item) for item in groups]
    print("групп в списке: %d" % len(names))
    print("первые: %s" % ", ".join(names[:12]))
    print("последние: %s" % ", ".join(names[-8:]))

    courses = {}
    for name in names:
        courses[name[:2]] = courses.get(name[:2], 0) + 1
    print("по годам набора: %s"
          % ", ".join("%s — %d" % (key, value) for key, value in sorted(courses.items())))

    missing = []
    for group in arguments.students:
        found = unifirst.find_group(groups, group)
        if found:
            print("✅ группа %s на месте" % group)
        else:
            missing.append(group)
            print("❌ группы %s НЕТ в списке" % group)
    if missing:
        print("\nПропали: %s — студенты этих групп не увидят расписание." % ", ".join(missing))
        print("Похоже, сайт обновляет список групп (например, новый период обучения).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
