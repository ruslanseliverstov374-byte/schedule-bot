# -*- coding: utf-8 -*-
"""Печатает ячейки .doc-файла с номерами — нужно, чтобы понять сетку таблицы."""

import ssl
import sys
import urllib.request

sys.path.insert(0, ".")
from schedule import docparse      # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def main():
    source = sys.argv[1]
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    if source.startswith("http"):
        request = urllib.request.Request(source, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=60,
                                    context=ssl._create_unverified_context()) as response:
            blob = response.read()
    else:
        with open(source, "rb") as handle:
            blob = handle.read()

    text = docparse.doc_text(blob)
    cells = text.split("\t")
    print("всего ячеек: %d" % len(cells))
    for index, cell in enumerate(cells[start:start + count], start=start):
        value = cell.replace("\n", "⏎")
        print("%4d | %s" % (index, value[:90] if value.strip() else "(пусто)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
