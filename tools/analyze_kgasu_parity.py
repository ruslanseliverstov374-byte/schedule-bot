# -*- coding: utf-8 -*-
"""Разбор привязки «Чет/Неч» к календарю в расписании КГАСУ.

В файлах КГАСУ на каждую пару идёт две строки: «Чет» и «Неч». Чтобы бот знал,
какая неделя сейчас, нужно понять, как эти метки соотносятся с датами.
В ячейках встречаются пометки вида «С 28.09.26.» — по ним и калибруем.
"""

import re
import sys
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree

NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MONTHS = {"01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6, "07": 7, "08": 8,
          "09": 9, "10": 10, "11": 11, "12": 12}


def cells_text(cell):
    return re.sub(r"\s+", " ", "".join(node.text or "" for node in cell.iter(NS + "t"))).strip()


def load_rows(path):
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    tables = []
    for table in root.iter(NS + "tbl"):
        rows = [[cells_text(cell) for cell in row.findall(NS + "tc")]
                for row in table.findall(NS + "tr")]
        tables.append(rows)
    return tables


def monday_of(day):
    return day - timedelta(days=day.weekday())


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/kgasu_26RP01_02.docx"
    tables = load_rows(path)
    rows = max(tables, key=len)
    print("главная таблица: %d строк" % len(rows))

    day = ""
    markers = []
    for row in rows:
        if len(row) < 5:
            continue
        if row[0].strip():
            day = row[0].strip()
        parity = row[2].strip().lower()
        for column, cell in enumerate(row[4:], start=4):
            for match in re.finditer(r"С\s+(\d{2})\.(\d{2})\.(\d{2})", cell):
                dd, mm, yy = match.groups()
                try:
                    when = date(2000 + int(yy), MONTHS[mm], int(dd))
                except ValueError:
                    continue
                markers.append((when, parity, day, row[1], column))

    print("пометок «С дата» найдено: %d" % len(markers))
    by_monday = {}
    for when, parity, day_name, para, column in markers:
        monday = monday_of(when)
        by_monday.setdefault(monday, set()).add(parity)
        print("   %s (%s) | неделя с %s | метка: %s | %s, пара %s, столбец %d"
              % (when.strftime("%d.%m.%Y"), "Пн" if when.weekday() == 0 else
                 ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][when.weekday()],
                 monday.strftime("%d.%m"), parity, day_name, para, column))

    print("\nпроверка непротиворечивости (одна неделя — одна метка):")
    bad = {monday: values for monday, values in by_monday.items() if len(values) > 1}
    print("   недель с метками:", len(by_monday), "| противоречивых:", len(bad))
    for monday, values in bad.items():
        print("   ⚠️ неделя с %s: %s" % (monday.strftime("%d.%m"), values))

    if by_monday:
        anchor_monday = min(by_monday)
        print("\nопорная неделя (самая ранняя с пометкой): %s -> %s"
              % (anchor_monday.strftime("%d.%m.%Y"), ", ".join(by_monday[anchor_monday])))
        for monday in sorted(by_monday):
            weeks = (monday - anchor_monday).days // 7
            print("   неделя с %s: +%d нед. -> %s" % (monday.strftime("%d.%m"), weeks,
                                                     ", ".join(by_monday[monday])))
        semester = date(2026, 9, 1)
        print("\nначало семестра в шапке: %s (%s)"
              % (semester.strftime("%d.%m.%Y"),
                 ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][semester.weekday()]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
