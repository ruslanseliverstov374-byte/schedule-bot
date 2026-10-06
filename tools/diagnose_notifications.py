# -*- coding: utf-8 -*-
"""Разбор ночных уведомлений: что и когда бот отправлял.

Скачивает закреплённую копию базы из чата владельца и показывает журнал:
какие напоминания уходили, сколько раз, во сколько, какие «изменения расписания»
бот находил и какие настройки стоят у пользователей.

Запуск (из корня проекта):
    python tools/diagnose_notifications.py --owner-id 1368878379
"""

import argparse
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tgbot                                     # noqa: E402
from bot import read_token                       # noqa: E402


def table(connection, query, params=()):
    try:
        cursor = connection.execute(query, params)
        columns = [item[0] for item in cursor.description]
        return columns, cursor.fetchall()
    except sqlite3.Error as error:
        return None, str(error)


def show(connection, title, query, params=(), limit=25):
    print("\n== %s ==" % title)
    columns, rows = table(connection, query, params)
    if columns is None:
        print("   ошибка:", rows)
        return
    print("   " + " | ".join(columns))
    if not rows:
        print("   (пусто)")
        return
    for row in rows[:limit]:
        print("   " + " | ".join(str(value) for value in row))
    if len(rows) > limit:
        print("   … ещё %d строк" % (len(rows) - limit))


def main():
    parser = argparse.ArgumentParser(description="Разбор уведомлений бота")
    parser.add_argument("--owner-id", required=True)
    arguments = parser.parse_args()

    token = read_token()
    if not token:
        print("Не найден токен бота (token.txt).")
        return 2
    owner_id = int(arguments.owner_id)

    telegram = tgbot.Telegram(token)
    chat = telegram.call("getChat", {"chat_id": owner_id})
    pinned = chat.get("pinned_message") or {}
    document = pinned.get("document") or {}
    print("Закреплённая копия: id=%s, файл=%s" % (pinned.get("message_id"),
                                                 document.get("file_name")))
    if not document:
        print("Нет закреплённого файла — облако ещё не сделало копию.")
        return 1

    handle, path = tempfile.mkstemp(prefix="cloud-diag-", suffix=".db.gz")
    os.close(handle)
    try:
        telegram.download_file(document["file_id"], path)
        print("Скачано: %d байт" % os.path.getsize(path))
        from snapshot import restore_bytes
        target = path + ".db"
        with open(path, "rb") as source:
            blob = source.read()
        if not restore_bytes(blob, target):
            print("Не удалось распаковать копию.")
            return 1

        connection = sqlite3.connect(target, timeout=10)
        try:
            show(connection, "Настройки пользователей",
                 "SELECT tg_id, first_name, group_title, evening_enabled, evening_time,"
                 " before_minutes, change_alerts, hw_alerts FROM users")
            show(connection, "Журнал отправок по видам",
                 "SELECT kind, COUNT(*) AS сколько, MIN(created_at) AS первое,"
                 " MAX(created_at) AS последнее FROM sent GROUP BY kind ORDER BY сколько DESC")
            show(connection, "Все отправки по времени (последние 40)",
                 "SELECT created_at, tg_id, kind, substr(key, 1, 40) AS key FROM sent"
                 " ORDER BY created_at DESC LIMIT 40", limit=40)
            show(connection, "Найденные изменения расписания",
                 "SELECT id, group_title, year, week, date, kind, substr(summary,1,45) AS summary,"
                 " created_at, announced FROM changes ORDER BY id DESC LIMIT 30", limit=30)
            show(connection, "Сводка по изменениям",
                 "SELECT group_title, COUNT(*) AS всего, MAX(created_at) AS последнее"
                 " FROM changes GROUP BY group_title")
            show(connection, "Домашка (если есть)",
                 "SELECT id, group_title, subject, due_date, status, created_at"
                 " FROM homework ORDER BY id DESC LIMIT 10", limit=10)
            show(connection, "Служебные отметки",
                 "SELECT key, substr(value,1,60) AS value FROM meta ORDER BY key")
        finally:
            connection.close()
    finally:
        for candidate in (path, path + ".db"):
            try:
                os.remove(candidate)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
