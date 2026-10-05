# -*- coding: utf-8 -*-
"""Проверка облачных копий базы: снимок в чат владельца и восстановление из него.

Это тот самый путь, который использует бот на бесплатном хостинге (Render):
контейнер перезапустился с пустым диском → бот находит закреплённое сообщение
с копией и поднимает из него базу целиком.

Что делает утилита:
  1. делает снимок текущей базы и отправляет его закреплённым сообщением владельцу;
  2. проверяет, что сообщение действительно закреплено (getChat → pinned_message);
  3. скачивает копию обратно и восстанавливает её в ОТДЕЛЬНЫЙ временный файл,
     чтобы убедиться, что данные читаются (живая база не затрагивается);
  4. сравнивает количество пользователей, групп, ДЗ и недель расписания.

Запуск (из корня проекта):
    python tools/cloud_check.py --owner-id 1368878379
    python tools/cloud_check.py --owner-id 1368878379 --db data/bot.db
    python tools/cloud_check.py --owner-id 1368878379 --verify-only   # без отправки
"""

import argparse
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tgbot                                     # noqa: E402
from bot import env_value, read_token            # noqa: E402
from snapshot import SnapshotManager, restore_bytes, restore_from_chat  # noqa: E402
from store import Store                          # noqa: E402

COUNTS = [
    ("users", "SELECT COUNT(*) FROM users"),
    ("groups", "SELECT COUNT(*) FROM groups"),
    ("homework", "SELECT COUNT(*) FROM homework"),
    ("timetable", "SELECT COUNT(*) FROM timetable"),
    ("sent", "SELECT COUNT(*) FROM sent"),
]


def counts_of(db_path):
    """Число записей в ключевых таблицах (или None, если база не читается)."""
    if not os.path.exists(db_path):
        return None
    result = {}
    try:
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            for name, query in COUNTS:
                try:
                    result[name] = connection.execute(query).fetchone()[0]
                except sqlite3.Error:
                    result[name] = "нет таблицы"
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"ошибка": str(error)}
    return result


def main():
    parser = argparse.ArgumentParser(description="Проверка облачных копий базы")
    parser.add_argument("--owner-id", help="Telegram id владельца (чат с копиями)")
    parser.add_argument("--db", default=os.path.join(ROOT, "data", "bot.db"))
    parser.add_argument("--verify-only", action="store_true",
                        help="не отправлять новую копию, только проверить восстановление")
    parser.add_argument("--disaster-test", action="store_true",
                        help="снести базу и восстановить её из чата (как после перезапуска Render)")
    arguments = parser.parse_args()

    token = read_token()
    if not token:
        print("Не найден токен бота (token.txt / BOT_TOKEN).")
        return 2
    owner_id = (arguments.owner_id or env_value("OWNER_ID", "")).strip()
    if not owner_id.isdigit():
        print("Не указан владелец: передайте --owner-id <ваш Telegram id>.")
        return 2
    owner_id = int(owner_id)

    db_path = os.path.abspath(arguments.db)
    print("База:      %s" % db_path)
    print("Владелец:  %s" % owner_id)
    before = counts_of(db_path)
    print("До проверки: %s" % before)

    tg = tgbot.Telegram(token)
    store = Store(db_path)

    if not arguments.verify_only:
        print("\n1. Делаю снимок и отправляю владельцу...")
        manager = SnapshotManager(tg, store, owner_id, logger=print)
        if not manager.save(force=True):
            print("❌ Не удалось сохранить копию: %s" % (manager.last_error or "без деталей"))
            return 1
        print("✅ Копия отправлена, message_id=%s, время: %s"
              % (manager.message_id, manager.last_saved_text()))

        print("\n2. Проверяю, что сообщение закреплено...")
        try:
            chat = tg.call("getChat", {"chat_id": owner_id})
            pinned = chat.get("pinned_message") or {}
            document = pinned.get("document") or {}
            print("   закреплено: %s | файл: %s" % (bool(pinned), document.get("file_name")))
            if not pinned:
                print("⚠️ Закреплённого сообщения нет — на Render восстановление не сработает.")
                return 1
        except Exception as error:
            print("⚠️ Не удалось проверить закрепление: %s" % error)

    print("\n3. Восстанавливаю копию из чата во временный файл (живую базу не трогаю)...")
    handle, temp_path = tempfile.mkstemp(prefix="cloud-check-", suffix=".db")
    os.close(handle)
    os.remove(temp_path)
    try:
        restored = restore_from_chat(tg, owner_id, temp_path, logger=print)
        if not restored:
            print("❌ Восстановление не удалось — в чате нет годной копии.")
            return 1
        after = counts_of(temp_path)
        print("✅ Восстановлено: %s" % after)
        if before and after:
            differences = [name for name, _ in COUNTS
                           if before.get(name) != after.get(name)]
            if differences:
                print("⚠️ Отличия в таблицах: %s" % ", ".join(differences))
                return 1
            print("✅ Данные совпали с текущей базой — копия годная.")
    finally:
        for path in (temp_path, temp_path + "-wal", temp_path + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass

    if arguments.disaster_test:
        print("\n4. Аварийный тест: сношу базу и поднимаю её только из копии в чате...")
        safety = db_path + ".disaster-backup"
        if os.path.exists(db_path):
            with open(db_path, "rb") as source, open(safety, "wb") as target:
                target.write(source.read())
        for path in (db_path, db_path + "-wal", db_path + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass
        print("   база удалена, копия для подстраховки: %s" % os.path.basename(safety))
        if not restore_from_chat(tg, owner_id, db_path, logger=print):
            print("❌ Не удалось поднять базу из копии — возвращаю исходную.")
            if os.path.exists(safety):
                os.replace(safety, db_path)
            return 1
        after = counts_of(db_path)
        print("✅ База поднята из чата: %s" % after)
        if before and after and any(before.get(name) != after.get(name)
                                    for name, _ in COUNTS):
            print("⚠️ Данные отличаются от исходных — возвращаю исходную базу.")
            if os.path.exists(safety):
                os.replace(safety, db_path)
            return 1
        print("✅ Данные совпали — аварийное восстановление работает.")
        try:
            os.remove(safety)
        except OSError:
            pass

    print("\nИТОГ: облачные копии работают — на Render база поднимется после перезапуска.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
