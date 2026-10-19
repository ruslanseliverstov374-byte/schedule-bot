# -*- coding: utf-8 -*-
"""Воспроизводит запуск Планёрки на копии облачной базы бота.

Скачивает закреплённую копию базы из Telegram и пытается поднять на ней
PlannerService — так видны ошибки миграции, которые в Render уходят в лог.
"""

import gzip
import json
import os
import ssl
import sys
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot import read_token      # noqa: E402
from planner.service import PlannerService      # noqa: E402

CONTEXT = ssl._create_unverified_context()
OWNER = 1368878379
TARGET = os.path.join(ROOT, "data", "cloud-copy.db")


def api(token, method):
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        headers={"User-Agent": "repro"})
    with urllib.request.urlopen(request, timeout=90, context=CONTEXT) as response:
        return json.loads(response.read().decode())


def download():
    token = read_token(token_file="token.txt")
    chat = api(token, "getChat?chat_id=%d" % OWNER)
    pinned = ((chat.get("result") or {}).get("pinned_message") or {})
    document = pinned.get("document") or {}
    info = api(token, "getFile?file_id=%s" % document["file_id"])
    url = "https://api.telegram.org/file/bot%s/%s" % (token,
                                                      info["result"]["file_path"])
    with urllib.request.urlopen(url, timeout=120, context=CONTEXT) as response:
        blob = response.read()
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    with open(TARGET, "wb") as handle:
        handle.write(raw)
    print("копия базы скачана: %d КБ" % (len(raw) // 1024))
    return len(raw) // 1024


def main():
    download()
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(TARGET + suffix)
        except OSError:
            pass
    try:
        service = PlannerService(
            db_path=TARGET, schedule_bot_path=ROOT, password=None,
            owner_name="Владелец", default_groups="unifirst:26282",
            logger=print)
        print("аккаунты до:", service.store.accounts())
        result = service.ensure_accounts()
        print("ensure_accounts:", result)
        print("аккаунты после:", service.store.accounts())
        state = service.app.state(1)
        print("state: аккаунт", state.get("account"), "| пар сегодня",
              len((state.get("today") or {}).get("pairs", [])),
              "| групп", [item.get("code") for item in state.get("groups") or []])
        service.app.store.close()
    except Exception:
        print("ОШИБКА:")
        traceback.print_exc()


if __name__ == "__main__":
    main()
