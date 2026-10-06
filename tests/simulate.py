# -*- coding: utf-8 -*-
"""Полный офлайн-прогон бота: сценарии студентов, ДЗ, напоминания, изменения.

Запуск:
    python tests/simulate.py

Ничего наружу не ходит: API подменяется локальным моком (tests/mock_api.py),
Telegram — классом FakeTelegram. Тест не зависит от текущей даты: расписание
из фикстуры сдвигается так, чтобы всегда попадать на «сегодня» и «завтра».
"""

import os
import shutil
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import texts                                            # noqa: E402
from bot import ScheduleBot, parse_due_date             # noqa: E402
from schedule import unifirst                           # noqa: E402
from store import Store                                 # noqa: E402
from mock_api import MockApi, load_default_timetables   # noqa: E402

DB_PATH = os.path.join(ROOT, "data", "simulate.db")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print("  ✅ %s" % name)
    else:
        print("  ❌ %s %s" % (name, ("— " + str(detail)) if detail else ""))
        FAILURES.append("%s %s" % (name, detail))


# ------------------------------------------------------------- фальшивый Telegram

class FakeTelegram:
    """Запоминает всё, что бот «отправил», вместо обращения к Telegram."""

    def __init__(self):
        self.messages = []
        self.edits = []
        self.answered = []
        self.actions = []
        self.commands = []

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML",
                     disable_notification=False):
        item = {"chat_id": chat_id, "text": text, "keyboard": reply_markup,
                "message_id": len(self.messages) + 1}
        self.messages.append(item)
        return {"message_id": item["message_id"]}

    def edit_message(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                           "keyboard": reply_markup})
        return {"message_id": message_id}

    def answer_callback(self, callback_id, text=None, show_alert=False):
        self.answered.append(callback_id)
        return True

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return True

    def get_me(self):
        return {"id": 1, "username": "test_bot", "first_name": "Test"}

    def set_my_commands(self, commands):
        self.commands = commands
        return True

    def delete_webhook(self, drop_pending_updates=False):
        return True

    def get_updates(self, offset=None, timeout=25, allowed_updates=None):
        return []

    # удобные выборки для проверок
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
        self.edits.clear()


# ------------------------------------------------------------------ подготовка

def shift_payload(payload, delta_days):
    """Сдвигает все даты расписания на delta_days (тест не зависит от даты)."""
    shifted = {"meta": dict(payload.get("meta") or {}), "data": []}
    for entry in payload.get("data") or []:
        new_entry = dict(entry)
        lessons = {}
        for iso_date, day in (entry.get("lessons") or {}).items():
            old_day = datetime.strptime(iso_date, "%Y-%m-%d").date()
            new_day = old_day + timedelta(days=delta_days)
            new_items = []
            for item in day.get("items") or []:
                new_item = dict(item)
                info = dict(item.get("date") or {})
                info["date"] = new_day.strftime("%d.%m.%Y")
                new_item["date"] = info
                new_items.append(new_item)
            lessons[new_day.isoformat()] = {
                "label": "%d %s %d, %s" % (new_day.day, MONTHS[new_day.month - 1],
                                           new_day.year, WEEKDAYS[new_day.weekday()]),
                "items": new_items,
            }
        new_entry["lessons"] = lessons
        shifted["data"].append(new_entry)
    if shifted["meta"]:
        base_meta = dict(shifted["meta"])
        first = min((datetime.strptime(key, "%Y-%m-%d").date()
                     for key in (shifted["data"][0]["lessons"] if shifted["data"] else {})),
                    default=date.today())
        base_meta["date_from"] = first.isoformat()
        base_meta["date_to"] = (first + timedelta(days=6)).isoformat()
        shifted["meta"] = base_meta
    return shifted


def send_text(bot, chat_id, text):
    bot.handle_update({"message": {"chat": {"id": chat_id}, "from": {
        "id": chat_id, "username": "student", "first_name": "Тест"}, "text": text}})


def press(bot, chat_id, data, message_id=1):
    bot.handle_update({"callback_query": {
        "id": "cb-%s-%d" % (data, message_id), "data": data,
        "from": {"id": chat_id, "username": "student", "first_name": "Тест"},
        "message": {"chat": {"id": chat_id}, "message_id": message_id}}})


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)

    today = date.today()
    year, week = unifirst.iso_year_week(today)
    fixtures = load_default_timetables()
    fixture_key = next(iter(fixtures))
    fixture = {"meta": {"year": fixture_key[1], "week": fixture_key[2], "limit": 10,
                        "count": len(fixtures[fixture_key])},
               "data": fixtures[fixture_key]}
    fixture_monday = min(datetime.strptime(key, "%Y-%m-%d").date()
                         for key in fixture["data"][0]["lessons"])
    delta = (today - fixture_monday).days
    payload = shift_payload(fixture, delta)
    days = sorted(payload["data"][0]["lessons"])
    today_lessons = payload["data"][0]["lessons"][days[0]]["items"]
    tomorrow_key = days[1] if len(days) > 1 else None
    tomorrow_lessons = payload["data"][0]["lessons"][tomorrow_key]["items"] if tomorrow_key else []

    print("Фикстура сдвинута на %+d дн.: сегодня %s (пар: %d), неделя %d/%d"
          % (delta, days[0], len(today_lessons), year, week))

    mock = MockApi()
    base = mock.start()
    mock.set_timetable("26281", year, week, payload)
    mock.set_timetable("26281", *unifirst.iso_year_week(today + timedelta(days=7)),
                       shift_payload(fixture, delta + 7))
    mock.set_timetable("26281", *unifirst.iso_year_week(today - timedelta(days=7)),
                       shift_payload(fixture, delta - 7))

    api = unifirst.Unifirst(base=base, timeout=5)
    tg = FakeTelegram()
    bot = ScheduleBot("test-token", db_path=DB_PATH, logger=lambda message: None,
                      api=api, telegram=tg)
    bot.me = {"id": 1}
    store = bot.store

    try:
        print("\n1. Онбординг и выбор группы")
        send_text(bot, 100, "/start")
        text = tg.last_text(100)
        check("новичку показан выбор группы", "Выбор группы" in text, text[:120])
        keyboard = (tg.messages[-1]["keyboard"] or {}).get("inline_keyboard", [])
        group_buttons = [button for row in keyboard for button in row
                         if (button.get("callback_data") or "").startswith("g:")]
        check("в списке есть кнопки групп", len(group_buttons) >= 5, len(group_buttons))
        check("есть навигация по страницам групп",
              any((button.get("callback_data") or "").startswith("gp:")
                  for row in keyboard for button in row), keyboard)

        group = store.group_by_name("26281")
        check("группа 26281 есть в кэше", bool(group))
        if group:
            send_text(bot, 100, "26281")
            check("выбор группы по номеру", store.get_user(100)["group_title"] == "26281",
                  store.get_user(100)["group_title"])
        tg.clear()

        print("\n2. Расписание: сегодня, завтра, неделя")
        send_text(bot, 100, texts.BTN_TODAY)
        text = tg.last_text(100)
        subject_today = today_lessons[0]["title"]
        check("сегодня показан предмет дня", subject_today in text, text[:200])
        check("сегодня показана аудитория", today_lessons[0]["auditories"][0]["name"] in text)
        check("сегодня показан преподаватель",
              today_lessons[0]["teachers"][0]["name"] in text)

        tg.clear()
        send_text(bot, 100, texts.BTN_TOMORROW)
        text = tg.last_text(100)
        if tomorrow_lessons:
            check("завтра показан предмет", tomorrow_lessons[0]["title"] in text, text[:200])
        else:
            check("завтра корректно сообщает об отсутствии пар", "Занятий нет" in text, text[:200])

        tg.clear()
        send_text(bot, 100, texts.BTN_WEEK)
        text = tg.last_text(100)
        check("неделя: есть заголовок недели", "Неделя с" in text, text[:200])
        check("неделя: встречается предмет", subject_today in text)

        print("\n3. Поиск группы с опечаткой (262801)")
        tg.clear()
        bot.store.ensure_user(200, "student2", "Второй")
        bot.store.update_user(200, group_title="", group_id=None, state="group_search")
        send_text(bot, 200, "262801")
        text = tg.joined(200)
        suggestion_buttons = [button.get("text") for item in tg.out(200)
                              for row in ((item.get("keyboard") or {})
                                          .get("inline_keyboard") or [])
                              for button in row]
        check("опечатка в номере группы распознана",
              any("26281" in str(label) for label in suggestion_buttons),
              "%s / кнопки: %s" % (text[:80], suggestion_buttons))

        print("\n3b. Выбор группы кнопкой (второй пользователь)")
        tg.clear()
        if group:
            press(bot, 200, "g:%d" % group["id"])
            check("кнопка группы выбирает её", store.get_user(200)["group_title"] == "26281",
                  store.get_user(200)["group_title"])
            check("после выбора показано расписание",
                  any(len(item["text"]) > 40 for item in tg.out(200)), tg.joined(200)[:200])

        print("\n4. Домашние задания")
        tg.clear()
        send_text(bot, 100, texts.BTN_HOMEWORK)
        check("экран ДЗ открылся", "ДЗ" in tg.last_text(100))
        press(bot, 100, "hw:new:choose")
        press(bot, 100, "hw:new:group")
        send_text(bot, 100, "История | §§1-2, вопросы 3-5 | завтра")
        homework = store.list_homework(100, "26281", scope="group")
        check("общее ДЗ сохранено", len(homework) == 1, len(homework))
        if homework:
            item = homework[0]
            check("предмет разобран", item["subject"] == "История", item["subject"])
            check("задание разобрано", item["task"].startswith("§§1-2"), item["task"])
            check("срок «завтра» разобран",
                  item["due_date"] == (today + timedelta(days=1)).isoformat(), item["due_date"])

        press(bot, 100, "hw:new:choose")
        press(bot, 100, "hw:new:personal")
        send_text(bot, 100, "Купить тетрадь по анатомии")
        personal = store.list_homework(100, "26281", scope="personal")
        check("личная заметка сохранена", len(personal) == 1, len(personal))
        check("личная заметка помечена как personal",
              personal and personal[0]["scope"] == "personal")

        other = bot.store.ensure_user(300, "student3", "Третий")
        store.update_user(300, group_title="26281")
        other_group_hw = store.list_homework(300, "26281", scope="group")
        other_all = store.list_homework(300, "26281", scope="all")
        check("чужие личные заметки не видны", len(other_group_hw) == 1, len(other_group_hw))
        check("в общем списке только общее ДЗ + свои заметки", len(other_all) == 1, len(other_all))

        if homework:
            hw_id = homework[0]["id"]
            tg.clear()
            press(bot, 100, "hv:%d" % hw_id)
            check("подтверждение ДЗ записано",
                  len(store.homework_votes(hw_id)) == 1, len(store.homework_votes(hw_id)))
            check("бот ответил на подтверждение", "записал" in tg.joined(100), tg.joined(100)[:120])

        tg.clear()
        send_text(bot, 100, texts.BTN_HOMEWORK)
        press(bot, 100, "hw:list:group")
        check("список ДЗ группы показывает задание", "История" in tg.joined(100))

        print("\n5. Напоминания: вечерний дайджест")
        user = store.get_user(100)
        store.update_user(100, evening_enabled=1, evening_time="20:00", before_minutes=0)
        tg.clear()
        evening_utc = datetime.combine(today, datetime.min.time()) + timedelta(hours=17, minutes=30)
        bot.engine.tick_once(now_utc=evening_utc)
        digest = tg.joined(100)
        if tomorrow_lessons:
            check("дайджест содержит пары на завтра", tomorrow_lessons[0]["title"] in digest,
                  digest[:250])
        check("дайджест напоминает про ДЗ",
              "Не забудь сдать" in digest and "История" in digest, digest[:250])
        before = len(tg.messages)
        bot.engine.tick_once(now_utc=evening_utc + timedelta(minutes=1))
        check("повторный тик не дублирует дайджест", len(tg.messages) == before,
              "%d -> %d" % (before, len(tg.messages)))

        print("\n6. Напоминание перед парой")
        store.update_user(100, evening_enabled=0, before_minutes=30)
        first_start = today_lessons[0]["date"]["time_start"]
        hours, minutes = [int(part) for part in first_start.split(":")]
        lesson_local = datetime.combine(today, datetime.min.time()).replace(
            hour=hours, minute=minutes)
        now_utc = lesson_local - timedelta(minutes=20) - timedelta(hours=3)
        tg.clear()
        bot.engine.tick_once(now_utc=now_utc)
        text = tg.joined(100)
        check("предупреждение перед парой отправлено", "Через 30 минут" in text, text[:200])
        check("в предупреждении есть предмет", today_lessons[0]["title"] in text, text[:200])

        print("\n7. Онлайн-проверка: расписание изменилось")
        store.update_user(100, evening_enabled=0, before_minutes=0, change_alerts=1)
        reduced = {"meta": payload["meta"], "data": []}
        for entry in payload["data"]:
            new_entry = dict(entry)
            lessons = {}
            for iso_date, day in entry["lessons"].items():
                items = list(day["items"])
                if iso_date == days[0] and items:
                    items = items[1:]          # убираем первую пару дня
                lessons[iso_date] = {"label": day["label"], "items": items}
            new_entry["lessons"] = lessons
            reduced["data"].append(new_entry)
        mock.set_timetable("26281", year, week, reduced)
        tg.clear()
        send_text(bot, 100, texts.BTN_REFRESH)
        text = tg.joined(100)
        check("бот сообщил об изменении расписания",
              "изменил" in text.lower() or "➖" in text, text[:250])
        check("изменение записано в базу",
              len(store.recent_changes("26281")) >= 1, store.recent_changes("26281"))

        store.update_user(200, group_title="26281", change_alerts=1)
        tg.clear()
        bot.engine.send_change_alerts()
        check("подписчики получили уведомление об изменении",
              "Расписание изменилось" in tg.joined(100) or "Расписание изменилось" in tg.joined(200),
              tg.joined(100)[:200])
        check("изменения помечены как объявленные",
              len(store.pending_changes("26281")) == 0,
              len(store.pending_changes("26281")))

        print("\n8. Fallback: сайт расписания недоступен")
        stale = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        store.execute("UPDATE timetable SET fetched_at=?", (stale,))
        mock.stop()
        tg.clear()
        send_text(bot, 100, texts.BTN_TODAY)
        text = tg.joined(100)
        check("бот показал данные из кэша", len(text) > 20, text[:120])
        check("бот честно предупредил об отсутствии связи",
              "нет связи" in text.lower(), text[:200])

        print("\n9. Настройки")
        tg.clear()
        send_text(bot, 100, texts.BTN_SETTINGS)
        check("экран настроек открылся", "Настройки" in tg.last_text(100))
        press(bot, 100, "st:evening")
        check("дайджест включён кнопкой", store.get_user(100)["evening_enabled"] == 1)
        press(bot, 100, "st:before")
        check("напоминание перед парой переключено",
              store.get_user(100)["before_minutes"] in (0, 15, 30, 60),
              store.get_user(100)["before_minutes"])
        press(bot, 100, "st:changes")
        check("подписка на изменения переключена",
              store.get_user(100)["change_alerts"] == 0)

        print("\n10. Разбор дат и ДЗ")
        check("дата 08.10 разобрана",
              parse_due_date("08.10", today) == date(today.year, 10, 8) or
              (today.month > 10 and parse_due_date("08.10", today) == date(today.year + 1, 10, 8)))
        check("дата «послезавтра» разобрана",
              parse_due_date("послезавтра", today) == today + timedelta(days=2))

        print("\n11. Навигация по неделям, помощь и админка")
        tg.clear()
        previous = unifirst.shift_week(year, week, -1)
        press(bot, 100, "wk:%d:%d" % previous)
        check("переход на прошлую неделю работает",
              "Неделя с" in tg.joined(100), tg.joined(100)[:150])
        press(bot, 100, "wk:%d:%d" % (year, week))
        check("возврат на текущую неделю работает", "Неделя с" in tg.joined(100))

        tg.clear()
        send_text(bot, 100, "/help")
        check("помощь содержит команды", "/refresh" in tg.joined(100) and "/hw" in tg.joined(100))
        send_text(bot, 100, "/whoami")
        check("whoami показывает группу", "26281" in tg.joined(100))

        tg.clear()
        send_text(bot, 100, "/admin")
        check("первый пользователь стал админом", store.get_user(100)["is_admin"] == 1)
        check("админка показывает статистику", "Пользователей" in tg.joined(100),
              tg.joined(100)[:150])
        press(bot, 100, "adm:groups")
        check("обновление списка групп из админки", "Список групп обновлён" in tg.joined(100),
              tg.joined(100)[:150])

        print("\n12. Защита от повторных напоминаний (ночной сценарий Render)")
        # На бесплатном хостинге сервис засыпает и просыпается с пустым диском:
        # база поднимается из копии, и отметка «уже отправлено» может пропасть.
        # Проверяем, что в этом случае старые напоминания не приходят заново.
        from reminders import ReminderEngine
        store.update_user(100, evening_enabled=1, evening_time="20:00", before_minutes=0)
        store.execute("DELETE FROM sent")
        bot.engine.reset_memory()
        evening_utc = datetime.combine(today, datetime.min.time()) + timedelta(hours=17, minutes=30)
        tg.clear()
        bot.engine.tick_once(now_utc=evening_utc)                  # 20:30 по Казани
        check("дайджест уходит в своё время", len(tg.out(100)) >= 1, tg.joined(100)[:120])

        tg.clear()
        bot.engine.tick_once(now_utc=evening_utc + timedelta(minutes=1))
        check("повтор в том же процессе не отправляется", len(tg.out(100)) == 0,
              tg.joined(100)[:160])

        # Перезапуск сервиса поздним вечером: журнал отправок потерян, время ушло
        store.execute("DELETE FROM sent")
        bot.engine = ReminderEngine(store, api, tg, logger=lambda message: None,
                                    refresh_minutes=20)
        late_utc = datetime.combine(today, datetime.min.time()) + timedelta(hours=19)   # 22:00
        tg.clear()
        bot.engine.tick_once(now_utc=late_utc)
        check("опоздавший дайджест не отправляется", len(tg.out(100)) == 0,
              tg.joined(100)[:200])
        sent_keys = [row["key"] for row in store.query("SELECT key FROM sent WHERE kind='evening'")]
        check("опоздавший дайджест помечен отправленным", bool(sent_keys), sent_keys)

        # Перезапуск ночью: время дайджеста ещё не наступило — тоже молчим
        store.execute("DELETE FROM sent")
        bot.engine.reset_memory()
        night_utc = datetime.combine(today, datetime.min.time())    # 03:00 по Казани
        tg.clear()
        bot.engine.tick_once(now_utc=night_utc)
        check("ночью бот молчит", len(tg.out(100)) == 0, tg.joined(100)[:200])

        print("\n13. Служебные сообщения не вызывают ответа")
        # Telegram присылает боту апдейт и о закреплении сообщения — без текста.
        # Раньше бот отвечал на него «Пока я понимаю только текст и кнопки»,
        # поэтому после каждой копии базы в чате появлялось лишнее сообщение.
        tg.clear()
        bot.handle_update({"message": {
            "message_id": 5, "chat": {"id": 100},
            "from": {"id": 100, "first_name": "Тест"},
            "pinned_message": {"message_id": 4, "date": 0,
                               "document": {"file_name": "schedule-backup.db.gz"}}}})
        check("сообщение о закреплении: бот молчит", len(tg.out(100)) == 0,
              tg.joined(100)[:160])

        tg.clear()
        bot.handle_update({"message": {
            "message_id": 6, "chat": {"id": 100},
            "from": {"id": 100, "first_name": "Тест"},
            "photo": [{"file_id": "AgAC"}]}})
        check("фото от человека: один вежливый ответ",
              "только текст" in tg.joined(100), tg.joined(100)[:160])

        print("\n14. Подгруппы: одна пара вместо «дублей»")
        # Вуз делит группу на подгруппы (язык, информатика, физкультура): сайт отдаёт
        # несколько строк с одним временем и предметом, но разными преподавателями.
        # Для студента это одна пара с выбором, а не две одинаковые.
        all_lessons = unifirst.normalize(payload)
        split_blocks = [block for block in unifirst.group_by_slot(all_lessons)
                        if len(block["lessons"]) > 1]
        check("в расписании есть пары с подгруппами", bool(split_blocks), len(split_blocks))
        if split_blocks:
            block = split_blocks[0]
            subject = block["lessons"][0]["subject"]
            rendered = texts.slot_block(block)
            check("подгруппы: предмет показан один раз", rendered.count(subject) == 1,
                  rendered[:220])
            variants = [", ".join(item.get("teachers") or []) for item in block["lessons"]]
            check("подгруппы: видны все варианты",
                  all(who and who in rendered for who in variants), variants)
            check("подгруппы: есть пояснение про выбор",
                  "подгрупп" in rendered.lower(), rendered[:160])

            day_lessons = [item for item in all_lessons if item["date"] == block["date"]]
            day_text = texts.day_schedule(date.fromisoformat(block["date"]), day_lessons,
                                          "26281")
            check("день: предмет подгрупп не задублирован", day_text.count(subject) == 1,
                  day_text[:220])
            compact = texts.day_compact(date.fromisoformat(block["date"]), day_lessons)
            check("недельный вид: подгруппы идут одной строкой", compact.count(subject) == 1,
                  compact[:220])
            digest = texts.evening_digest({}, date.fromisoformat(block["date"]), day_lessons,
                                          [], "26281")
            check("вечерний дайджест: без дублей", digest.count(subject) == 1, digest[:220])
            reminder = texts.before_lesson_reminder(30, block)
            check("перед парой: одно напоминание на подгруппы",
                  reminder.count(subject) == 1 and "подгрупп" in reminder.lower(),
                  reminder[:220])

    finally:
        try:
            mock.stop()
        except Exception:
            pass
        bot.engine.stop()

    print("\n" + "=" * 60)
    print("Проверок: %d, провалов: %d" % (len(CHECKS), len(FAILURES)))
    if FAILURES:
        for failure in FAILURES:
            print("  ❌ %s" % failure)
        print("ИТОГ: ЕСТЬ ОШИБКИ")
        return 1
    print("ИТОГ: ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
