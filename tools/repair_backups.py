# -*- coding: utf-8 -*-
"""Восстанавливает базы ботов: собирает исправленные копии и закрепляет их в чатах.

Зачем: 9 октября боты перепутали копии (оба писали файл с одним именем), поэтому
бот КГАСУ восстановился из базы ПГУФКСиТ — показывал чужие группы, и «26зк01»
не находилась. Этот скрипт собирает правильные базы из того, что уцелело, и
кладёт их в чаты ботов закреплёнными сообщениями: при следующем запуске сервиса
боты поднимутся уже из своих баз.

Запуск (сначала посмотреть план, потом применить):
    python tools/repair_backups.py --owner-id 1368878379
    python tools/repair_backups.py --owner-id 1368878379 --apply
"""

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, ".")
from bot import ROOT, read_token      # noqa: E402
from store import Store               # noqa: E402

#: Кого вернуть в базу: tg_id -> (имя, username, группа, админ)
RECOVER_USERS = {
    "unifirst": {
        1368878379: ("Руслан", "", "26282", 1),
        5508518097: ("Григорий", "", "26101", 0),
    },
    "kgasu": {
        1368878379: ("Руслан", "", "26ЗК01", 1),
    },
}

#: Откуда брать целые части: своя локальная база + группы
SOURCES = {
    "unifirst": {"token": "token.txt", "db": os.path.join(ROOT, "data", "bot.db")},
    "kgasu": {"token": "token-kgasu.txt", "db": os.path.join(ROOT, "data", "kgasu.db")},
}


def api(token, method, payload):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "repair-backups"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "desc": error.read().decode("utf-8", "replace")[:200]}


def api_upload(token, chat_id, path, caption):
    """Отправляет файл копии как документ (multipart/form-data)."""
    boundary = "----repair%s" % os.urandom(8).hex()
    with open(path, "rb") as handle:
        blob = handle.read()
    parts = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption),
                        ("disable_notification", "true")):
        parts.append(("--%s\r\n" % boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(("--%s\r\n" % boundary).encode())
    parts.append(('Content-Disposition: form-data; name="document"; filename="%s"\r\n'
                  % os.path.basename(path)).encode())
    parts.append(b"Content-Type: application/gzip\r\n\r\n")
    parts.append(blob)
    parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendDocument" % token, data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "User-Agent": "repair-backups"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": error.code,
                "desc": error.read().decode("utf-8", "replace")[:200]}


def build_database(bot_name, target_path):
    """Собирает исправленную базу: группы из локальной копии + нужные пользователи."""
    source_path = SOURCES[bot_name]["db"]
    repaired = os.path.join(ROOT, "data", "repair-%s.db" % bot_name)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(repaired + suffix):
            os.remove(repaired + suffix)
    if os.path.exists(source_path):
        shutil.copy2(source_path, repaired)
        print("   основа: %s" % source_path)
    else:
        print("   исходной базы нет — соберу пустую")

    store = Store(repaired)
    store.set_meta("provider", bot_name)
    for tg_id, (name, username, group, admin) in RECOVER_USERS[bot_name].items():
        row = store.get_user(tg_id)
        if row:
            # Имя ставим заново: раньше служебные сообщения о копии базы
            # записывали владельцу имя бота («Расписание ГУФКСиТ»).
            store.update_user(tg_id, first_name=name,
                              username=row.get("username") or username,
                              group_title=row.get("group_title") or group,
                              is_admin=admin or row.get("is_admin"))
        else:
            store.ensure_user(tg_id, username, name)
            store.update_user(tg_id, group_title=group, is_admin=admin)
    # Запись о самом боте в списке пользователей не нужна.
    me = api(read_token(token_file=SOURCES[bot_name]["token"]), "getMe", {})
    bot_id = (me.get("result") or {}).get("id") if me.get("ok") else None
    if bot_id and bot_id != 0:
        store.delete_user(bot_id)
    users = store.count_users()
    groups = store.groups_count()
    store.set_meta("snapshot_users", str(users))
    store.execute("DELETE FROM meta WHERE key IN ('snapshot_message_id','snapshot_saved_at')")
    print("   получилось: пользователей %d, групп %d" % (users, groups))
    for row in store.all_users():
        print("      id=%s имя=%s группа=%s админ=%s"
              % (row["tg_id"], row["first_name"], row["group_title"], row["is_admin"]))
    return repaired


def pack(path):
    """Собирает копию тем же способом, что и бот.

    Важно: нельзя просто прочитать файл базы — часть изменений может лежать в
    журнале WAL, и копия окажется устаревшей. backup_bytes делает согласованный
    снимок через SQLite и сжимает его.
    """
    from snapshot import backup_bytes

    packed = path + ".gz"
    blob = backup_bytes(path)
    with open(packed, "wb") as handle:
        handle.write(blob)
    return packed


def verify_pinned(token, owner_id, expected_users):
    """Скачивает закреплённую копию обратно и проверяет, что в ней нужные данные."""
    chat = api(token, "getChat", {"chat_id": owner_id})
    pinned = ((chat.get("result") or {}).get("pinned_message") or {})
    document = pinned.get("document") or {}
    if not document.get("file_id"):
        return False, "в чате нет закреплённого файла"
    info = api(token, "getFile", {"file_id": document["file_id"]})
    if not info.get("ok"):
        return False, "файл не получен: %s" % str(info)[:120]
    url = "https://api.telegram.org/file/bot%s/%s" % (token, info["result"]["file_path"])
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read()
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    check_path = os.path.join(ROOT, "data", "_verify.db")
    with open(check_path, "wb") as handle:
        handle.write(raw)
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % check_path.replace(os.sep, "/"),
                                     uri=True)
        try:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            groups = connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
            provider = ""
            try:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='provider'").fetchone()
                provider = row[0] if row else ""
            except sqlite3.Error:
                pass
        finally:
            connection.close()
        ok = users >= expected_users
        return ok, "в копии пользователей %d (ждали %d), групп %d, вуз %s" % (
            users, expected_users, groups, provider or "не указан")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(check_path + suffix)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Восстановление баз ботов")
    parser.add_argument("--owner-id", required=True, help="Telegram ID владельца")
    parser.add_argument("--apply", action="store_true",
                        help="отправить и закрепить копии (иначе только показать план)")
    arguments = parser.parse_args()
    owner_id = int(arguments.owner_id)

    for bot_name in ("unifirst", "kgasu"):
        token = read_token(token_file=SOURCES[bot_name]["token"])
        print("=" * 60)
        print("Бот: %s" % bot_name)
        if not token:
            print("   нет токена (%s)" % SOURCES[bot_name]["token"])
            continue
        me = api(token, "getMe", {})
        print("   бот: @%s" % (me.get("result", {}).get("username") if me.get("ok") else me))
        repaired = build_database(bot_name, None)
        packed = pack(repaired)
        print("   копия: %s (%.1f КБ)" % (os.path.basename(packed),
                                          os.path.getsize(packed) / 1024))
        if not arguments.apply:
            print("   план: отправить файл в чат %d и закрепить его" % owner_id)
            continue
        chat = api(token, "getChat", {"chat_id": owner_id})
        old_pinned = ((chat.get("result") or {}).get("pinned_message") or {}).get("message_id")
        caption = ("💾 Копия базы расписания (восстановлено %s UTC)"
                   % datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
        sent = api_upload(token, owner_id, packed, caption)
        if not sent.get("ok"):
            print("   не удалось отправить копию: %s" % str(sent)[:200])
            continue
        message_id = (sent.get("result") or {}).get("message_id")
        pinned = api(token, "pinChatMessage",
                     {"chat_id": owner_id, "message_id": message_id,
                      "disable_notification": True})
        print("   отправлено сообщение %s, закрепление: %s"
              % (message_id, "ок" if pinned.get("ok") else pinned))
        if old_pinned and old_pinned != message_id:
            removed = api(token, "deleteMessage",
                          {"chat_id": owner_id, "message_id": old_pinned})
            print("   старая копия (%s) удалена: %s"
                  % (old_pinned, "ок" if removed.get("ok") else removed))
        expected = len(RECOVER_USERS[bot_name])
        ok, detail = verify_pinned(token, owner_id, expected)
        print("   проверка закреплённой копии: %s — %s"
              % ("ок" if ok else "ПРОБЛЕМА", detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
