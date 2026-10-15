# -*- coding: utf-8 -*-
"""Диагностика файла расписания КГАСУ: что сайт реально отдаёт.

Запуск:
    python tools/check_kgasu_file.py 26ЗК01
"""

import ssl
import sys
import urllib.request

sys.path.insert(0, ".")
from schedule import docparse, docxparse, kgasu      # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def describe(blob, name):
    head = blob[:8].hex(" ")
    print("   размер: %d байт" % len(blob))
    print("   первые байты: %s" % head)
    if blob[:4] == b"PK\x03\x04":
        print("   похоже на: .docx (zip)")
    elif blob[:8] == bytes.fromhex("d0cf11e0a1b11ae1"):
        print("   похоже на: старый .doc (OLE2)")
    elif blob[:5] == b"{\\rtf":
        print("   похоже на: RTF")
    elif blob[:1] == b"<" or b"<html" in blob[:400].lower():
        print("   похоже на: HTML-страницу (а не файл расписания)")
        text = blob[:400].decode("utf-8", "replace")
        print("   начало: %s" % text.replace("\n", " ")[:200])
    else:
        print("   формат не определён")


def main():
    group_query = sys.argv[1] if len(sys.argv) > 1 else "26ЗК01"
    print("1. Список групп КГАСУ...")
    groups = kgasu.groups()
    found = kgasu.find_group(groups, group_query)
    if not found:
        print("   группа %s не найдена (всего групп: %d)" % (group_query, len(groups)))
        return 1
    print("   группа: %s | файл: %s" % (found["name"], found["file_url"]))

    print("\n2. Скачиваю файл напрямую (как браузер)...")
    request = urllib.request.Request(found["file_url"], headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": kgasu.PAGE,
    })
    try:
        with urllib.request.urlopen(request, timeout=60,
                                    context=ssl._create_unverified_context()) as response:
            blob = response.read()
            print("   HTTP %s | Content-Type: %s | Content-Length: %s"
                  % (response.status, response.headers.get("Content-Type"),
                     response.headers.get("Content-Length")))
    except Exception as error:
        print("   не скачалось: %s" % error)
        return 1
    describe(blob, found["file_name"])

    print("\n3. Пробую разобрать тем же кодом, что и бот...")
    for parser_name, parser in (("docxparse", lambda: docxparse.docx_tables(blob)),
                                ("docparse", lambda: docparse.doc_text(blob))):
        try:
            result = parser()
            if parser_name == "docxparse":
                print("   %s: таблиц %d" % (parser_name, len(result)))
            else:
                print("   %s: символов текста %d" % (parser_name, len(result)))
        except Exception as error:
            print("   %s: ошибка — %s" % (parser_name, str(error)[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
