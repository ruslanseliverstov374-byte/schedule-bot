# -*- coding: utf-8 -*-
"""Проверки админских функций: рассылка и данные по пользователям.

Запуск:
    python tests/test_admin.py

Всё офлайн: расписание берётся у поддельного источника, Telegram — поддельный,
база — временный файл SQLite. Проверяются:
  * кто становится админом и что посторонним админ-функции недоступны;
  * учёт активности (последний визит и число сообщений);
  * список пользователей и выгрузка CSV-файлом;
  * рассылка: выбор получателей → текст → предпросмотр → подтверждение → отчёт
    и запись в историю;
  * адресная рассылка (по группе и только активным).
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot import ScheduleBot      # noqa: E402

CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print("  ✅ %s" % name)
    else:
        print("  ❌ %s %s" % (name, ("— " + str(detail)) if detail else ""))
        FAILURES.append("%s %s" % (name, detail))


class FakeTelegram:
    """Запоминает отправленное вместо реальных запросов в Telegram."""

    def __init__(self):
        self.messages = []
        self.documents = []

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML",
                     disable_notification=False):
        message_id = len(self.messages) + 1
        self.messages.append({"chat_id": chat_id, "text": text,
                              "keyboard": reply_markup, "message_id": message_id})
        return {"message_id": message_id}

    def edit_message(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        self.messages.append({"chat_id": chat_id, "text": text, "keyboard": reply_markup,
                              "message_id": message_id})
        return {"message_id": message_id}

    def send_document(self, chat_id, file_path, caption=None, filename=None,
                      disable_notification=False):
        self.documents.append({"chat_id": chat_id, "path": file_path,
                               "caption": caption, "filename": filename})
        return {"message_id": len(self.messages) + 1}

    def answer_callback(self, callback_id, text=None, show_alert=False):
        return True

    def send_chat_action(self, chat_id, action="typing"):
        return True

    def get_me(self):
        return {"id": 1, "username": "test_bot", "first_name": "Test"}

    def set_my_commands(self, commands):
        return True

    def delete_webhook(self, drop_pending_updates=False):
        return True

    def get_updates(self, offset=None, timeout=25, allowed_updates=None):
        return []

    def out(self, chat_id=None):
        return [item for item in self.messages
                if chat_id is None or item["chat_id"] == chat_id]

    def last_text(self, chat_id=None):
        items = self.out(chat_id)
        return items[-1]["text"] if items else ""

    def joined(self, chat_id=None):
        return "\n".join(item["text"] for item in self.out(chat_id))

    def clear(self):
        self.messages.clear()
        self.documents.clear()


class FakeProvider:
    """Поддельный источник расписания: тесты не ходят в интернет."""

    name = "unifirst"
    title = "Тестовый вуз"
    city = "Казань"

    def groups(self):
        return [{"name": "26281", "hasSubgroups": False, "subgroups": []},
                {"name": "26282", "hasSubgroups": False, "subgroups": []}]

    def week_lessons(self, group, year, week, force=False):
        return [], {}

    def check(self):
        return {"groups": 2}

    def subgroups_of(self, group):
        return []


NAMES = {100: "Руслан", 200: "Софа", 300: "Давний"}


def send_text(bot, chat_id, text, name=None):
    """Отправляет боту текстовое сообщение от имени пользователя.

    Имя берётся из NAMES: Telegram всегда присылает first_name, и без этого
    последующие сообщения затирали бы имя на «Студент».
    """
    bot.handle_message({"message_id": 1, "date": 0, "chat": {"id": chat_id},
                        "from": {"id": chat_id, "username": "u%d" % chat_id,
                                 "first_name": name or NAMES.get(chat_id, "Студент")},
                        "text": text})


def click(bot, chat_id, data):
    bot.handle_callback({"id": "cb%d" % chat_id, "from": {"id": chat_id},
                         "message": {"message_id": 1, "chat": {"id": chat_id}},
                         "data": data})


def main():
    # База внутри проекта: во временных папках системы песочница может запретить запись.
    temp_dir = os.path.join(ROOT, "data", "test-admin")
    os.makedirs(temp_dir, exist_ok=True)
    db_path = os.path.join(temp_dir, "test.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    tg = FakeTelegram()
    bot = ScheduleBot("test-token", db_path=db_path, logger=lambda message: None,
                      telegram=tg, provider=FakeProvider())
    bot.me = {"id": 1}
    store = bot.store
    admin, student, inactive = 100, 200, 300

    try:
        print("1. Кто админ и кто нет")
        send_text(bot, admin, "/start", "Руслан")
        check("первый пользователь стал админом", bool(store.get_user(admin)["is_admin"]))
        send_text(bot, student, "/start", "Софа")
        check("второй пользователь не админ", not store.get_user(student)["is_admin"])
        tg.clear()
        send_text(bot, student, "/admin")
        check("постороннему админ-панель закрыта", "админа" in tg.last_text(student).lower(),
              tg.last_text(student)[:80])
        tg.clear()
        send_text(bot, student, "/users")
        check("постороннему список пользователей закрыт",
              "админа" in tg.last_text(student).lower(), tg.last_text(student)[:80])

        print("\n2. Админ-панель")
        tg.clear()
        send_text(bot, admin, "/admin")
        text = tg.last_text(admin)
        check("админ-панель открылась", "Админ-панель" in text, text[:60])
        buttons = [button.get("callback_data")
                   for row in (tg.messages[-1]["keyboard"] or {}).get("inline_keyboard", [])
                   for button in row]
        for expected in ("adm:users", "adm:stats", "adm:csv", "adm:history", "adm:broadcast"):
            check("кнопка %s есть" % expected, expected in buttons, buttons)

        print("\n3. Учёт активности")
        session = store.get_user(admin)
        before = session.get("messages_count") or 0
        before_seen = session.get("last_seen") or ""
        send_text(bot, admin, "/today")
        send_text(bot, admin, "/today")
        after = store.get_user(admin)
        check("счётчик сообщений растёт", (after.get("messages_count") or 0) >= before + 2,
              (before, after.get("messages_count")))
        check("время последнего визита обновилось",
              bool(after.get("last_seen")) and after["last_seen"] >= before_seen and
              after["last_seen"].startswith(datetime.now().strftime("%Y-%m-%d")),
              after.get("last_seen"))

        print("\n4. Данные: список пользователей и CSV")
        store.update_user(student, group_title="26281", subgroup="Подгруппа 1")
        send_text(bot, inactive, "/start", "Давний")
        old_stamp = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        store.execute("UPDATE users SET last_seen=? WHERE tg_id=?", (old_stamp, inactive))
        tg.clear()
        send_text(bot, admin, "/users")
        listing = tg.last_text(admin)
        check("список пользователей открылся", "Пользователи" in listing, listing[:80])
        check("в списке есть имя студента", "Софа" in listing, listing[:200])
        check("в списке видна группа", "26281" in listing, listing[:200])
        check("в списке видно число сообщений", "💬" in listing, listing[:200])
        check("показан счётчик всех пользователей", "всего 3" in listing, listing[:80])
        tg.clear()
        send_text(bot, admin, "/users csv")
        check("CSV отправлен файлом", len(tg.documents) == 1, tg.documents)
        if tg.documents:
            path = tg.documents[0]["path"]
            check("файл CSV существует", os.path.exists(path), path)
            with open(path, encoding="utf-8-sig") as handle:
                body = handle.read()
            check("CSV с заголовком", body.startswith("бот;tg_id;"), body[:60])
            check("CSV содержит студента", "Софа" in body, body[:200])
            check("CSV содержит группу", "26281" in body)
            check("строк в CSV — все пользователи", len(body.strip().splitlines()) == 4,
                  len(body.strip().splitlines()))
        tg.clear()
        send_text(bot, admin, "/stats")
        stats_text = tg.last_text(admin)
        check("сводка открылась", "Данные по боту" in stats_text, stats_text[:80])
        check("сводка показывает всего пользователей", "Всего пользователей: <b>3</b>"
              in stats_text, stats_text[:300])
        check("сводка показывает активность",
              "сегодня заходили" in stats_text and "за 7 дней" in stats_text)
        check("сводка показывает домашку", "Домашка" in stats_text)

        print("\n5. Рассылка: выбор получателей, предпросмотр, подтверждение")
        tg.clear()
        send_text(bot, admin, "/broadcast")
        targets = tg.last_text(admin)
        check("экран выбора получателей", "Рассылка от админа" in targets, targets[:80])
        buttons = [button.get("callback_data")
                   for row in (tg.messages[-1]["keyboard"] or {}).get("inline_keyboard", [])
                   for button in row]
        check("есть вариант «всем»", "bc:all" in buttons, buttons)
        check("есть вариант «активным»", "bc:active" in buttons, buttons)
        check("есть вариант по группе", any(b.startswith("bc:group:") for b in buttons),
              buttons)
        tg.clear()
        click(bot, admin, "bc:all")
        check("бот попросил текст", "Пришли текст" in tg.last_text(admin),
              tg.last_text(admin)[:80])
        tg.clear()
        send_text(bot, admin, "Завтра не будет первой пары")
        preview = tg.last_text(admin)
        check("показан предпросмотр", "Предпросмотр рассылки" in preview, preview[:80])
        check("в предпросмотре виден текст", "Завтра не будет первой пары" in preview)
        check("в предпросмотре видно число получателей", "Получателей: <b>2</b>" in preview,
              preview[:200])
        buttons = [button.get("callback_data")
                   for row in (tg.messages[-1]["keyboard"] or {}).get("inline_keyboard", [])
                   for button in row]
        check("есть кнопка подтверждения", "bc:send" in buttons, buttons)
        check("есть кнопка отмены", "bc:cancel" in buttons, buttons)
        tg.clear()
        click(bot, admin, "bc:send")
        check("отчёт о рассылке отправлен админу", "Рассылка" in tg.last_text(admin)
              and "доставлено" in tg.last_text(admin), tg.last_text(admin)[:100])
        check("сообщение дошло студенту",
              any("Завтра не будет первой пары" in item["text"] for item in tg.out(student)),
              [item["text"][:40] for item in tg.out(student)])
        check("сообщение дошло второму студенту",
              any("Завтра не будет первой пары" in item["text"] for item in tg.out(inactive)))
        check("сам админ себе не написал",
              not any("Завтра не будет первой пары" in item["text"]
                      for item in tg.out(admin)), [item["text"][:40] for item in tg.out(admin)])
        history = store.recent_broadcasts(5)
        check("рассылка записана в историю", len(history) == 1, history)
        check("в истории верный текст и результат",
              history and history[0]["text"] == "Завтра не будет первой пары"
              and history[0]["delivered"] == 2 and history[0]["failed"] == 0, history)
        tg.clear()
        send_text(bot, admin, "/broadcasts")
        check("история показывается в чате", "История рассылок" in tg.last_text(admin),
              tg.last_text(admin)[:80])

        print("\n6. Адресная рассылка и отмена")
        tg.clear()
        send_text(bot, admin, "/broadcast")
        click(bot, admin, "bc:group:26281")
        check("выбрана группа", "Пришли текст" in tg.last_text(admin), tg.last_text(admin)[:80])
        send_text(bot, admin, "Консультация в 18:00")
        click(bot, admin, "bc:send")
        check("уведомление дошло только студенту группы",
              any("Консультация в 18:00" in item["text"] for item in tg.out(student)),
              [item["text"][:40] for item in tg.out(student)])
        check("вне группы уведомление не пришло",
              not any("Консультация в 18:00" in item["text"] for item in tg.out(inactive)))
        tg.clear()
        send_text(bot, admin, "/broadcast")
        click(bot, admin, "bc:all")
        send_text(bot, admin, "Черновик, который отменим")
        click(bot, admin, "bc:cancel")
        check("отмена работает", "отменена" in tg.last_text(admin).lower(),
              tg.last_text(admin)[:80])
        check("после отмены ничего не отправлено",
              not any("Черновик" in item["text"] for item in tg.out(student)))
        check("после отмены состояние сброшено", not (store.get_user(admin) or {}).get("state"),
              store.get_user(admin).get("state"))
        check("в истории две рассылки", len(store.recent_broadcasts(5)) == 2,
              len(store.recent_broadcasts(5)))
        tg.clear()
        send_text(bot, admin, "/broadcast")
        click(bot, admin, "bc:active")
        send_text(bot, admin, "Только для активных")
        click(bot, admin, "bc:send")
        check("активным ушло", any("Только для активных" in item["text"]
                                   for item in tg.out(student)))
        check("давно не заходившему не ушло",
              not any("Только для активных" in item["text"] for item in tg.out(inactive)))

    finally:
        try:
            bot.store.close()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("Проверок: %d, провалов: %d" % (len(CHECKS), len(FAILURES)))
    if FAILURES:
        for failure in FAILURES:
            print("  ❌ %s" % failure)
        print("ИТОГ: ЕСТЬ ОШИБКИ")
        return 1
    print("OK: %d проверок" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
