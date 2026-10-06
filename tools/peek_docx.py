# -*- coding: utf-8 -*-
"""Просмотр таблиц внутри .docx (расписание КГАСУ) — только стандартная библиотека."""

import re
import sys
import zipfile
from xml.etree import ElementTree

NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def cell_text(cell):
    return re.sub(r"\s+", " ", "".join(node.text or "" for node in cell.iter(NS + "t"))).strip()


def tables_from_docx(path):
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    result = []
    for table in root.iter(NS + "tbl"):
        rows = []
        for row in table.findall(NS + "tr"):
            cells = [cell_text(cell) for cell in row.findall(NS + "tc")]
            rows.append(cells)
        result.append(rows)
    return result


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/recon/kgasu_26RP01_02.docx"
    tables = tables_from_docx(path)
    print("файл: %s" % path)
    print("таблиц: %d" % len(tables))
    for index, rows in enumerate(tables[:3], 1):
        print("\n--- таблица %d: строк %d, столбцов %d ---"
              % (index, len(rows), max((len(row) for row in rows), default=0)))
        for row in rows[:14]:
            print("   | " + " | ".join((cell[:26] or "·") for cell in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
