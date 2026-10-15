# -*- coding: utf-8 -*-
"""Ищем альтернативный источник расписания КГАСУ: ссылки, поддомены, онлайн-сервисы.

Запуск:
    python tools/probe_kgasu_source.py
"""

import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from schedule import kgasu      # noqa: E402

CANDIDATES = [
    "https://rasp.kgasu.ru/",
    "https://schedule.kgasu.ru/",
    "https://timetable.kgasu.ru/",
    "https://raspisanie.kgasu.ru/",
    "https://1c.kgasu.ru/",
    "https://edu.kgasu.ru/",
    "https://lk.kgasu.ru/",
]


def fetch(url, timeout=40):
    request = urllib.request.Request(url, headers={
        "User-Agent": kgasu.USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl._create_unverified_context()) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""
    except Exception as error:
        return None, str(error)[:120].encode()


def main():
    print("1. Все форматы файлов в текущем списке (2026-2027, осень)")
    groups = kgasu.groups()
    extensions = {}
    for item in groups:
        extensions[item["file_ext"]] = extensions.get(item["file_ext"], 0) + 1
    print("   групп: %d, форматы: %s" % (len(groups), extensions))

    print("\n2. Проверяю другие периоды и виды расписания на сайте")
    html = kgasu.fetch_text(kgasu.PAGE, timeout=60)
    for field, title in (("arrFilter_pf[UCH_GOD]", "учебные годы"),
                         ("arrFilter_pf[SEMESTR]", "семестры"),
                         ("arrFilter_pf[TIP_RASP]", "виды расписания")):
        options = kgasu.filter_options(html, field)
        print("   %s: %s" % (title, ", ".join("%s=%s" % (value, name)
                                              for value, name in options[:8])))

    print("\n3. Ссылки на странице расписания (ищу онлайн-расписание)")
    words = ("rasp", "schedule", "timetable", "1c", "online", "онлайн", "электрон")
    links = set(re.findall(r'href="([^"]+)"', html))
    interesting = [link for link in links
                   if any(word in link.lower() for word in words)]
    for link in sorted(interesting)[:12]:
        print("   %s" % link)
    if not interesting:
        print("   ничего похожего на онлайн-расписание не нашлось")
    iframes = re.findall(r'<iframe[^>]+src="([^"]+)"', html)
    for frame in iframes[:5]:
        print("   iframe: %s" % frame)

    print("\n4. Проверяю типовые поддомены")
    for url in CANDIDATES:
        status, body = fetch(url, timeout=20)
        size = len(body) if isinstance(body, bytes) else 0
        title = ""
        if isinstance(body, bytes) and body[:200]:
            found = re.search(rb"<title[^>]*>(.{0,80}?)</title>", body, re.S | re.I)
            if found:
                title = found.group(1).decode("utf-8", "replace").strip()
        print("   %-34s %s %s %s" % (url, status, ("%d КБ" % (size // 1024)) if size else "",
                                     title[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
