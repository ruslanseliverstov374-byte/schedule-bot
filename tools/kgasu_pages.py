# -*- coding: utf-8 -*-
"""Проверка пагинации страницы расписаний КГАСУ: сколько всего файлов на самом деле."""

import re
import ssl
import sys
import urllib.parse
import urllib.request

PAGE = "https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=60,
                                context=ssl._create_unverified_context()) as response:
        return response.read().decode("utf-8", "replace")


def main():
    html = fetch(PAGE)
    params = {
        "arrFilter_pf[TIP_RASP]": "107",
        "arrFilter_pf[UCH_GOD]": "294",
        "arrFilter_pf[SEMESTR]": "95",
        "set_filter": "Y",
    }
    base = PAGE + "?" + urllib.parse.urlencode(params)
    first = fetch(base)
    pattern = re.compile(
        r'href="(https://st\.kgasu\.ru/[^"]+\.(?:docx?|xlsx?|pdf))"[^>]*>([^<]{1,120})</a>',
        re.I)

    def links(page_html):
        return [(title.strip(), link) for link, title in pattern.findall(page_html)]

    page_one = links(first)
    print("страница 1: файлов %d" % len(page_one))

    # Признаки пагинации Bitrix: PAGEN_1=2, «Следующая», номера страниц
    pagen = sorted({int(value) for value in
                    re.findall(r"PAGEN_1=(\d+)", first) if value.isdigit()})
    print("номера страниц в разметке:", pagen[:10])
    print("есть слово «Следующая»:", "следующая" in first.lower())

    all_links = list(page_one)
    for number in range(2, (max(pagen) if pagen else 1) + 1):
        url = base + "&PAGEN_1=%d" % number
        page_html = fetch(url)
        extra = links(page_html)
        print("страница %d: файлов %d" % (number, len(extra)))
        all_links.extend(extra)
        if not extra:
            break

    unique = {}
    for title, link in all_links:
        unique[link] = title
    print("\nвсего уникальных файлов: %d" % len(unique))
    groups = set()
    for link, title in unique.items():
        for code in re.split(r"[,\s]+", title):
            if code:
                groups.add(code)
    print("групп всего: %d" % len(groups))

    # Ищем файлы, где встречается наша группа
    print("\nфайлы с группой 26ЗК01з:")
    for link, title in unique.items():
        if "26ЗК01" in title.upper():
            print("   %-40s %s" % (title, link.split("/")[-1]))

    print("\nвсе файлы, где в названии есть «ЗК»:")
    for link, title in sorted(unique.items(), key=lambda item: item[1]):
        if "ЗК" in title.upper():
            print("   %-40s %s" % (title, link.split("/")[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
