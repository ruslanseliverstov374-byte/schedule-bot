# -*- coding: utf-8 -*-
"""Разведка источника расписания.

Скачивает URL и помогает понять, что там внутри: тип, размер, куски текста,
ссылки и (для JS-бандлов) строковые литералы с путями/эндпоинтами.

Примеры:
    python tools/probe.py https://timetable.unifirst.ru/
    python tools/probe.py https://timetable.unifirst.ru/assets/index-BxEAb22-.js --js
"""

import argparse
import os
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url, timeout=40):
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read()


def string_literals(text):
    """Все строковые литералы в JS: '...', "...", `...`."""
    found = set()
    for match in re.finditer(r"""(['"`])((?:\\.|(?!\1).){2,200})\1""", text):
        found.add(match.group(2))
    return found


def interesting(literals, keywords):
    result = []
    for item in literals:
        low = item.lower()
        if any(key in low for key in keywords):
            result.append(item)
    return sorted(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--js", action="store_true", help="разбирать как JS-бандл")
    parser.add_argument("--save", help="куда сохранить сырой ответ")
    parser.add_argument("--show", type=int, default=2500, help="сколько символов текста показать")
    args = parser.parse_args()

    status, headers, data = fetch(args.url)
    print("STATUS:", status)
    print("CONTENT-TYPE:", headers.get("Content-Type"))
    print("BYTES:", len(data))

    if args.save:
        os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
        with open(args.save, "wb") as handle:
            handle.write(data)
        print("SAVED:", args.save)

    text = data.decode("utf-8", "replace")

    if args.js:
        literals = string_literals(text)
        print("LITERALS:", len(literals))
        for title, keywords in [
            ("API-пути", ["/api", "/json", "graphql", "endpoint", "baseurl", "base_url"]),
            ("маршруты", ["calendar", "week", "group", "timetable", "schedule", "teacher"]),
            ("внешние", ["http://", "https://"]),
        ]:
            hits = interesting(literals, keywords)
            print("\n--- %s (%d) ---" % (title, len(hits)))
            for hit in hits[:120]:
                print("   ", hit[:160])
    else:
        print("\n--- первые %d символов ---" % args.show)
        print(text[:args.show])


if __name__ == "__main__":
    sys.exit(main())
