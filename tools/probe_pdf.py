# -*- coding: utf-8 -*-
"""Разведка PDF-файла расписания: есть ли внутри текст и как он закодирован.

Запуск:
    python tools/probe_pdf.py https://st.kgasu.ru/iblock/.../26ZK01.pdf
"""

import re
import ssl
import sys
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load(source):
    if source.startswith("http"):
        request = urllib.request.Request(source, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=90,
                                    context=ssl._create_unverified_context()) as response:
            return response.read()
    with open(source, "rb") as handle:
        return handle.read()


def main():
    source = sys.argv[1]
    blob = load(source)
    print("файл: %d байт, версия %s" % (len(blob), blob[:8].decode("latin-1").strip()))

    print("\n1. Общая структура")
    for pattern in (b"/Type /Page", b"/Type/Page", b"/Subtype /Image", b"/Subtype/Image",
                    b"/Subtype /Type0", b"/Subtype/Type0", b"/TrueType", b"/ToUnicode",
                    b"FlateDecode", b"BT", b"Tj", b"TJ"):
        print("   %-18s встречается %d раз" % (pattern.decode("latin-1"),
                                               len(re.findall(re.escape(pattern), blob))))

    print("\n2. Пробую распаковать потоки")
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", blob, re.S)
    print("   потоков найдено: %d" % len(streams))
    unpacked = 0
    text_pieces = 0
    sample = ""
    for raw in streams[:40]:
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            continue
        unpacked += 1
        if b"Tj" in data or b"TJ" in data:
            text_pieces += 1
            if not sample:
                sample = data[:600].decode("latin-1", "replace")
    print("   распаковано: %d, из них с текстом: %d" % (unpacked, text_pieces))
    if sample:
        print("\n3. Пример содержимого потока (текстовые операторы):")
        print("   " + sample.replace("\n", "\n   ")[:700])

    print("\n4. Шрифты и кодировки")
    fonts = set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9+\-_,]+)", blob))
    print("   шрифтов: %d" % len(fonts))
    for name in sorted(fonts)[:10]:
        print("      %s" % name.decode("latin-1"))
    cmaps = re.findall(rb"/ToUnicode\s+(\d+)\s+0\s+R", blob)
    print("   ссылок на ToUnicode: %d" % len(cmaps))
    if b"beginbfchar" in blob or b"beginbfrange" in blob:
        print("   ToUnicode-таблицы внутри есть — кириллицу можно расшифровать")
    else:
        print("   ToUnicode в явном виде не найден (проверю потоки)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
