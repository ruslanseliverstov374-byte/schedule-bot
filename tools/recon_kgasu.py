# -*- coding: utf-8 -*-
"""Разведка расписания КГАСУ: список групп и формат файлов расписания."""

import re
import ssl
import sys
import urllib.parse
import urllib.request

BASE = "https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PARAMS = {
    "arrFilter_pf[TIP_RASP]": "107",     # Расписание занятий
    "arrFilter_pf[UCH_GOD]": "294",      # 2026-2027
    "arrFilter_pf[SEMESTR]": "95",       # Осенний
    "set_filter": "Y",
}


def fetch(url, timeout=40):
    request = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "*/*"})
    context = ssl._create_unverified_context()      # сайт отдаёт неполную цепочку
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.status, dict(response.headers), response.read()


def main():
    url = BASE + "?" + urllib.parse.urlencode(PARAMS)
    status, headers, data = fetch(url)
    text = data.decode("utf-8", "replace")
    print("страница фильтра: HTTP %s, %d байт" % (status, len(data)))

    # Ссылки вида: <a href="https://st.kgasu.ru/.../26IM01.doc">26ИМ01</a>
    pattern = re.compile(
        r'href="(https://st\.kgasu\.ru/[^"]+\.(?:docx?|xlsx?|pdf))"[^>]*>([^<]{1,80})</a>',
        re.I)
    entries = [(re.sub(r"\s+", " ", title).strip(), link)
               for link, title in pattern.findall(text)]

    print("файлов расписания найдено: %d" % len(entries))
    for title, link in entries[:12]:
        print("   %-32s %s" % (title, link.split("/")[-1]))

    extensions = {}
    for _, link in entries:
        ext = link.rsplit(".", 1)[-1].lower()
        extensions[ext] = extensions.get(ext, 0) + 1
    print("расширения:", extensions)
    print("всего групп в списке: %d" % sum(len(title.split(",")) for title, _ in entries))

    saved = []
    for wanted in (".doc", ".docx"):
        sample = next((link for _, link in entries
                       if link.lower().endswith(wanted)), None)
        if not sample:
            continue
        name = sample.split("/")[-1]
        print("\nпроверяю файл: %s" % name)
        _, _, blob = fetch(sample)
        head = blob[:16]
        if head.startswith(b"PK"):
            kind = "docx/xlsx (zip) — разбирается стандартной библиотекой"
        elif head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            kind = ".doc (старый OLE-формат) — нужен отдельный разбор"
        elif b"{\\rtf" in blob[:200].lower():
            kind = "RTF — разбирается как текст"
        elif b"<html" in blob[:2000].lower() or b"<table" in blob[:4000].lower():
            kind = "HTML под расширением .doc — разбирается как таблица"
        else:
            kind = "неизвестный формат"
        print("   размер: %d байт | начало: %r" % (len(blob), head))
        print("   формат: %s" % kind)
        path = "data/recon/kgasu_" + name
        with open(path, "wb") as handle:
            handle.write(blob)
        saved.append(path)
        print("   сохранено: %s" % path)
    return saved


if __name__ == "__main__":
    sys.exit(main())
