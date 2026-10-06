# -*- coding: utf-8 -*-
"""Разбор дублей в расписании и проверка чётности недель."""

import json
import sys
from collections import Counter, defaultdict
from datetime import date

sys.path.insert(0, ".")
from schedule.unifirst import Unifirst, iso_year_week, normalize  # noqa: E402

api = Unifirst(timeout=30)

print("=== 1. Сколько групп вообще имеют подгруппы ===")
groups = api.groups(limit=500)
with_sub = [g for g in groups if g.get("hasSubgroups")]
print("групп всего: %d | с подгруппами: %d" % (len(groups), len(with_sub)))
names = set()
for group in with_sub[:40]:
    for sub in group.get("subgroups") or []:
        names.add(str(sub.get("name")))
print("примеры подгрупп:", sorted(names)[:25])
target = [g for g in groups if g.get("name") == "26282"]
print("наша группа:", json.dumps(target[0], ensure_ascii=False) if target else "не найдена")

print("\n=== 2. Ключи в данных (нет ли признака чётности недели) ===")
today = date.today()
year, week = iso_year_week(today)
payload = api.timetable("26282", year, week)
entry = (payload.get("data") or [{}])[0]
print("ключи записи:", sorted(entry.keys()))
print("ключи meta:", sorted((payload.get("meta") or {}).keys()))
item_keys = Counter()
for day in (entry.get("lessons") or {}).values():
    for item in day.get("items") or []:
        for key in item:
            item_keys[key] += 1
print("ключи пары:", dict(item_keys))
for day_key, day in list((entry.get("lessons") or {}).items())[:1]:
    items = day.get("items") or []
    if items:
        print("пример пары целиком:", json.dumps(items[0], ensure_ascii=False))

print("\n=== 3. Параллельные пары (один слот — разные преподаватели/аудитории) ===")
for delta in (0, 1, 2):
    y, w = iso_year_week(today.replace(day=today.day) if delta == 0 else
                         date.fromordinal(today.toordinal() + 7 * delta))
    try:
        lessons = normalize(api.timetable("26282", y, w))
    except Exception as error:
        print("неделя %d/%d: ошибка %s" % (y, w, error))
        continue
    slots = defaultdict(list)
    for lesson in lessons:
        slots[(lesson["date"], lesson["para"], lesson["start"])].append(lesson)
    parallel = {slot: group for slot, group in slots.items() if len(group) > 1}
    print("неделя %d/%d: пар %d, слотов с дублями %d" % (y, w, len(lessons), len(parallel)))
    for slot, group in list(parallel.items())[:3]:
        print("   %s пара %s (%s):" % (slot[0], slot[1], slot[2]))
        for lesson in group:
            print("      %s | %s | %s" % (lesson["subject"], ", ".join(lesson["teachers"]),
                                          ", ".join(lesson["rooms"])))

print("\n=== 4. Сравнение соседних недель (чётная/нечётная) ===")
for delta in (0, 1):
    y, w = iso_year_week(date.fromordinal(today.toordinal() + 7 * delta))
    lessons = normalize(api.timetable("26282", y, w))
    subjects = Counter(lesson["subject"] for lesson in lessons)
    print("неделя %d/%d: пар %d" % (y, w, len(lessons)))
    for subject, count in subjects.most_common(6):
        print("   %-55s %d" % (subject[:55], count))
