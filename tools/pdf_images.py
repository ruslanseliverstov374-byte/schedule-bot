# -*- coding: utf-8 -*-
"""Достаёт картинки (страницы скана) из PDF-расписания КГАСУ.

Запуск:
    python tools/pdf_images.py <ссылка-или-файл> [папка]
"""

import os
import re
import ssl
import sys
import urllib.request

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


def jpeg_size(blob):
    """Размер картинки из JPEG-заголовка (ширина, высота)."""
    index = 2
    while index < len(blob) - 9:
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = int.from_bytes(blob[index + 5:index + 7], "big")
            width = int.from_bytes(blob[index + 7:index + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(blob[index + 2:index + 4], "big")
        index += 2 + length
    return 0, 0


def main():
    source = sys.argv[1]
    target_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "kgasu-scan")
    os.makedirs(target_dir, exist_ok=True)
    blob = load(source)
    print("файл: %d байт" % len(blob))

    objects = re.findall(rb"(\d+)\s+0\s+obj(.*?)endobj", blob, re.S)
    saved = []
    for number, body in objects:
        dictionary = body.split(b"stream", 1)[0]
        if b"/Image" not in dictionary or b"stream" not in body:
            continue
        raw = body.split(b"stream", 1)[1].lstrip(b"\r\n").rsplit(b"endstream", 1)[0]
        raw = raw.rstrip(b"\r\n")
        if raw[:2] != b"\xff\xd8":
            print("   объект %s: не JPEG, пропускаю" % number.decode())
            continue
        width, height = jpeg_size(raw)
        name = os.path.join(target_dir, "page-%s.jpg" % number.decode())
        with open(name, "wb") as handle:
            handle.write(raw)
        saved.append(name)
        print("   страница: %s — %dx%d, %d КБ" % (name, width, height, len(raw) // 1024))
    print("сохранено страниц: %d" % len(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
