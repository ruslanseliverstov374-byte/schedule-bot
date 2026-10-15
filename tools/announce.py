# -*- coding: utf-8 -*-
"""Объявление пользователям бота: отправляет текст всем, кто им пользуется.

Список берётся из закреплённой копии базы в чате владельца (та же копия, что
бот обновляет каждые полчаса), поэтому объявление доходит до всех, кто есть
в базе, даже если их группа давно не выбиралась.

Запуск:
    python tools/announce.py --university unifirst --text-file data/announce.txt
    python tools/announce.py --university kgasu --text "Текст объявления"
    python tools/announce.py --university unifirst --text-file ... --dry-run
"""

import argparse
import gzip
import json
import os
import ssl
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from bot import ROOT, read_token      # noqa: E402

CONTEXT = ssl._create_unverified_context()
TOKENS = {"unifirst": "token.txt", "kgasu": "token-kgasu.txt"}


def api(token, method, payload, timeout=45):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "announce"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=CONTEXT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "desc": error.read().decode("utf-8", "replace")[:200]}
    except Exception as error:
        return {"ok": False, "desc": str(error)[:200]}


def recipients(token, owner_id):
    """Список tg_id из закреплённой копии базы."""
    chat = api(token, "getChat", {"chat_id": owner_id})
    if not chat.get("ok"):
        return [], "чат недоступен: %s" % str(chat)[:120]
    pinned = (chat["result"].get("pinned_message") or {})
    document = pinned.get("document") or {}
    if not document.get("file_id"):
        return [], "в чате нет закреплённой копии базы"
    info = api(token, "getFile", {"file_id": document["file_id"]})
    if not info.get("ok"):
        return [], "файл копии не получен"
    url = "https://api.telegram.org/file/bot%s/%s" % (token, info["result"]["file_path"])
    try:
        with urllib.request.urlopen(url, timeout=120, context=CONTEXT) as response:
            blob = response.read()
    except Exception as error:
        return [], "копия не скачалась: %s" % str(error)[:100]
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    path = os.path.join(ROOT, "data", "_announce.db")
    with open(path, "wb") as handle:
        handle.write(raw)
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % path.replace(os.sep, "/"), uri=True)
        try:
            rows = connection.execute(
                "SELECT tg_id, first_name, group_title FROM users ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return [], "копия не читается: %s" % error
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
    return rows, ""


def main():
    parser = argparse.ArgumentParser(description="Объявление пользователям бота")
    parser.add_argument("--university", default="unifirst",
                        choices=("unifirst", "kgasu"))
    parser.add_argument("--owner-id", default="1368878379")
    parser.add_argument("--text", default="")
    parser.add_argument("--text-file", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, кому уйдёт, и не отправлять")
    arguments = parser.parse_args()

    text = arguments.text
    if arguments.text_file:
        with open(arguments.text_file, encoding="utf-8") as handle:
            text = handle.read().strip()
    if not text:
        print("Пустой текст объявления.")
        return 2

    token = read_token(token_file=TOKENS[arguments.university])
    if not token:
        print("Нет токена бота (%s)" % TOKENS[arguments.university])
        return 2
    me = api(token, "getMe", {})
    username = (me.get("result") or {}).get("username") if me.get("ok") else "?"
    print("Бот: @%s | объявление длиной %d символов" % (username, len(text)))

    rows, error = recipients(token, int(arguments.owner_id))
    if error:
        print("Не удалось получить список: %s" % error)
        return 1
    print("Получателей: %d" % len(rows))
    for tg_id, name, group in rows:
        print("   %s — %s (%s)" % (name or tg_id, group or "группа не выбрана", tg_id))
    if arguments.dry_run:
        print("\nЭто предпросмотр (--dry-run): ничего не отправлено.")
        return 0

    delivered = failed = 0
    for tg_id, name, _group in rows:
        result = api(token, "sendMessage", {"chat_id": tg_id, "text": text,
                                            "parse_mode": "HTML",
                                            "disable_notification": False})
        if result.get("ok"):
            delivered += 1
            print("   отправлено: %s" % (name or tg_id))
        else:
            failed += 1
            print("   НЕ доставлено %s: %s" % (tg_id, str(result)[:120]))
        time.sleep(0.2)
    print("\nИтог: доставлено %d, ошибок %d" % (delivered, failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
