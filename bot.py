# -*- coding: utf-8 -*-
"""Telegram-бот расписания Поволжского ГУФКСиТ (Казань).

Только стандартная библиотека Python: ни pip, ни виртуальных окружений.
Данные — с официального сайта расписания: https://timetable.unifirst.ru
(публичный API https://api.unifirst.ru/api/v1).

Запуск:
    python bot.py --token 8123456789:AAH...
    python bot.py                 # токен из token.txt, .env или BOT_TOKEN
    python bot.py --check         # самопроверка без Telegram
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
from datetime import date, datetime, timedelta, timezone

import texts
import tgbot
from instance_lock import InstanceLock
from reminders import ReminderEngine
from schedule import providers, unifirst
from snapshot import SnapshotManager, restore_from_chat
from store import Store
from webapp import StatusApp

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data", "bot.db")
LOCK_FILE = os.path.join(ROOT, "data", "bot.lock")
TOKEN_FILE = os.path.join(ROOT, "token.txt")
ENV_FILE = os.path.join(ROOT, ".env")
LOG_FILE = os.path.join(ROOT, "logs", "bot.log")

EVENING_TIMES = ["18:00", "19:00", "20:00", "21:00", "22:00"]
BEFORE_OPTIONS = [0, 15, 30, 60]
GROUPS_PER_PAGE = 8

MENU_LABELS = set()
for _row in texts.MAIN_BUTTONS:
    for _label in _row:
        MENU_LABELS.add(_label)
MENU_LABELS.update({texts.BTN_MENU, texts.BTN_CANCEL, "🏠 Главное меню"})


# ------------------------------------------------------------------ служебное

def env_value(key, default=""):
    """Значение из переменной окружения или из файла .env."""
    value = os.environ.get(key)
    if value:
        return value.strip()
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("#"):
                    continue
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def snapshot_interval_minutes():
    """Интервал копий базы: SNAPSHOT_INTERVAL в секундах (или в минутах, если мало)."""
    raw = env_value("SNAPSHOT_INTERVAL", "1800") or "1800"
    try:
        value = int(raw)
    except ValueError:
        value = 1800
    if value <= 0:
        value = 1800
    return max(1, value // 60) if value > 120 else value


def read_token(cli_token=None, token_file=None):
    if cli_token:
        return cli_token.strip()
    token = env_value("BOT_TOKEN")
    if token:
        return token
    for path in ([token_file] if token_file else []) + [TOKEN_FILE]:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
                if value:
                    return value
    return ""


def make_logger(log_path=LOG_FILE):
    directory = os.path.dirname(log_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    def log(message):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (stamp, message)
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    return log


def http_error_code(error):
    return getattr(error, "code", None) or getattr(error, "error_code", None)


# ------------------------------------------------------------------------ бот

class ScheduleBot:
    def __init__(self, token, db_path=DEFAULT_DB, refresh_minutes=20, logger=print,
                 api=None, telegram=None, owner_id=None, webhook_base="", port=None,
                 provider=None, university=""):
        self.log = logger
        self.token = token
        self.store = Store(db_path)
        self.api = api or unifirst.Unifirst()
        self.tg = telegram or tgbot.Telegram(token)
        # Источник расписания выбирается переменной UNIVERSITY (unifirst или kgasu):
        # одна кодовая база обслуживает оба вуза. Если провайдер передан снаружи
        # (например в тестах с офлайн-моком), используем его как есть.
        if provider is not None:
            self.provider = provider
        elif (university or env_value("UNIVERSITY", "unifirst")).strip().lower() in (
                "kgasu", "кгасу"):
            self.provider = providers.KgasuProvider(store=self.store)
        else:
            self.provider = providers.UnifirstProvider(self.api)
        self.engine = ReminderEngine(self.store, self.api, self.tg, logger=logger,
                                     refresh_minutes=refresh_minutes,
                                     provider=self.provider)
        self.lock = InstanceLock(os.path.join(ROOT, "data",
                                              "bot-%s.lock" % self.provider.name))
        self.owner_id = int(owner_id) if str(owner_id or "").isdigit() else None
        self.webhook_base = (webhook_base or "").rstrip("/")
        self.port = port
        self.status_app = None
        self.snapshot = None
        self.webhook_mode = False
        self.me = None

    # ------------------------------------------------------------ запуск

    def setup(self):
        self.me = self.tg.get_me()
        self.log("Бот @%s запущен (id %s)" % (self.me.get("username"), self.me.get("id")))
        self.tg.set_my_commands([
            {"command": "start", "description": "Выбрать группу и начать"},
            {"command": "today", "description": "Расписание на сегодня"},
            {"command": "tomorrow", "description": "Расписание на завтра"},
            {"command": "week", "description": "Расписание на неделю"},
            {"command": "hw", "description": "Домашние задания"},
            {"command": "refresh", "description": "Проверить актуальность расписания"},
            {"command": "settings", "description": "Настройки и напоминания"},
            {"command": "help", "description": "Помощь"},
        ])
        if self.webhook_base:
            self.start_webhook_mode()
        else:
            try:
                self.tg.delete_webhook(drop_pending_updates=False)
            except Exception as error:
                self.log("deleteWebhook: %s" % error)
            if self.port:
                self.start_status_page()
        self.refresh_groups_cache(force=self.store.groups_count() == 0)
        # Если вуз у бота сменился (например база приехала от другого источника),
        # список групп нужно загрузить заново — иначе студент не найдёт свою группу.
        if self.store.get_meta("provider") != self.provider.name:
            self.log("Источник расписания: %s — обновляю список групп"
                     % self.provider.name)
            self.refresh_groups_cache(force=True)
            self.store.set_meta("provider", self.provider.name)
        self.start_snapshot()

    # ------------------------------------------------- облако: вебхук и копии

    def start_status_page(self):
        """Health-страница без вебхука: удобно для проверок и мониторинга."""
        self.status_app = StatusApp(
            self.store, on_update=None, webhook_secret="",
            bot_username=(self.me or {}).get("username", ""),
            engine=self.engine, logger=self.log)
        actual_port = self.status_app.start(self.port)
        self.log("Страница состояния: http://127.0.0.1:%d/ (health: /health)" % actual_port)
        return self.status_app

    def start_webhook_mode(self):
        """Render (и любой хостинг с HTTPS): принимаем обновления вебхуком."""
        secret = env_value("WEBHOOK_SECRET") or hashlib.sha256(
            ("schedule-bot:" + self.token).encode("utf-8")).hexdigest()[:40]
        url = self.webhook_base + "/telegram"
        self.tg.set_webhook(url, secret_token=secret)
        self.webhook_mode = True
        self.status_app = StatusApp(
            self.store, on_update=self.handle_update, webhook_secret=secret,
            bot_username=(self.me or {}).get("username", ""),
            engine=self.engine, logger=self.log)
        actual_port = self.status_app.start(self.port)
        self.log("Режим вебхука: %s (страница состояния на порту %d)" % (url, actual_port))
        return self.status_app

    def start_snapshot(self):
        """Копии базы в чат владельца — на бесплатном хостинге диск стирается."""
        owner_id = self.owner_id
        if not owner_id:
            stored = self.store.get_meta("owner_id")
            owner_id = int(stored) if str(stored or "").isdigit() else None
        if not owner_id:
            self.log("Копии базы выключены: не задан OWNER_ID "
                     "(включить: /admin → «Включить копии»)")
            return None
        interval = snapshot_interval_minutes()
        self.snapshot = SnapshotManager(self.tg, self.store, owner_id,
                                        interval_minutes=interval, logger=self.log)
        self.snapshot.start()
        self.log("Копии базы: раз в %d мин в чат %s" % (interval, owner_id))
        return self.snapshot

    def enable_snapshots(self, chat_id):
        self.store.set_meta("owner_id", chat_id)
        if not self.snapshot:
            self.start_snapshot()
        return bool(self.snapshot)

    def refresh_groups_cache(self, force=False):
        if not force and not self.store.groups_stale(hours=24):
            return self.store.groups_count()
        try:
            groups = self.provider.groups()
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            self.log("Не удалось обновить список групп: %s" % error)
            return self.store.groups_count()
        if groups:
            self.store.save_groups(groups)
            self.log("Список групп обновлён: %d" % len(groups))
        return self.store.groups_count()

    def run(self):
        if not self.lock.acquire():
            pid = InstanceLock.running_pid(self.lock.pid_path)
            self.log("Бот уже запущен (pid %s) — второй экземпляр не нужен. "
                     "Остановите старый процесс или запустите remove_autostart.bat."
                     % (pid or "?"))
            return 1
        try:
            self.setup()
            self.engine.start()
            if self.webhook_mode:
                self.log("Вебхук активен: опрос Telegram не нужен, жду сообщения")
                while True:
                    time.sleep(30)
            offset = self.store.get_meta("update_offset")
            offset = int(offset) if offset else None
            self.log("Опрос Telegram запущен")
            while True:
                try:
                    updates = self.tg.get_updates(offset=offset, timeout=25,
                                                  allowed_updates=["message", "callback_query"])
                except urllib.error.URLError as error:
                    self.log("Нет связи с Telegram: %s" % error)
                    time.sleep(5)
                    continue
                except tgbot.TgError as error:
                    self.log("Ошибка Telegram: %s" % error)
                    if http_error_code(error) == 401:
                        self.log("Токен неверный — остановка.")
                        return 1
                    time.sleep(3)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    self.store.set_meta("update_offset", offset)
                    try:
                        self.handle_update(update)
                    except Exception as error:
                        self.log("Ошибка обработки апдейта: %s\n%s"
                                 % (error, traceback.format_exc(limit=3)))
            return 0
        finally:
            self.engine.stop()
            if self.snapshot:
                self.snapshot.stop()
            if self.status_app:
                self.status_app.stop()
            self.lock.release()

    # --------------------------------------------------------- маршрутизация

    def handle_update(self, update):
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message):
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        sender = message.get("from") or {}
        user = self.store.ensure_user(chat_id, sender.get("username", ""),
                                      sender.get("first_name", ""))
        if not text:
            # Служебные сообщения (закрепление копии базы, смена названия чата и т.п.)
            # приходят без текста — отвечать на них нельзя, иначе бот «разговаривает»
            # сам с собой: например, после каждой резервной копии писал
            # «Пока я понимаю только текст и кнопки».
            if is_service_message(message):
                self.log("Служебное сообщение (%s) — не отвечаю" % service_kind(message))
                return
            self.tg.send_message(chat_id, "Пока я понимаю только текст и кнопки 🙂")
            return
        self.log("→ %s (%s): %s" % (chat_id, user.get("group_title") or "без группы",
                                    text[:80]))

        command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        if command:
            self.handle_command(chat_id, user, command, text)
            return

        # Кнопки меню работают из любого состояния: иначе, например, в режиме
        # ввода ДЗ кнопка «Сегодня» воспринималась бы как текст задания.
        if text in MENU_LABELS:
            if user.get("state"):
                self.store.set_state(user["tg_id"], "")
            self.handle_button(chat_id, user, text)
            return

        state = user.get("state") or ""
        if state == "hw_input":
            self.finish_homework_input(chat_id, user, text)
            return
        if state == "hw_due":
            self.finish_homework_due(chat_id, user, text)
            return
        if state == "broadcast":
            self.finish_broadcast(chat_id, user, text)
            return
        if state == "evening_time":
            self.finish_evening_time(chat_id, user, text)
            return

        if state == "group_search" or not user.get("group_title"):
            self.try_select_group_by_text(chat_id, user, text)
            return

        self.handle_button(chat_id, user, text)

    def handle_command(self, chat_id, user, command, raw):
        mapping = {
            "/start": self.cmd_start,
            "/today": lambda c, u, a: self.send_day(c, u, date.today()),
            "/tomorrow": lambda c, u, a: self.send_day(
                c, u, self.local_today(u) + timedelta(days=1)),
            "/week": self.cmd_week,
            "/hw": self.cmd_homework,
            "/refresh": self.cmd_refresh,
            "/settings": self.cmd_settings,
            "/reminders": self.cmd_reminders,
            "/help": self.cmd_help,
            "/whoami": self.cmd_whoami,
            "/admin": self.cmd_admin,
        }
        handler = mapping.get(command)
        if not handler:
            self.tg.send_message(chat_id, "Неизвестная команда. Жми /help 🙂",
                                 reply_markup=self.main_keyboard())
            return
        handler(chat_id, user, raw)

    def handle_button(self, chat_id, user, label):
        if label == texts.BTN_TODAY:
            self.send_day(chat_id, user, self.local_today(user))
        elif label == texts.BTN_TOMORROW:
            self.send_day(chat_id, user, self.local_today(user) + timedelta(days=1))
        elif label == texts.BTN_WEEK:
            self.cmd_week(chat_id, user, "")
        elif label == texts.BTN_HOMEWORK:
            self.cmd_homework(chat_id, user, "")
        elif label == texts.BTN_REMINDERS:
            self.cmd_reminders(chat_id, user, "")
        elif label == texts.BTN_REFRESH:
            self.cmd_refresh(chat_id, user, "")
        elif label == texts.BTN_SETTINGS:
            self.cmd_settings(chat_id, user, "")
        elif label == texts.BTN_HELP:
            self.cmd_help(chat_id, user, "")
        elif label == texts.BTN_CANCEL:
            self.tg.send_message(chat_id, "Отменил.", reply_markup=self.main_keyboard())
        elif label == texts.BTN_MENU or label == "🏠 Главное меню":
            self.tg.send_message(chat_id, "🏠 Главное меню", reply_markup=self.main_keyboard())
        else:
            self.tg.send_message(
                chat_id,
                "Не понял сообщение. Выбери пункт меню или отправь /help.\n"
                "Если хочешь сменить группу — напиши её номер, например <code>26281</code>.",
                reply_markup=self.main_keyboard())

    # ------------------------------------------------------------- клавиатуры

    def main_keyboard(self):
        return tgbot.reply_keyboard([[{"text": label} for label in row]
                                     for row in texts.MAIN_BUTTONS])

    def local_today(self, user):
        offset = int(user.get("tz_offset") or 3)
        return (datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=offset)).date()

    def week_keyboard(self, year, week):
        previous = unifirst.shift_week(year, week, -1)
        following = unifirst.shift_week(year, week, 1)
        today_year, today_week = unifirst.iso_year_week(date.today())
        middle = "🏠 Текущая" if (year, week) != (today_year, today_week) else "🔄 Проверить"
        return tgbot.inline([[
            tgbot.btn("⬅️", "wk:%d:%d" % previous),
            tgbot.btn(middle, "wk:%d:%d" % (today_year, today_week)
                      if middle.startswith("🏠") else "r:%d:%d" % (year, week)),
            tgbot.btn("➡️", "wk:%d:%d" % following),
        ], [
            tgbot.btn("📝 ДЗ группы", "hw:list:group"),
            tgbot.btn("🏠 Меню", "menu"),
        ]])

    def group_keyboard(self, page, pages, groups, prefix="gp"):
        rows = []
        for group in groups:
            title = str(group["name"])
            mark = " ✅" if group.get("has_subgroups") else ""
            rows.append([tgbot.btn("%s%s" % (title, mark), "g:%s" % group["id"])])
        navigation = []
        if page > 0:
            navigation.append(tgbot.btn("⬅️", "%s:%d" % (prefix, page - 1)))
        navigation.append(tgbot.btn("%d/%d" % (page + 1, max(pages, 1)), "noop"))
        if page + 1 < pages:
            navigation.append(tgbot.btn("➡️", "%s:%d" % (prefix, page + 1)))
        if navigation:
            rows.append(navigation)
        rows.append([tgbot.btn("🏠 Меню", "menu")])
        return tgbot.inline(rows)

    def homework_keyboard(self):
        return tgbot.inline([
            [tgbot.btn("➕ Добавить ДЗ", "hw:new:choose")],
            [tgbot.btn("📋 Задания группы", "hw:list:group"),
             tgbot.btn("🗒 Личные заметки", "hw:list:personal")],
            [tgbot.btn("📅 На неделю", "hw:list:week"),
             tgbot.btn("🔥 Срочные", "hw:list:soon")],
            [tgbot.btn("🏠 Меню", "menu")],
        ])

    def homework_item_keyboard(self, item, user):
        rows = [[tgbot.btn("👍 Подтвердить", "hv:%d" % item["id"]),
                 tgbot.btn("✅ Выполнено" if item.get("scope") == "personal"
                           else "✔️ Выполнено", "hc:%d" % item["id"])]]
        if item.get("created_by") == user["tg_id"] or user.get("is_admin"):
            rows.append([tgbot.btn("🗑 Удалить", "hd:%d" % item["id"])])
        return tgbot.inline(rows)

    def settings_keyboard(self, user):
        evening = "🔕 Выключить" if user.get("evening_enabled") else "🌙 Включить"
        before = user.get("before_minutes") or 0
        return tgbot.inline([
            [tgbot.btn("🎓 Сменить группу", "st:group")],
            [tgbot.btn(evening, "st:evening"),
             tgbot.btn("🕒 Время: %s" % user.get("evening_time"), "st:time")],
            [tgbot.btn("🔔 Перед парой: %s" % ("выкл" if not before else "%d мин" % before),
                       "st:before")],
            [tgbot.btn("📣 Изменения: %s" % ("вкл" if user.get("change_alerts") else "выкл"),
                       "st:changes"),
             tgbot.btn("📚 Дедлайны: %s" % ("вкл" if user.get("hw_alerts") else "выкл"),
                       "st:hw")],
            [tgbot.btn("🔕 Тихий режим", "st:quiet"),
             tgbot.btn("🔔 Все напоминания", "st:loud")],
            [tgbot.btn("ℹ️ Помощь", "help"), tgbot.btn("🏠 Меню", "menu")],
        ])

    # ------------------------------------------------------------ команды

    def cmd_start(self, chat_id, user, raw):
        if not user.get("group_title"):
            self.send_group_page(chat_id, user, 0)
            return
        self.tg.send_message(chat_id, texts.hello(user.get("first_name"),
                                                  self.provider.title,
                                                  getattr(self.provider, "city", "Казань")),
                             reply_markup=self.main_keyboard())
        self.send_day(chat_id, user, self.local_today(user))

    def cmd_week(self, chat_id, user, raw):
        if not self.require_group(chat_id, user):
            return
        today_year, today_week = unifirst.iso_year_week(self.local_today(user))
        self.send_week(chat_id, user, today_year, today_week)

    def cmd_homework(self, chat_id, user, raw):
        if not self.require_group(chat_id, user):
            return
        group_title = user["group_title"]
        open_items = self.store.list_homework(user["tg_id"], group_title, scope="group")
        personal = self.store.list_homework(user["tg_id"], group_title, scope="personal")
        self.tg.send_message(
            chat_id,
            texts.homework_menu(open_count=len(open_items), personal_count=len(personal)),
            reply_markup=self.homework_keyboard())

    def cmd_refresh(self, chat_id, user, raw):
        if not self.require_group(chat_id, user):
            return
        self.tg.send_chat_action(chat_id)
        group_title = user["group_title"]
        today = self.local_today(user)
        year, week = unifirst.iso_year_week(today)
        messages = []
        total_changes = 0
        for delta in (0, 1):
            target_year, target_week = unifirst.iso_year_week(today + timedelta(weeks=delta))
            try:
                lessons, changed, old_lessons = self.engine.ensure_week(
                    group_title, target_year, target_week, force=True)
            except (unifirst.UnifirstError, providers.ProviderError) as error:
                self.tg.send_message(chat_id, "😔 Сайт расписания не отвечает: %s" % texts.esc(error),
                                     reply_markup=self.main_keyboard())
                return
            if changed:
                total_changes += 1
                changes = unifirst.diff_lessons(old_lessons, lessons)
                lines = texts.changes_lines(changes)
                messages.append("⚠️ <b>%s</b>\n%s" % (
                    texts.esc(unifirst.week_label(target_year, target_week)),
                    "\n".join(lines) if lines else "детали не распознаны"))
            else:
                messages.append("✅ %s — без изменений" % texts.esc(
                    unifirst.week_label(target_year, target_week)))
        meta = self.store.get_timetable(group_title, year, week)
        stamp = (meta or {}).get("fetched_at", "")
        header = texts.refresh_result(group_title, total_changes > 0, messages)
        self.tg.send_message(chat_id, header, reply_markup=self.main_keyboard())

    def cmd_settings(self, chat_id, user, raw):
        self.tg.send_message(chat_id, texts.settings_screen(user),
                             reply_markup=self.settings_keyboard(user))

    def cmd_reminders(self, chat_id, user, raw):
        self.tg.send_message(chat_id, texts.reminders_screen(user),
                             reply_markup=self.reminders_keyboard(user))

    def reminders_keyboard(self, user):
        return tgbot.inline([
            [tgbot.btn("🌙 Дайджест: %s" % ("вкл" if user.get("evening_enabled") else "выкл"),
                       "st:evening"),
             tgbot.btn("🕒 %s" % user.get("evening_time"), "st:time")],
            [tgbot.btn("🔔 Перед парой: %s" % (
                "выкл" if not user.get("before_minutes") else "%d мин" % user["before_minutes"]),
                "st:before")],
            [tgbot.btn("🏠 Меню", "menu")],
        ])

    def cmd_help(self, chat_id, user, raw):
        self.tg.send_message(chat_id, texts.help_text(user, bool(user.get("is_admin"))),
                             reply_markup=self.main_keyboard())

    def cmd_whoami(self, chat_id, user, raw):
        self.tg.send_message(chat_id, texts.whoami(user), reply_markup=self.main_keyboard())

    def cmd_admin(self, chat_id, user, raw):
        if not user.get("is_admin"):
            self.tg.send_message(chat_id, "Команда только для админа бота.")
            return
        stats = self.store.stats()
        online = True
        try:
            self.provider.check()
        except Exception:
            online = False
        buttons = [
            [tgbot.btn("🔄 Обновить группы", "adm:groups"),
             tgbot.btn("♻️ Обновить расписание", "adm:cache")],
            [tgbot.btn("💾 Сохранить копию базы", "adm:backup"),
             tgbot.btn("🗄 Включить копии" if not self.snapshot else "🗄 Копии включены",
                       "adm:enable_backup" if not self.snapshot else "adm:stats")],
            [tgbot.btn("📊 Статистика", "adm:stats"),
             tgbot.btn("📣 Рассылка", "adm:broadcast")],
            [tgbot.btn("🏠 Меню", "menu")],
        ]
        self.tg.send_message(
            chat_id,
            texts.admin_screen(stats, self.store.groups_updated_at(), online,
                               self.backup_status()),
            reply_markup=tgbot.inline(buttons))

    def backup_status(self):
        """Строка состояния резервных копий для админ-экрана."""
        if not self.snapshot:
            return "выключены (нет OWNER_ID)"
        saved = getattr(self.snapshot, "last_saved_text", "") or "ещё не делалась"
        return "включены, последняя: %s" % saved

    def touched_data(self):
        """Важные изменения (ДЗ, смена группы): просим обновить копию базы.

        На бесплатном хостинге сервис засыпает каждые ~15 минут, а при пробуждении
        диск пустой — без такой просьбы свежие правки могли пропасть.
        """
        if self.snapshot:
            try:
                self.snapshot.request_save()
            except Exception as error:
                self.log("Не удалось запросить копию базы: %s" % error)

    # ------------------------------------------------------------- расписание

    def example_group(self):
        """Пример названия группы для подсказки в списке (у вузов разные форматы)."""
        if getattr(self.provider, "name", "") == "kgasu":
            return "26ЗК01з"
        return "26281"

    def require_group(self, chat_id, user):
        if user.get("group_title"):
            return True
        self.tg.send_message(chat_id, texts.no_group_hint())
        self.send_group_page(chat_id, user, 0)
        return False

    def lessons_of(self, chat_id, user, day):
        """Пары на дату; при недоступности API — из кэша."""
        group_title = user["group_title"]
        year, week = unifirst.iso_year_week(day)
        try:
            lessons, _, _ = self.engine.ensure_week(group_title, year, week)
            return lessons, True
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            self.log("API недоступен для %s: %s" % (group_title, error))
            row = self.store.get_timetable(group_title, year, week)
            return (row["lessons"] if row else []), False

    def send_day(self, chat_id, user, day):
        if not self.require_group(chat_id, user):
            return
        self.tg.send_chat_action(chat_id)
        lessons, online = self.lessons_of(chat_id, user, day)
        day_lessons = [item for item in lessons if item.get("date") == day.isoformat()]
        text = texts.day_schedule(day, day_lessons, user.get("group_title"))
        note = texts.cache_note(online=online)
        if note:
            text += "\n\n" + note
        rows = []
        if day_lessons:
            rows.append([tgbot.btn("📝 ДЗ по предмету", "hwp:%s" % day.isoformat())])
        rows.append([tgbot.btn("🗓 Неделя", "wk:%d:%d" % unifirst.iso_year_week(day)),
                     tgbot.btn("🔄 Обновить", "r:%d:%d" % unifirst.iso_year_week(day))])
        rows.append([tgbot.btn("🏠 Меню", "menu")])
        self.tg.send_message(chat_id, text, reply_markup=tgbot.inline(rows))

    def send_week(self, chat_id, user, year, week):
        if not self.require_group(chat_id, user):
            return
        self.tg.send_chat_action(chat_id)
        group_title = user["group_title"]
        try:
            lessons, _, _ = self.engine.ensure_week(group_title, year, week)
            online = True
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            self.log("API недоступен: %s" % error)
            row = self.store.get_timetable(group_title, year, week)
            lessons = row["lessons"] if row else []
            online = False
        text = texts.week_schedule(lessons, group_title, year, week)
        note = texts.cache_note(online=online)
        if note:
            text += "\n\n" + note
        self.tg.send_message(chat_id, text, reply_markup=self.week_keyboard(year, week))

    # --------------------------------------------------------- выбор группы

    def send_group_page(self, chat_id, user, page, query="", edit_message_id=None):
        total = self.store.groups_count()
        if total == 0:
            self.refresh_groups_cache(force=True)
            total = self.store.groups_count()
        if total == 0:
            self.tg.send_message(chat_id, "😔 Не удалось получить список групп с сайта. "
                                          "Попробуй ещё раз через минуту — напиши /start.")
            return
        pages = (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE
        page = max(0, min(page, pages - 1))
        if query:
            groups = self.store.search_groups(query, limit=GROUPS_PER_PAGE)
            pages = 1
        else:
            groups = self.store.list_groups(page * GROUPS_PER_PAGE, GROUPS_PER_PAGE)
        text, _ = texts.group_list_page(groups, page, pages, query,
                                        user.get("group_title") if user else "",
                                        self.example_group())
        keyboard = self.group_keyboard(page, pages, groups)
        self.store.set_state((user or {}).get("tg_id"), "group_search")
        if edit_message_id:
            try:
                self.tg.edit_message(chat_id, edit_message_id, text, keyboard)
                return
            except tgbot.TgError:
                pass
        self.tg.send_message(chat_id, text, reply_markup=keyboard)

    def try_select_group_by_text(self, chat_id, user, text):
        """Пользователь написал номер группы (или ошибся в нём)."""
        query = text.strip()
        exact = self.store.group_by_name(query)
        if exact:
            self.apply_group(chat_id, user, exact)
            return
        groups = []
        for candidate in group_query_candidates(query):
            groups = self.store.search_groups(candidate, limit=GROUPS_PER_PAGE)
            if groups:
                if candidate != query:
                    self.tg.send_message(
                        chat_id,
                        "🤔 Группы <code>%s</code> нет. Похоже, ты имел в виду:"
                        % texts.esc(query),
                        reply_markup=self.group_keyboard(0, 1, groups))
                    return
                break
        if not groups:
            self.tg.send_message(
                chat_id,
                "😕 Группу <b>%s</b> не нашёл.\n"
                "Напиши только цифры, например <code>26281</code>, или посмотри "
                "список: /start" % texts.esc(query))
            return
        if len(groups) == 1:
            self.apply_group(chat_id, user, groups[0])
            return
        self.store.set_state(user["tg_id"], "group_search")
        self.tg.send_message(
            chat_id,
            "🎓 Нашёл несколько групп — выбери свою:",
            reply_markup=self.group_keyboard(0, 1, groups))

    def apply_group(self, chat_id, user, group):
        group_id = group["id"]
        name = str(group["name"])
        subgroups = group.get("subgroups") or []
        if group.get("has_subgroups") and subgroups:
            rows = [[tgbot.btn(str(item.get("name")), "sg:%d:%d" % (group_id, index))]
                    for index, item in enumerate(subgroups)]
            rows.append([tgbot.btn("Без подгруппы", "sg:%d:-1" % group_id)])
            self.store.set_state(user["tg_id"], "subgroup", {"group_id": group_id, "name": name})
            self.tg.send_message(chat_id, texts.group_confirmed(name, subgroups),
                                 reply_markup=tgbot.inline(rows))
            return
        self.store.update_user(user["tg_id"], group_id=group_id, group_title=name, subgroup="")
        self.store.set_state(user["tg_id"], "")
        fresh = self.store.get_user(user["tg_id"])
        self.touched_data()
        self.tg.send_message(chat_id, "✅ Группа <b>%s</b> выбрана!" % texts.esc(name),
                             reply_markup=self.main_keyboard())
        self.send_day(chat_id, fresh, self.local_today(fresh))

    def finish_evening_time(self, chat_id, user, text):
        match = re.search(r"(\d{1,2})[:.\s]?(\d{2})?", text)
        if not match:
            self.tg.send_message(chat_id, "Не понял время. Напиши, например, <code>20:30</code>.")
            return
        hours = int(match.group(1))
        minutes = int(match.group(2) or 0)
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            self.tg.send_message(chat_id, "Такого времени не бывает 🙂 Напиши, например, 20:30.")
            return
        value = "%02d:%02d" % (hours, minutes)
        self.store.update_user(user["tg_id"], evening_time=value, state="")
        self.tg.send_message(chat_id, "🕒 Вечерний дайджест будет приходить в <b>%s</b> "
                                      "(время Казани)." % value,
                             reply_markup=self.main_keyboard())

    # -------------------------------------------------------------------- ДЗ

    def finish_homework_input(self, chat_id, user, text):
        if text.strip().lower() in ("отмена", "/cancel", texts.BTN_CANCEL.lower()):
            self.store.set_state(user["tg_id"], "")
            self.tg.send_message(chat_id, "Отменил.", reply_markup=self.main_keyboard())
            return
        state_data = self.store.state_of(user)
        scope = state_data.get("scope", "group")
        subject_hint = state_data.get("subject", "")
        parsed = parse_homework_text(text, fallback_subject=subject_hint)
        if parsed.get("single") and not parsed.get("task"):
            # Одна строка без разделителей — считаем её текстом задания.
            parsed["task"] = parsed.get("subject", "")
            parsed["subject"] = subject_hint or ""
        if not parsed.get("task"):
            self.tg.send_message(
                chat_id,
                "Не разобрал. Напиши в формате:\n"
                "<code>предмет | задание | срок</code>\n"
                "Например: <code>История | §§1-2 | 08.10</code>")
            return
        due = parse_due_date(parsed.get("due", ""), self.local_today(user))
        teacher = state_data.get("teacher", "")
        room = state_data.get("room", "")
        if parsed.get("teacher"):
            teacher = parsed["teacher"]
        hw_id = self.store.add_homework(
            user["group_title"], parsed["subject"], parsed["task"],
            due_date=due.isoformat() if due else "", teacher=teacher, room=room,
            scope=scope, owner_id=user["tg_id"] if scope == "personal" else None,
            created_by=user["tg_id"])
        self.store.set_state(user["tg_id"], "")
        item = self.store.get_homework(hw_id)
        self.touched_data()
        self.tg.send_message(chat_id, texts.homework_created(item, scope),
                             reply_markup=self.homework_item_keyboard(item, user))

    def finish_homework_due(self, chat_id, user, text):
        due = parse_due_date(text, self.local_today(user))
        hw_id = self.store.state_of(user).get("hw_id")
        self.store.update_user(user["tg_id"], state="")
        if not hw_id or not due:
            self.tg.send_message(chat_id, "Не понял дату. Напиши, например, <code>08.10</code>.",
                                 reply_markup=self.main_keyboard())
            return
        self.store.update_homework(hw_id, due_date=due.isoformat())
        item = self.store.get_homework(hw_id)
        self.tg.send_message(chat_id, "✅ Срок обновлён:\n\n" + texts.homework_item(item),
                             reply_markup=self.homework_item_keyboard(item, user))

    def send_homework_list(self, chat_id, user, mode):
        group_title = user["group_title"]
        today = self.local_today(user)
        due_from = due_to = ""
        scope = "all"
        title = "📝 <b>Домашние задания</b>"
        if mode == "group":
            scope = "group"
            title = "📘 <b>Задания группы %s</b>" % texts.esc(group_title)
        elif mode == "personal":
            scope = "personal"
            title = "🗒 <b>Личные заметки</b>"
        elif mode == "week":
            due_from = today.isoformat()
            due_to = (today + timedelta(days=7)).isoformat()
            title = "📅 <b>ДЗ на неделю</b> (с %s по %s)" % (
                today.strftime("%d.%m"), (today + timedelta(days=7)).strftime("%d.%m"))
        elif mode == "soon":
            due_from = today.isoformat()
            due_to = (today + timedelta(days=2)).isoformat()
            title = "🔥 <b>Срочное</b> (сегодня и завтра)"
        items = self.store.list_homework(user["tg_id"], group_title, scope=scope,
                                         due_from=due_from, due_to=due_to)
        if not items:
            text = title + "\n\n" + texts.homework_empty(scope if scope != "all" else "group")
            self.tg.send_message(chat_id, text, reply_markup=self.homework_keyboard())
            return
        self.tg.send_message(chat_id, title)
        for item in items[:15]:
            votes = len(self.store.homework_votes(item["id"]))
            text = texts.homework_item(item, votes=votes, show_scope=(scope == "all"))
            self.tg.send_message(chat_id, text,
                                 reply_markup=self.homework_item_keyboard(item, user))
        if len(items) > 15:
            self.tg.send_message(chat_id, "…и ещё %d. Уточни список: «Личные» или «На неделю»."
                                 % (len(items) - 15))
        self.tg.send_message(chat_id, texts.homework_intro(),
                             reply_markup=self.homework_keyboard())

    # ------------------------------------------------------------- напоминания

    def finish_broadcast(self, chat_id, user, text):
        self.store.set_state(user["tg_id"], "")
        if not user.get("is_admin"):
            return
        delivered = failed = 0
        for target in self.store.all_users():
            if target["tg_id"] == chat_id:
                continue
            try:
                self.tg.send_message(target["tg_id"], "📣 <b>Сообщение от админа</b>\n\n" + text)
                delivered += 1
            except Exception:
                failed += 1
        self.tg.send_message(chat_id, texts.admin_broadcast_done(delivered, failed),
                             reply_markup=self.main_keyboard())

    # -------------------------------------------------------------- колбэки

    def handle_callback(self, callback):
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        sender = callback.get("from") or {}
        user = self.store.ensure_user(chat_id, sender.get("username", ""),
                                      sender.get("first_name", ""))
        self.tg.answer_callback(callback["id"])
        if data == "noop":
            return
        try:
            self.route_callback(chat_id, user, message_id, data)
        except tgbot.TgError as error:
            if "message is not modified" not in str(error):
                self.log("Ошибка колбэка %s: %s" % (data, error))
                self.tg.send_message(chat_id, texts.error_generic(str(error)))
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            self.tg.send_message(chat_id, texts.error_generic(str(error)))
        except Exception as error:
            self.log("Сбой колбэка %s: %s\n%s" % (data, error, traceback.format_exc(limit=3)))
            self.tg.send_message(chat_id, texts.error_generic(str(error)))

    def route_callback(self, chat_id, user, message_id, data):
        parts = data.split(":")
        head = parts[0]

        if head == "menu":
            self.tg.send_message(chat_id, "🏠 Главное меню", reply_markup=self.main_keyboard())
        elif head == "help":
            self.cmd_help(chat_id, user, "")
        elif head == "gp":
            self.send_group_page(chat_id, user, int(parts[1]), edit_message_id=message_id)
        elif head == "g":
            group = self.store.group_by_id(int(parts[1]))
            if group:
                self.apply_group(chat_id, user, group)
        elif head == "sg":
            self.apply_subgroup(chat_id, user, int(parts[1]), int(parts[2]), message_id)
        elif head == "wk":
            self.send_week(chat_id, user, int(parts[1]), int(parts[2]))
        elif head == "r":
            self.cmd_refresh(chat_id, user, "")
        elif head == "hw":
            self.route_homework_callback(chat_id, user, message_id, parts)
        elif head == "hv":
            self.vote_homework(chat_id, user, int(parts[1]), message_id)
        elif head == "hc":
            self.close_homework(chat_id, user, int(parts[1]), message_id)
        elif head == "hd":
            self.delete_homework(chat_id, user, int(parts[1]), message_id)
        elif head == "st":
            self.route_settings_callback(chat_id, user, parts[1], message_id)
        elif head == "hwp":
            self.route_homework_from_day(chat_id, user, parts)
        elif head == "adm":
            self.route_admin_callback(chat_id, user, parts[1] if len(parts) > 1 else "")
        else:
            self.log("Неизвестный колбэк: %s" % data)

    def route_homework_callback(self, chat_id, user, message_id, parts):
        action = parts[1] if len(parts) > 1 else ""
        if action == "list":
            self.send_homework_list(chat_id, user, parts[2] if len(parts) > 2 else "all")
        elif action == "new":
            step = parts[2] if len(parts) > 2 else "choose"
            if step == "choose":
                self.tg.send_message(
                    chat_id,
                    "Куда добавить ДЗ?",
                    reply_markup=tgbot.inline([
                        [tgbot.btn("📘 В общую базу группы", "hw:new:group")],
                        [tgbot.btn("🗒 Личная заметка", "hw:new:personal")],
                        [tgbot.btn("⬅️ Назад", "hw:list:group")],
                    ]))
            else:
                self.start_homework_input(chat_id, user, step)
        elif action == "due":
            hw_id = int(parts[2])
            self.store.set_state(user["tg_id"], "hw_due", {"hw_id": hw_id})
            self.tg.send_message(chat_id, "Напиши новый срок, например <code>12.10</code> "
                                          "или <code>завтра</code>.")

    def start_homework_input(self, chat_id, user, scope, subject="", teacher="", room=""):
        self.store.set_state(user["tg_id"], "hw_input",
                             {"scope": scope, "subject": subject,
                              "teacher": teacher, "room": room})
        hint = texts.explain_homework_how_to_add()
        if subject:
            hint = ("✍️ Пишу ДЗ по предмету <b>%s</b>.\n\n"
                    "Отправь текст задания" % texts.esc(subject))
            if teacher or room:
                hint += " (сохраню %s%s)" % (
                    ("преподавателя " + texts.esc(teacher)) if teacher else "",
                    (", аудиторию " + texts.esc(room)) if room else "")
        self.tg.send_message(chat_id, hint)

    def route_homework_from_day(self, chat_id, user, parts):
        """hwp:<дата> — выбрать предмет; hwp:<дата>:<пара> — сразу писать задание."""
        day_iso = parts[1] if len(parts) > 1 else ""
        if len(parts) > 2 and parts[2]:
            para = parts[2]
            lessons, _ = self.lessons_of(chat_id, user,
                                         _parse_iso(day_iso) or self.local_today(user))
            blocks = [block for block in unifirst.group_by_slot(
                [item for item in lessons if item.get("date") == day_iso])
                if str(block.get("para")) == para]
            if blocks:
                items = blocks[0]["lessons"]
                # Если пара делится на подгруппы, преподавателя не подставляем:
                # он у каждой подгруппы свой.
                teacher = ", ".join(items[0].get("teachers") or []) if len(items) == 1 else ""
                room = ", ".join(items[0].get("rooms") or []) if len(items) == 1 else ""
                self.start_homework_input(
                    chat_id, user, "group", subject=items[0].get("subject", ""),
                    teacher=teacher, room=room)
                return
        self.prompt_homework_from_day(chat_id, user, day_iso)

    def prompt_homework_from_day(self, chat_id, user, day_iso):
        lessons, _ = self.lessons_of(chat_id, user, _parse_iso(day_iso) or date.today())
        day_lessons = [item for item in lessons if item.get("date") == day_iso]
        if not day_lessons:
            self.tg.send_message(chat_id, "На этот день пар нет — добавь ДЗ вручную.",
                                 reply_markup=self.homework_keyboard())
            return
        # Одна кнопка на пару: подгруппы — это варианты одной пары, дублировать не нужно.
        rows = []
        for block in unifirst.group_by_slot(day_lessons):
            items = block["lessons"]
            label = "%s. %s" % (block.get("para"), items[0].get("subject", "пара"))
            if len(items) > 1:
                label += " (подгруппы)"
            rows.append([tgbot.btn(label, "hwp:%s:%s" % (day_iso, block.get("para")))])
        rows.append([tgbot.btn("⬅️ Назад", "hw:list:group")])
        self.tg.send_message(chat_id, "По какому предмету добавить ДЗ?",
                             reply_markup=tgbot.inline(rows))

    def vote_homework(self, chat_id, user, hw_id, message_id):
        item = self.store.get_homework(hw_id)
        if not item:
            return
        self.store.vote_homework(hw_id, user["tg_id"])
        votes = len(self.store.homework_votes(hw_id))
        try:
            self.tg.edit_message(chat_id, message_id,
                                 texts.homework_item(item, votes=votes),
                                 self.homework_item_keyboard(item, user))
        except tgbot.TgError:
            pass
        self.tg.send_message(chat_id, "👍 Отметил: ты тоже записал это задание.")

    def close_homework(self, chat_id, user, hw_id, message_id):
        item = self.store.get_homework(hw_id)
        if not item:
            return
        if item.get("scope") == "group" and item.get("created_by") != user["tg_id"] \
                and not user.get("is_admin"):
            self.tg.send_message(chat_id, "Отметить выполненным общее задание может автор "
                                          "или админ. Можно просто подтвердить его 👍")
            return
        self.store.update_homework(hw_id, status="done")
        self.touched_data()
        try:
            self.tg.edit_message(chat_id, message_id, "✅ Выполнено: " + texts.homework_item(item))
        except tgbot.TgError:
            self.tg.send_message(chat_id, "✅ Отметил как выполненное: " + texts.homework_item(item))

    def delete_homework(self, chat_id, user, hw_id, message_id):
        item = self.store.get_homework(hw_id)
        if not item:
            return
        if item.get("created_by") != user["tg_id"] and not user.get("is_admin"):
            self.tg.send_message(chat_id, "Удалять может только автор задания или админ.")
            return
        self.store.delete_homework(hw_id)
        self.touched_data()
        try:
            self.tg.edit_message(chat_id, message_id, "🗑 Задание удалено.")
        except tgbot.TgError:
            self.tg.send_message(chat_id, "🗑 Задание удалено.")

    def route_settings_callback(self, chat_id, user, action, message_id):
        if action == "group":
            self.send_group_page(chat_id, user, 0)
            return
        if action == "evening":
            self.store.update_user(user["tg_id"],
                                   evening_enabled=0 if user.get("evening_enabled") else 1)
        elif action == "time":
            current = user.get("evening_time") or "20:00"
            index = EVENING_TIMES.index(current) if current in EVENING_TIMES else 2
            nxt = EVENING_TIMES[(index + 1) % len(EVENING_TIMES)]
            self.store.update_user(user["tg_id"], evening_time=nxt)
            self.tg.send_message(chat_id, "🕒 Время дайджеста: <b>%s</b>\n"
                                          "Нужно другое? Напиши, например <code>20:30</code>."
                                 % nxt)
            self.store.set_state(user["tg_id"], "evening_time")
        elif action == "before":
            current = int(user.get("before_minutes") or 0)
            index = BEFORE_OPTIONS.index(current) if current in BEFORE_OPTIONS else 0
            self.store.update_user(user["tg_id"],
                                   before_minutes=BEFORE_OPTIONS[(index + 1) % len(BEFORE_OPTIONS)])
        elif action == "changes":
            self.store.update_user(user["tg_id"],
                                   change_alerts=0 if user.get("change_alerts") else 1)
        elif action == "hw":
            self.store.update_user(user["tg_id"],
                                   hw_alerts=0 if user.get("hw_alerts") else 1)
        elif action == "quiet":
            # Всё выключаем: бот пишет только в ответ на вопросы.
            self.store.update_user(user["tg_id"], evening_enabled=0, before_minutes=0,
                                   change_alerts=0, hw_alerts=0)
            self.touched_data()
            self.tg.send_message(
                chat_id,
                "🔕 <b>Тихий режим включён.</b>\n\n"
                "Бот больше не пишет сам: ни вечернего дайджеста, ни напоминаний "
                "перед парой, ни сообщений об изменениях расписания. Он отвечает "
                "только тогда, когда ты сам нажмёшь кнопку или напишешь.\n\n"
                "Вернуть напоминания: ⚙️ Настройки → «🔔 Все напоминания».",
                reply_markup=self.main_keyboard())
        elif action == "loud":
            self.store.update_user(user["tg_id"], evening_enabled=1, before_minutes=30,
                                   change_alerts=1, hw_alerts=1)
            self.touched_data()
            self.tg.send_message(
                chat_id,
                "🔔 <b>Напоминания включены.</b>\n\n"
                "🌙 Вечером — расписание на завтра и что сдать\n"
                "🔔 За 30 минут до пары\n"
                "📣 Если расписание изменят\n"
                "📚 Утром в день сдачи\n\n"
                "Каждое напоминание приходит один раз. Настроить по отдельности — "
                "в ⚙️ Настройках.",
                reply_markup=self.main_keyboard())
        fresh = self.store.get_user(user["tg_id"])
        try:
            self.tg.edit_message(chat_id, message_id, texts.settings_screen(fresh),
                                 self.settings_keyboard(fresh))
        except tgbot.TgError:
            self.tg.send_message(chat_id, texts.settings_screen(fresh),
                                 reply_markup=self.settings_keyboard(fresh))

    def apply_subgroup(self, chat_id, user, group_id, index, message_id):
        group = self.store.group_by_id(group_id)
        if not group:
            return
        subgroups = group.get("subgroups") or []
        subgroup = ""
        if 0 <= index < len(subgroups):
            subgroup = str(subgroups[index].get("name") or "")
        title = unifirst.group_title({"name": group["name"]}, subgroup)
        self.store.update_user(user["tg_id"], group_id=group_id, group_title=title,
                               subgroup=subgroup)
        self.store.set_state(user["tg_id"], "")
        fresh = self.store.get_user(user["tg_id"])
        self.touched_data()
        if message_id:
            try:
                self.tg.edit_message(chat_id, message_id,
                                     "✅ Группа <b>%s</b> выбрана." % texts.esc(title))
            except tgbot.TgError:
                pass
        self.tg.send_message(chat_id, "Готово! Показываю расписание.",
                             reply_markup=self.main_keyboard())
        self.send_day(chat_id, fresh, self.local_today(fresh))

    def route_admin_callback(self, chat_id, user, action):
        if not user.get("is_admin"):
            self.tg.send_message(chat_id, "Только для админа.")
            return
        if action == "groups":
            count = self.refresh_groups_cache(force=True)
            self.tg.send_message(chat_id, "🔄 Список групп обновлён: <b>%d</b>" % count,
                                 reply_markup=self.main_keyboard())
        elif action == "cache":
            groups = self.store.groups_in_use()
            self.tg.send_chat_action(chat_id)
            updated = errors = 0
            for group_title in groups:
                today = date.today()
                for delta in (0, 1):
                    year, week = unifirst.iso_year_week(today + timedelta(weeks=delta))
                    try:
                        self.engine.ensure_week(group_title, year, week, force=True)
                        updated += 1
                    except (unifirst.UnifirstError, providers.ProviderError):
                        errors += 1
            self.engine.send_change_alerts()
            self.tg.send_message(chat_id, "♻️ Обновлено недель: <b>%d</b>, ошибок: %d"
                                 % (updated, errors), reply_markup=self.main_keyboard())
        elif action == "stats":
            stats = self.store.stats()
            self.tg.send_message(chat_id, texts.admin_screen(stats,
                                                             self.store.groups_updated_at()),
                                 reply_markup=self.main_keyboard())
        elif action == "broadcast":
            self.store.set_state(user["tg_id"], "broadcast")
            self.tg.send_message(chat_id, "📣 Напиши текст рассылки всем пользователям "
                                          "(или «отмена»).")
        elif action == "backup":
            if not self.snapshot and not self.enable_snapshots(chat_id):
                self.tg.send_message(chat_id, "😔 Не удалось включить копии — смотрите лог.",
                                     reply_markup=self.main_keyboard())
                return
            ok = self.snapshot.save(force=True)
            self.tg.send_message(
                chat_id,
                "💾 Копия базы сохранена и закреплена в этом чате."
                if ok else "😔 Скопировать базу не удалось — подробности в logs/bot.log",
                reply_markup=self.main_keyboard())
        elif action == "enable_backup":
            if self.enable_snapshots(chat_id):
                self.snapshot.save(force=True)
                self.tg.send_message(
                    chat_id,
                    "🗄 Копии базы включены: раз в %d мин буду присылать сжатую копию "
                    "и закреплять её здесь. Это спасёт группы, ДЗ и настройки после "
                    "перезапуска бесплатного сервиса." % snapshot_interval_minutes(),
                    reply_markup=self.main_keyboard())
            else:
                self.tg.send_message(chat_id, "😔 Не удалось включить копии — смотрите лог.",
                                     reply_markup=self.main_keyboard())


# ------------------------------------------------------------ парсинг текста

#: Поля служебных сообщений Telegram: закрепление, смена названия/аватара чата и т.п.
#: Такие апдейты приходят без текста, и отвечать на них нельзя.
SERVICE_MESSAGE_KEYS = (
    "pinned_message", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "new_chat_members", "left_chat_member", "group_chat_created",
    "supergroup_chat_created", "channel_chat_created", "migrate_to_chat_id",
    "migrate_from_chat_id", "message_auto_delete_timer_changed", "video_chat_started",
    "video_chat_ended", "video_chat_scheduled", "video_chat_participants_invited",
    "forum_topic_created", "forum_topic_closed", "forum_topic_reopened",
    "proximity_alert_triggered", "write_access_allowed", "successful_payment",
    "passport_data", "web_app_data", "users_shared", "chat_shared", "boost_added",
)


def service_kind(message):
    """Название служебного поля сообщения (для лога)."""
    for key in SERVICE_MESSAGE_KEYS:
        if key in (message or {}):
            return key
    return "нет отправителя"


def is_service_message(message):
    """Служебное ли сообщение: без отправителя или с системным полем."""
    if not (message or {}).get("from"):
        return True
    return any(key in message for key in SERVICE_MESSAGE_KEYS)


def group_query_candidates(query):
    """Варианты запроса группы: как есть, без одного символа, без последнего.

    Нужно, чтобы «262801» (лишняя цифра) находило реальную группу «26281».
    """
    query = (query or "").strip()
    candidates = []

    def push(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    push(query)
    if query.isdigit() and len(query) >= 5:
        for index in range(len(query)):
            push(query[:index] + query[index + 1:])
        push(query[:-1])
        push(query[:5])
    return candidates


def parse_homework_text(text, fallback_subject=""):
    """«предмет | задание | срок [| преподаватель]» -> словарь.

    Ключ ``single`` означает, что разделителей не было и вся строка — одно поле.
    """
    cleaned = (text or "").strip()
    parts = [part.strip() for part in re.split(r"\s*\|\s*|\s+—\s+|\s+--\s+", cleaned)
             if part.strip()]
    result = {"subject": "", "task": "", "due": "", "teacher": "",
              "single": len(parts) <= 1}
    if fallback_subject:
        result["subject"] = fallback_subject
        result["single"] = False
    if not parts:
        return result
    if result["subject"]:
        result["task"] = parts[0]
        if len(parts) > 1:
            result["due"] = parts[1]
        if len(parts) > 2:
            result["teacher"] = parts[2]
        return result
    result["subject"] = parts[0]
    if len(parts) > 1:
        result["task"] = parts[1]
    if len(parts) > 2:
        result["due"] = parts[2]
    if len(parts) > 3:
        result["teacher"] = parts[3]
    return result


WEEKDAY_NAMES = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4,
    "сб": 5, "суббота": 5,
    "вс": 6, "воскресенье": 6,
}


def parse_due_date(value, today=None):
    """Дата сдачи: 08.10, 8.10.2026, 2026-10-08, завтра, пн, через 3 дня."""
    today = today or date.today()
    raw = (value or "").strip().lower()
    if not raw:
        return None
    raw = raw.replace("к ", "", 1) if raw.startswith("к ") else raw
    if raw in ("сегодня", "today"):
        return today
    if raw in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    if raw in ("послезавтра",):
        return today + timedelta(days=2)
    match = re.search(r"через\s+(\d+)\s*(день|дня|дней|недел\w*)", raw)
    if match:
        amount = int(match.group(1))
        if match.group(2).startswith("недел"):
            return today + timedelta(weeks=amount)
        return today + timedelta(days=amount)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", raw)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = match.group(3)
        if year:
            year = int(year)
            if year < 100:
                year += 2000
        else:
            candidate = date(today.year, month, min(day, 28)) if month <= 12 else None
            year = today.year
            if candidate and candidate < today - timedelta(days=180):
                year += 1
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if raw in WEEKDAY_NAMES:
        target = WEEKDAY_NAMES[raw]
        delta = (target - today.weekday()) % 7
        return today + timedelta(days=delta if delta else 7)
    return None


def _parse_iso(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ проверка

def run_checks(db_path=DEFAULT_DB, logger=print):
    """Самопроверка без Telegram: API, база, парсинг дат."""
    problems = []
    logger("1. Проверяю API расписания...")
    api = unifirst.Unifirst()
    try:
        groups = api.groups(limit=500)
    except (unifirst.UnifirstError, providers.ProviderError) as error:
        groups = []
        problems.append("API недоступен: %s" % error)
    logger("   групп получено: %d" % len(groups))
    if groups and len(groups) < 50:
        problems.append("подозрительно мало групп: %d" % len(groups))

    lessons = []
    if groups:
        year, week = unifirst.iso_year_week(date.today())
        title = unifirst.find_group(groups, "26281") or groups[0]
        title = unifirst.group_title(title)
        logger("2. Беру расписание группы %s на неделю %d/%d..." % (title, year, week))
        try:
            lessons = unifirst.normalize(api.timetable(title, year, week))
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            problems.append("расписание не получено: %s" % error)
        logger("   пар на неделе: %d" % len(lessons))
        for lesson in lessons[:3]:
            logger("   %s | %s пара %s-%s | %s | %s | %s" % (
                lesson["date"], lesson["para"], lesson["start"], lesson["end"],
                lesson["subject"], ", ".join(lesson["rooms"]) or "—",
                ", ".join(lesson["teachers"]) or "—"))

    logger("3. Проверяю базу данных...")
    store = Store(db_path)
    store.save_groups(groups)
    logger("   групп в базе: %d" % store.groups_count())
    if lessons:
        store.save_timetable(unifirst.group_title(unifirst.find_group(groups, "26281") or groups[0]),
                             *unifirst.iso_year_week(date.today()),
                             payload={}, lessons=lessons, digest_value=unifirst.digest(lessons))

    logger("4. Проверяю разбор дат...")
    today = date(2026, 10, 5)
    cases = {
        "08.10": date(2026, 10, 8),
        "8.10.2026": date(2026, 10, 8),
        "2026-10-08": date(2026, 10, 8),
        "завтра": date(2026, 10, 6),
        "сегодня": date(2026, 10, 5),
        "через 3 дня": date(2026, 10, 8),
        "пн": date(2026, 10, 12),
    }
    for raw, expected in cases.items():
        got = parse_due_date(raw, today)
        if got != expected:
            problems.append("дата %r разобрана как %s, ожидалось %s" % (raw, got, expected))
    logger("   проверено форматов: %d" % len(cases))

    logger("5. Проверяю разбор ДЗ...")
    parsed = parse_homework_text("История | §§1-2, вопросы 3-5 | 08.10")
    expected = {"subject": "История", "task": "§§1-2, вопросы 3-5", "due": "08.10"}
    for key, value in expected.items():
        if parsed.get(key) != value:
            problems.append("разбор ДЗ: %s = %r, ожидалось %r" % (key, parsed.get(key), value))
    logger("   «%s» -> %s" % (parsed["subject"], parsed["task"]))

    if problems:
        logger("\n❌ Найдены проблемы:")
        for problem in problems:
            logger("   - %s" % problem)
        return 1
    logger("\n✅ Все проверки пройдены.")
    return 0


# ---------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Telegram-бот расписания Поволжского ГУФКСиТ")
    parser.add_argument("--token", help="токен бота от @BotFather")
    parser.add_argument("--token-file", help="файл с токеном (по умолчанию token.txt)")
    parser.add_argument("--university", help="источник расписания: unifirst или kgasu "
                                             "(иначе переменная UNIVERSITY)")
    parser.add_argument("--db", default=DEFAULT_DB, help="путь к базе SQLite")
    parser.add_argument("--refresh-minutes", type=int, default=None,
                        help="как часто проверять расписание на сайте (по умолчанию 20)")
    parser.add_argument("--check", action="store_true", help="самопроверка без Telegram")
    parser.add_argument("--owner-id", help="Telegram id владельца: чат для копий базы")
    parser.add_argument("--webhook-url", help="адрес сервиса для вебхука "
                                              "(иначе берётся WEBHOOK_URL / RENDER_EXTERNAL_URL)")
    parser.add_argument("--port", type=int, help="порт health-страницы (иначе PORT или 8080)")
    arguments = parser.parse_args(argv)

    logger = make_logger()
    if arguments.check:
        return run_checks(arguments.db, logger)

    refresh_minutes = arguments.refresh_minutes
    if not refresh_minutes:
        try:
            refresh_minutes = int(env_value("REFRESH_MINUTES", "20") or 20)
        except ValueError:
            refresh_minutes = 20

    token = read_token(arguments.token, arguments.token_file)
    if not token:
        logger("Не найден токен бота.\n"
               "Получи его у @BotFather и положи в token.txt, "
               "или запусти: python bot.py --token 8123456789:AAH...")
        return 2

    # Облако: адрес для вебхука, порт health-страницы и владелец копий базы
    owner_id = (arguments.owner_id or env_value("OWNER_ID", "")).strip()
    webhook_base = (arguments.webhook_url or env_value("WEBHOOK_URL")
                    or env_value("RENDER_EXTERNAL_URL") or "").strip()
    port = arguments.port
    if not port:
        raw_port = env_value("PORT", "").strip()
        port = int(raw_port) if raw_port.isdigit() else None

    # На бесплатном хостинге диск пустой: поднимаем базу из закреплённой копии
    if owner_id.isdigit() and not os.path.exists(arguments.db):
        logger("Базы нет — пробую восстановить из чата владельца %s..." % owner_id)
        try:
            if restore_from_chat(tgbot.Telegram(token), int(owner_id),
                                 arguments.db, logger):
                logger("База восстановлена, данные на месте")
            else:
                logger("Копии в чате нет — начинаю с чистой базы")
        except Exception as error:
            logger("Восстановление базы не удалось: %s" % error)

    bot = ScheduleBot(token, db_path=arguments.db,
                      refresh_minutes=refresh_minutes, logger=logger,
                      owner_id=owner_id, webhook_base=webhook_base, port=port,
                      university=arguments.university or "")
    try:
        return bot.run()
    except KeyboardInterrupt:
        logger("Остановлено пользователем.")
        return 0
    except tgbot.TgError as error:
        if http_error_code(error) == 401:
            logger("Telegram отклонил токен (401 Unauthorized). "
                   "Проверь token.txt — возможно, токен отозван.")
            return 2
        logger("Ошибка Telegram: %s" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
