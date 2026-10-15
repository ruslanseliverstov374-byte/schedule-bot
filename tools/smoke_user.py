# -*- coding: utf-8 -*-
"""Прогон бота по всем экранам как обычный студент — на живых данных.

Печатает то, что студент реально увидит, и подмечает странности: пустые ответы,
ошибки, дубли пар, «делится на подгруппы» без вариантов и т.п.

Запуск:
    python tools/smoke_user.py 26282
    python tools/smoke_user.py 26ЗК01 --university kgasu
"""

import argparse
import os
import re
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
import texts                                    # noqa: E402
from bot import ScheduleBot                     # noqa: E402
from schedule import providers                  # noqa: E402
from test_admin import FakeTelegram             # noqa: E402

WARNINGS = []


def plain(markup):
    text = re.sub(r"<[^>]+>", "", markup or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def warn(message):
    WARNINGS.append(message)
    print("   ⚠️ %s" % message)


def show(title, reply):
    print("\n" + "-" * 66)
    print("→ %s" % title)
    print("-" * 66)
    if not reply:
        warn("бот ничего не ответил на «%s»" % title)
        return
    text = plain(reply)
    print(text)
    if "Traceback" in text or "внутренняя ошибка" in text.lower():
        warn("в ответе на «%s» ошибка" % title)
    if "не удалось" in text.lower() or "не нашёл" in text.lower():
        warn("в ответе на «%s» есть сообщение о неудаче" % title)


def main():
    parser = argparse.ArgumentParser(description="Прогон бота по экранам студента")
    parser.add_argument("group", nargs="?", default="26282")
    parser.add_argument("--university", default="unifirst", choices=("unifirst", "kgasu"))
    parser.add_argument("--chat-id", type=int, default=777001)
    arguments = parser.parse_args()

    db_path = os.path.join("data", "smoke-%s.db" % arguments.university)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)

    tg = FakeTelegram()
    bot = ScheduleBot("test-token", db_path=db_path, logger=lambda message: None,
                      telegram=tg, university=arguments.university)
    bot.me = {"id": 1, "username": "smoke_bot"}
    chat = arguments.chat_id

    def send(text):
        tg.messages.clear()
        bot.handle_message({"message_id": 1, "date": 0, "chat": {"id": chat},
                           "from": {"id": chat, "username": "student",
                                    "first_name": "Студент"},
                           "text": text})
        replies = [item["text"] for item in tg.messages if item["chat_id"] == chat]
        return "\n\n".join(replies)

    print("=" * 66)
    print("  ПРОГОН БОТА: %s, группа %s" % (arguments.university, arguments.group))
    print("=" * 66)

    show("/start", send("/start"))
    show("выбор группы %s" % arguments.group, send(arguments.group))
    user = bot.store.get_user(chat)
    print("\n(в базе: группа %s, подгруппа %s)"
          % (user.get("group_title") or "—", user.get("subgroup") or "—"))

    for title, command in (("📅 Сегодня", texts.BTN_TODAY),
                           ("📅 Завтра", texts.BTN_TOMORROW),
                           ("🗓 Неделя", texts.BTN_WEEK)):
        reply = send(command)
        show(title, reply)
        day_text = plain(reply)
        slots = re.findall(r"(\d)\s*пара\s*·\s*([\d:–-]+)", day_text)
        if slots and len(slots) != len(set(slots)):
            warn("в «%s» повторяются пары" % title)
        duplicates = re.findall(r"(?:✏️|📖|🔬|📘)\s*([^\n]+)", day_text)
        repeated = [item for item in set(duplicates) if duplicates.count(item) > 1]
        if repeated:
            warn("в «%s» дублируются предметы: %s" % (title, repeated[:3]))

    for title, command in (("📝 ДЗ", texts.BTN_HOMEWORK),
                           ("⏰ Напоминания", texts.BTN_REMINDERS),
                           ("⚙️ Настройки", texts.BTN_SETTINGS),
                           ("🔄 Обновить", texts.BTN_REFRESH),
                           ("ℹ️ Помощь", texts.BTN_HELP),
                           ("/whoami", "/whoami")):
        show(title, send(command))

    # Домашка: добавление, список, срок
    show("ДЗ: добавить", send("📝 ДЗ"))
    show("ДЗ: ввод задания", send("Анатомия | выучить череп | завтра"))
    show("ДЗ: список", send(texts.BTN_HOMEWORK))
    homework = bot.store.query("SELECT * FROM homework")
    print("\n(в базе ДЗ: %d записей)" % len(homework))
    for row in homework:
        print("   %s | %s | срок %s | статус %s"
              % (row["subject"], row["task"], row["due_date"], row["status"]))

    print("\n" + "=" * 66)
    if WARNINGS:
        print("  ЗАМЕЧАНИЙ: %d" % len(WARNINGS))
        for item in WARNINGS:
            print("   ⚠️ %s" % item)
    else:
        print("  ЗАМЕЧАНИЙ НЕТ: все экраны отвечают нормально")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
