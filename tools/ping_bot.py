# -*- coding: utf-8 -*-
"""Проверка бота «изнутри»: отправляем запрос как владелец и смотрим результат.

Нужно, чтобы убедиться, что облачный бот умеет разбирать источник (например,
распознавать скан PDF) — ответ придёт в чат владельца, а счётчики покажут,
была ли ошибка.

Запуск:
    python tools/ping_bot.py --text "📅 Сегодня"
    python tools/ping_bot.py --university kgasu --text "📅 Сегодня"
"""

import argparse
import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from bot import read_token      # noqa: E402

DEFAULT_URL = "https://schedule-bot-frgz.onrender.com"


def telegram(token, method, params=""):
    url = "https://api.telegram.org/bot%s/%s%s" % (token, method, params)
    request = urllib.request.Request(url, headers={"User-Agent": "ping-bot"})
    with urllib.request.urlopen(request, timeout=45,
                                context=ssl._create_unverified_context()) as response:
        return json.loads(response.read().decode())


def service_json(base):
    request = urllib.request.Request(base + "/service.json", headers={"User-Agent": "ping"})
    with urllib.request.urlopen(request, timeout=90,
                                context=ssl._create_unverified_context()) as response:
        return json.loads(response.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Запрос боту от имени владельца")
    parser.add_argument("--owner-id", default="1368878379")
    parser.add_argument("--text", default="📅 Сегодня")
    parser.add_argument("--university", default="kgasu", choices=("unifirst", "kgasu"))
    parser.add_argument("--url", default=DEFAULT_URL)
    arguments = parser.parse_args()

    token_file = "token.txt" if arguments.university == "unifirst" else "token-kgasu.txt"
    path = "/telegram" if arguments.university == "unifirst" else "/telegram-kgasu"
    token = read_token(token_file=token_file)
    secret = hashlib.sha256(("schedule-bot:" + token).encode()).hexdigest()[:40]
    owner = int(arguments.owner_id)
    base = arguments.url.rstrip("/")

    before = service_json(base)
    print("до запроса: обработано %s, ошибок %s"
          % (before["updates"]["handled"], before["updates"]["errors"]))

    update = {"update_id": int(time.time()) % 1000000,
              "message": {"message_id": 1, "date": int(time.time()),
                          "chat": {"id": owner, "type": "private"},
                          "from": {"id": owner, "is_bot": False, "first_name": "Руслан",
                                   "username": "ruka"},
                          "text": arguments.text}}
    body = json.dumps(update, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": secret,
        "User-Agent": "ping-bot"})
    with urllib.request.urlopen(request, timeout=90,
                                context=ssl._create_unverified_context()) as response:
        print("обновление принято: HTTP %s" % response.status)

    print("жду обработку (распознавание скана занимает до минуты)...")
    for attempt in range(12):
        time.sleep(10)
        data = service_json(base)
        updates = data["updates"]
        handled = updates["handled"] - before["updates"]["handled"]
        errors = updates["errors"] - before["updates"]["errors"]
        print("   %2d с: обработано +%s, ошибок +%s%s"
              % (10 * (attempt + 1), handled, errors,
                 (" | " + updates["last_error"][:100]) if errors else ""))
        if handled or errors:
            if errors:
                print("\n❌ Ошибка обработки: %s" % updates.get("last_error"))
                return 1
            print("\n✅ Бот обработал запрос без ошибок — ответ в чате владельца.")
            return 0
    print("\n⏰ Не дождался обработки — проверьте /service.json вручную.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
