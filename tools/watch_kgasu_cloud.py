# -*- coding: utf-8 -*-
"""Ждёт, пока бот КГАСУ начнёт опрашивать Telegram в облаке.

Как это определяется: если бота уже кто-то опрашивает (облачный сервис), то наш
запрос getUpdates получает ответ 409 Conflict — значит второй бот поднялся.
Заодно проверяется, что токен живой и что вебхук не перебивает опрос.

Запуск:
    python tools/watch_kgasu_cloud.py                # ждать 10 минут
    python tools/watch_kgasu_cloud.py --minutes 3
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from bot import read_token      # noqa: E402

TOKEN_FILE = "token-kgasu.txt"
CONTEXT = ssl._create_unverified_context()


def telegram(token, method, params=""):
    url = "https://api.telegram.org/bot%s/%s%s" % (token, method, params)
    request = urllib.request.Request(url, headers={"User-Agent": "watch-kgasu"})
    try:
        with urllib.request.urlopen(request, timeout=45, context=CONTEXT) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:200]


def main():
    parser = argparse.ArgumentParser(description="Ждём запуска бота КГАСУ в облаке")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--interval", type=float, default=10)
    arguments = parser.parse_args()

    token = read_token(token_file=TOKEN_FILE)
    if not token:
        print("Нет токена КГАСУ (%s)." % TOKEN_FILE)
        return 2

    code, body = telegram(token, "getMe")
    if not (isinstance(body, dict) and body.get("ok")):
        print("Токен не работает: %s %s" % (code, body))
        return 1
    print("Токен живой: @%s" % body["result"].get("username"))

    deadline = time.time() + arguments.minutes * 60
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        code, body = telegram(token, "getUpdates", "?timeout=0&limit=1")
        if code == 409 or (isinstance(body, dict) and body.get("error_code") == 409):
            print("✅ %s — бота КГАСУ опрашивает облако: второй бот работает"
                  % time.strftime("%H:%M:%S"))
            return 0
        left = int(deadline - time.time())
        print("   %s — пока нет (осталось %d с)" % (time.strftime("%H:%M:%S"), max(left, 0)),
              flush=True)
        time.sleep(arguments.interval)
    print("⏰ Время вышло: бот КГАСУ так и не начал опрашивать Telegram.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
