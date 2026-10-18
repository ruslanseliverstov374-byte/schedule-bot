# -*- coding: utf-8 -*-
"""Проверка живого бота КГАСУ: отправить запрос через вебхук и дождаться ответа."""

import hashlib
import json
import ssl
import sys
import time
import urllib.request

CONTEXT = ssl._create_unverified_context()
BASE = "https://schedule-bot-frgz.onrender.com"
OWNER = 1368878379


def service():
    request = urllib.request.Request(BASE + "/service.json", headers={"User-Agent": "diag"})
    with urllib.request.urlopen(request, timeout=150, context=CONTEXT) as response:
        return json.loads(response.read().decode())


def send(university, text):
    token_file = "token.txt" if university == "unifirst" else "token-kgasu.txt"
    path = "/telegram" if university == "unifirst" else "/telegram-kgasu"
    token = open(token_file, encoding="utf-8").read().strip()
    secret = hashlib.sha256(("schedule-bot:" + token).encode()).hexdigest()[:40]
    update = {"update_id": int(time.time()) % 900000 + 100,
              "message": {"message_id": 3, "date": int(time.time()),
                          "chat": {"id": OWNER, "type": "private"},
                          "from": {"id": OWNER, "is_bot": False, "first_name": "Руслан"},
                          "text": text}}
    body = json.dumps(update, ensure_ascii=False).encode()
    request = urllib.request.Request(BASE + path, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": secret,
        "User-Agent": "diag"})
    with urllib.request.urlopen(request, timeout=90, context=CONTEXT) as response:
        return response.status


def main():
    university = sys.argv[1] if len(sys.argv) > 1 else "kgasu"
    text = sys.argv[2] if len(sys.argv) > 2 else "📅 Сегодня"
    path = "/telegram" if university == "unifirst" else "/telegram-kgasu"
    before = service().get("bots_updates", {}).get(path, {})
    print("до: handled=%s errors=%s" % (before.get("handled"), before.get("errors")))
    print("POST:", send(university, text))
    started = time.time()
    for step in range(24):
        time.sleep(10)
        now = service().get("bots_updates", {}).get(path, {})
        handled = now.get("handled", 0) - before.get("handled", 0)
        errors = now.get("errors", 0) - before.get("errors", 0)
        if handled:
            print("%.0f с: ОБРАБОТАНО (handled +%s, errors +%s)"
                  % (time.time() - started, handled, errors))
            return 0
        if errors:
            print("%.0f с: ОШИБКА: %s" % (time.time() - started, now.get("last_error")))
            return 1
        if step % 3 == 0:
            print("%.0f с: в работе (очередь %s)" % (time.time() - started,
                                                    now.get("queue")))
    print("не дождался за 4 минуты")
    return 1


if __name__ == "__main__":
    sys.exit(main())
