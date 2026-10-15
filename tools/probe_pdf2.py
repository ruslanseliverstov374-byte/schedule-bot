# -*- coding: utf-8 -*-
"""Глубокая разведка PDF: объекты, фильтры, попытки достать текст.

Запуск:
    python tools/probe_pdf2.py <файл-или-ссылка>
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
        with urllib.request.urlopen(request, timeout=120,
                                    context=ssl._create_unverified_context()) as response:
            return response.read()
    with open(source, "rb") as handle:
        return handle.read()


def main():
    blob = load(sys.argv[1])
    print("файл: %d байт" % len(blob))

    objects = re.findall(rb"(\d+)\s+0\s+obj(.*?)endobj", blob, re.S)
    print("объектов: %d" % len(objects))
    texts_found = []
    images = 0
    for number, body in objects:
        dictionary = body.split(b"stream", 1)[0]
        keys = re.findall(rb"/(\w+)", dictionary)
        interesting = [key.decode() for key in keys
                       if key in (b"Filter", b"Subtype", b"Font", b"ToUnicode", b"Length",
                                  b"Width", b"Height", b"ColorSpace", b"Type")]
        if b"/Image" in dictionary:
            images += 1
        if b"stream" in body:
            raw = body.split(b"stream", 1)[1]
            raw = raw.lstrip(b"\r\n").rsplit(b"endstream", 1)[0].rstrip(b"\r\n")
            data = None
            if b"FlateDecode" in dictionary:
                try:
                    data = zlib.decompress(raw)
                except zlib.error:
                    try:
                        data = zlib.decompressobj().decompress(raw)
                    except zlib.error:
                        data = None
            if data is None and (b"Tj" in raw or b"TJ" in raw or b"BT" in raw):
                data = raw
            if data is not None and (b"BT" in data or b"Tj" in data or b"TJ" in data):
                texts_found.append((number.decode(), len(data), data))
            if data is not None and b"/Image" not in dictionary and len(data) > 1000:
                pass
    print("объектов-изображений: %d" % images)
    print("потоков с текстовыми операторами: %d" % len(texts_found))
    for number, size, data in texts_found[:3]:
        print("\n--- объект %s (%d байт) ---" % (number, size))
        print(data[:500].decode("latin-1", "replace"))

    # Пытаемся понять, есть ли вообще шрифты и как кодируется текст
    print("\nпоиск шрифтовых объектов:")
    for pattern in (rb"/Font", rb"/F\d+", rb"/Type0", rb"/Type1", rb"/TTF", rb"Identity-H",
                    rb"beginbfchar", rb"beginbfrange", rb"/Differences"):
        print("   %-16s %d" % (pattern.decode(), len(re.findall(pattern, blob))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
