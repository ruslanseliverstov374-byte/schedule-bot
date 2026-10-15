# -*- coding: utf-8 -*-
"""Ищет все файлы расписания для группы КГАСУ во всех периодах и форматах.

Запуск:
    python tools/find_kgasu_files.py 26ЗК01
"""

import re
import sys
import urllib.parse

sys.path.insert(0, ".")
from schedule import kgasu      # noqa: E402

GROUPS = ("2026-2027", "2025-2026", "2024-2025")
SEMESTERS = ("Осенний", "Весенний")
TYPES = ("Расписание занятий", "Расписание зачетов",
         "Расписание консультаций и экзаменов")


def main():
    query = (sys.argv[1] if len(sys.argv) > 1 else "26ЗК01").upper()
    normalized = query.replace("З", "Z").replace("з", "z")
    html = kgasu.fetch_text(kgasu.PAGE, timeout=60)

    print("Ищу %s во всех периодах (это займёт минуту)..." % query)
    for tip in TYPES:
        for year in GROUPS:
            for semester in SEMESTERS:
                params = {
                    "arrFilter_pf[TIP_RASP]": kgasu.filter_value(
                        kgasu.filter_options(html, "arrFilter_pf[TIP_RASP]"), tip),
                    "arrFilter_pf[UCH_GOD]": kgasu.filter_value(
                        kgasu.filter_options(html, "arrFilter_pf[UCH_GOD]"), year),
                    "arrFilter_pf[SEMESTR]": kgasu.filter_value(
                        kgasu.filter_options(html, "arrFilter_pf[SEMESTR]"), semester),
                    "set_filter": "Y",
                }
                base = kgasu.PAGE + "?" + urllib.parse.urlencode(params)
                page_number = 1
                found = []
                while page_number <= 12:
                    url = base if page_number == 1 else "%s&PAGEN_1=%d" % (base, page_number)
                    try:
                        listing = kgasu.fetch_text(url, timeout=60)
                    except Exception:
                        break
                    links = re.findall(
                        r'href="(https://st\.kgasu\.ru/[^"]+\.(?:docx?|pdf))"[^>]*>([^<]{1,120})</a>',
                        listing, re.I)
                    if not links:
                        break
                    for link, title in links:
                        clean = re.sub(r"\s+", " ", title).strip().upper()
                        if query in clean or normalized in clean.replace("З", "Z"):
                            found.append((clean, link))
                    page_number += 1
                if found:
                    print("\n%s / %s / %s:" % (tip, year, semester))
                    for title, link in found:
                        print("   %-28s %s" % (title, link))
    return 0


if __name__ == "__main__":
    sys.exit(main())
