# -*- coding: utf-8 -*-
"""Просмотр расписания КГАСУ из файла .doc или .docx (ссылка или локальный путь)."""

import sys
import ssl
import urllib.request

sys.path.insert(0, ".")

from schedule import docparse, docxparse      # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load(source):
    if source.startswith("http"):
        request = urllib.request.Request(source, headers={"User-Agent": UA})
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            return response.read(), source.split("/")[-1]
    with open(source, "rb") as handle:
        return handle.read(), source.split("\\")[-1]


def main():
    if len(sys.argv) < 2:
        print("укажите ссылку или путь к файлу расписания")
        return 2
    blob, name = load(sys.argv[1])
    print("файл: %s (%d байт)" % (name, len(blob)))

    if blob[:2] == b"PK":
        tables = docxparse.docx_tables(blob)
        text = docxparse.docx_text(blob)
    else:
        tables = docparse.doc_tables(blob)
        text = docparse.doc_text(blob)

    print("таблиц: %d | строк в самой большой: %d"
          % (len(tables), max((len(rows) for rows in tables), default=0)))
    print("текст, первые 6 непустых строк:")
    shown = 0
    for line in text.splitlines():
        if line.strip():
            print("   " + line.strip()[:110])
            shown += 1
            if shown >= 6:
                break

    for index, rows in enumerate(tables[:2], 1):
        print("\n--- таблица %d (%d строк) ---" % (index, len(rows)))
        for row in rows[:8]:
            print("   | " + " | ".join((cell.replace("\n", " ")[:30] or "·") for cell in row[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
