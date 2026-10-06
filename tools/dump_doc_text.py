# -*- coding: utf-8 -*-
"""Показывает структуру текста .doc: абзацы и ячейки (разделитель — табуляция)."""

import ssl
import sys
import urllib.request

sys.path.insert(0, ".")
from schedule import docparse      # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def main():
    source = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    if source.startswith("http"):
        request = urllib.request.Request(source, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=60,
                                    context=ssl._create_unverified_context()) as response:
            blob = response.read()
    else:
        with open(source, "rb") as handle:
            blob = handle.read()

    text = docparse.doc_text(blob)
    paragraphs = text.split("\n")
    print("абзацев: %d" % len(paragraphs))
    for index, paragraph in enumerate(paragraphs[:limit]):
        cells = paragraph.split("\t")
        if not paragraph.strip():
            print("%3d. (пусто)" % index)
            continue
        short = " ⟩ ".join((cell.strip()[:24] or "·") for cell in cells[:9])
        print("%3d. [%2d ячеек] %s%s" % (index, len(cells), short,
                                         " …" if len(cells) > 9 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
