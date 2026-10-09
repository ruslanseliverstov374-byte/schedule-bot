# -*- coding: utf-8 -*-
"""Показывает, что лежит в закреплённых копиях баз у ботов.

Полезно, когда облако перезапустилось и кажется, что данные пропали: скрипт
скачивает копию прямо из чата (токеном бота), распаковывает и читает содержимое —
сколько пользователей, групп и домашки внутри.

Запуск:
    python tools/check_backups.py --owner-id 1368878379
"""

import argparse
import gzip
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, ".")
from bot import ROOT, read_token      # noqa: E402

BOTS = (("token.txt", "ПГУФКСиТ"), ("token-kgasu.txt", "КГАСУ"))


def api(token, method, params="", timeout=60):
    url = "https://api.telegram.org/bot%s/%s%s" % (token, method, params)
    request = urllib.request.Request(url, headers={"User-Agent": "backup-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "desc": error.read().decode("utf-8", "replace")[:200]}
    except Exception as error:
        return {"ok": False, "desc": str(error)[:200]}


def unpack(data):
    """Распаковывает копию: Bot API отдаёт файл, сжатый gzip или zip."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data), "gzip"
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = archive.namelist()[0]
            return archive.read(name), "zip (%s)" % name
    return data, "как есть"


def inspect(path):
    """Читает базу копии: сколько пользователей, групп и домашки внутри."""
    connection = sqlite3.connect("file:%s?mode=ro" % path.replace(os.sep, "/"), uri=True)
    try:
        def count(sql):
            try:
                return connection.execute(sql).fetchone()[0]
            except sqlite3.Error:
                return 0
        result = {
            "users": count("SELECT COUNT(*) FROM users"),
            "groups": count("SELECT COUNT(*) FROM groups"),
            "homework": count("SELECT COUNT(*) FROM homework"),
            "people": connection.execute(
                "SELECT tg_id, first_name, group_title FROM users LIMIT 8").fetchall(),
        }
    finally:
        connection.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="Что лежит в закреплённых копиях баз")
    parser.add_argument("--owner-id", required=True, help="Telegram ID владельца (чат с ботом)")
    arguments = parser.parse_args()
    owner_id = int(arguments.owner_id)

    for token_file, title in BOTS:
        token = read_token(token_file=token_file)
        print("=" * 60)
        if not token:
            print("%s: нет файла %s" % (title, token_file))
            continue
        chat = api(token, "getChat?chat_id=%d" % owner_id)
        if not chat.get("ok"):
            print("%s: чат недоступен (%s)" % (title, str(chat)[:120]))
            continue
        pinned = chat["result"].get("pinned_message") or {}
        document = pinned.get("document") or {}
        print("%s: закреплённое сообщение #%s (%s)"
              % (title, pinned.get("message_id"), document.get("file_name") or "без файла"))
        if not document:
            continue
        info = api(token, "getFile?file_id=%s" % document["file_id"])
        if not info.get("ok"):
            print("   файл не получен: %s" % str(info)[:140])
            continue
        url = "https://api.telegram.org/file/bot%s/%s" % (token, info["result"]["file_path"])
        with urllib.request.urlopen(url, timeout=90) as response:
            blob = response.read()
        raw, how = unpack(blob)
        print("   скачано %d КБ, распаковано (%s) -> %d КБ"
              % (len(blob) // 1024, how, len(raw) // 1024))
        path = os.path.join(ROOT, "data", "_backup-check.db")
        with open(path, "wb") as handle:
            handle.write(raw)
        try:
            data = inspect(path)
            print("   В КОПИИ: пользователей %d, групп %d, домашки %d"
                  % (data["users"], data["groups"], data["homework"]))
            for row in data["people"]:
                print("      id=%s имя=%s группа=%s" % row)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
