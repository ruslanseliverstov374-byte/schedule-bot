# -*- coding: utf-8 -*-
"""Универсальный разведчик сайта: подбирает способ подключения и показывает, что внутри.

Нужен при подключении нового вуза: у учебных сайтов бывают проблемы с TLS
(старые протоколы, сертификаты российских удостоверяющих центров), поэтому
инструмент пробует несколько вариантов и сообщает, какой сработал.

Запуск:
    python tools/probe_url.py https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php
    python tools/probe_url.py <url> --save data/recon/page.html
    python tools/probe_url.py <url> --show 5000
"""

import argparse
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url, context=None, timeout=30, method="GET", data=None, headers=None):
    request_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.status, dict(response.headers), response.read()


def attempts(url):
    """Варианты подключения: обычный, без проверки сертификата, только TLS 1.2, HTTP."""
    yield "обычный HTTPS", None, url
    yield "HTTPS без проверки сертификата", ssl._create_unverified_context(), url
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    yield "HTTPS только TLS 1.2", context, url
    context = ssl.create_default_context()
    context.set_ciphers("DEFAULT@SECLEVEL=1")
    yield "HTTPS с пониженными требованиями к шифрам", context, url
    if url.startswith("https://"):
        yield "обычный HTTP", None, "http://" + url[len("https://"):]


def main():
    parser = argparse.ArgumentParser(description="Разведка сайта: подбор способа подключения")
    parser.add_argument("url")
    parser.add_argument("--save", help="сохранить ответ в файл")
    parser.add_argument("--show", type=int, default=2000, help="сколько символов показать")
    arguments = parser.parse_args()

    for title, context, candidate in attempts(arguments.url):
        try:
            status, headers, data = fetch(candidate, context=context)
        except Exception as error:
            print("❌ %-42s %s: %s" % (title, type(error).__name__,
                                      str(error)[:120]))
            continue
        print("✅ %-42s HTTP %s | %s | %d байт"
              % (title, status, headers.get("Content-Type"), len(data)))
        text = data.decode("utf-8", "replace")
        if arguments.save:
            os.makedirs(os.path.dirname(arguments.save) or ".", exist_ok=True)
            with open(arguments.save, "w", encoding="utf-8") as handle:
                handle.write(text)
            print("   сохранено: %s" % arguments.save)
        print("\n--- первые %d символов ---" % arguments.show)
        print(re.sub(r"\n{3,}", "\n\n", text[:arguments.show]))
        return 0
    print("\nНи один способ не сработал: сайт недоступен из этой сети.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
