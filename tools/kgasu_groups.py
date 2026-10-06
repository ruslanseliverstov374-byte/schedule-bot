# -*- coding: utf-8 -*-
"""Список групп КГАСУ с файлами расписания (сохраняет JSON и печатает таблицу)."""

import json
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
    context = ssl._create_unverified_context()     # у сайта неполная цепочка сертификата
    with urllib.request.urlopen(request, timeout=40, context=context) as response:
        return response.read().decode("utf-8", "replace")


def options(html, name):
    block = re.search(r'name="%s"[^>]*>(.*?)</select>' % re.escape(name), html, re.S)
    if not block:
        return []
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', block.group(1))


def main():
    html = fetch(PAGE)
    print("=== фильтры страницы ===")
    filters = {}
    for name in ("arrFilter_pf[TIP_RASP]", "arrFilter_pf[UCH_GOD]",
                 "arrFilter_pf[SEMESTR]", "arrFilter_pf[KURS]", "arrFilter_pf[INSTITUT]"):
        values = options(html, name)
        filters[name] = values
        print("%-28s %s" % (name, ", ".join("%s=%s" % (title, value)
                                            for value, title in values[:8])))

    def value_of(name, title_part):
        for value, title in filters.get(name, []):
            if title_part.lower() in title.lower():
                return value
        return ""

    params = {
        "arrFilter_pf[TIP_RASP]": value_of("arrFilter_pf[TIP_RASP]", "занятий"),
        "arrFilter_pf[UCH_GOD]": value_of("arrFilter_pf[UCH_GOD]", "2026-2027"),
        "arrFilter_pf[SEMESTR]": value_of("arrFilter_pf[SEMESTR]", "Осенний"),
        "set_filter": "Y",
    }
    print("\n=== запрос расписания занятий 2026-2027, осенний семестр ===")
    print("параметры:", params)
    listing = fetch(PAGE + "?" + urllib.parse.urlencode(params))

    pattern = re.compile(
        r'href="(https://st\.kgasu\.ru/[^"]+\.(?:docx?|xlsx?|pdf))"[^>]*>([^<]{1,120})</a>',
        re.I)
    files = [(re.sub(r"\s+", " ", title).strip(), link) for link, title in pattern.findall(listing)]

    groups = []
    for title, link in files:
        for code in re.split(r"[,\s]+", title):
            code = code.strip()
            if not code:
                continue
            groups.append({
                "name": code,
                "file_url": link,
                "file_name": link.split("/")[-1],
                "file_ext": link.rsplit(".", 1)[-1].lower(),
                "shared_with": [other.strip() for other in re.split(r"[,\s]+", title)
                                if other.strip() and other.strip() != code],
            })

    print("\n=== группы (%d) ===" % len(groups))
    for index, group in enumerate(groups, 1):
        print("%3d. %-12s %-28s %s"
              % (index, group["name"], group["file_name"], group["file_ext"]))

    with open("data/recon/kgasu_groups.json", "w", encoding="utf-8") as handle:
        json.dump({"page": PAGE, "params": params, "groups": groups}, handle,
                  ensure_ascii=False, indent=1)
    print("\nсохранено: data/recon/kgasu_groups.json")
    by_ext = {}
    for group in groups:
        by_ext[group["file_ext"]] = by_ext.get(group["file_ext"], 0) + 1
    print("по форматам:", by_ext)
    return 0


if __name__ == "__main__":
    sys.exit(main())
