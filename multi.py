# -*- coding: utf-8 -*-
"""Два бота расписания в одном процессе: ПГУФКСиТ и КГАСУ.

Зачем так: бесплатный Render даёт 750 часов в месяц на аккаунт, а два отдельных
сервиса круглосуточно в этот лимит не укладываются. Здесь оба бота живут в одном
контейнере:

  * ПГУФКСиТ — принимает сообщения вебхуком и он же отдаёт /health и страницу
    состояния (Render требует открытый порт);
  * КГАСУ — работает обычным опросом Telegram, ему порт не нужен.

Базы у ботов раздельные (`data/bot.db` и `data/kgasu.db`), резервные копии —
в своих чатах с каждым ботом, поэтому друг другу они не мешают.

Запуск:
    python multi.py
    python multi.py --port 8099 --owner-id 1368878379
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime

from bot import (DEFAULT_DB, ROOT, ScheduleBot, clean_token, env_value, make_logger,
                 read_token)
from snapshot import restore_from_chat
import tgbot

KGASU_DB = os.path.join(ROOT, "data", "kgasu.db")
KGASU_TOKEN_FILE = os.path.join(ROOT, "token-kgasu.txt")
STATUS_FILE = os.path.join(ROOT, "data", "service-status.json")


def write_status(entries):
    """Состояние ботов сервиса — его читает страница /service.json.

    Так видно снаружи, поднялся ли второй бот, даже без доступа к панели Render.
    Токены в файл не попадают: только id бота и понятная причина ошибки.
    """
    payload = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "bots": entries}
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return entries


def token_info(token):
    """Проверяет токен: (id бота, имя, текст ошибки).

    Ошибка «Unauthorized» означает, что в переменной лежит старый или неверный
    токен — именно такая опечатка чаще всего мешает второму боту запуститься.
    """
    try:
        me = tgbot.Telegram(token, timeout=30).get_me()
        return me.get("id"), me.get("username"), ""
    except Exception as error:
        return None, None, str(error)[:160]


def restore_if_needed(token, db_path, owner_id, logger):
    """Поднимает базу из закреплённой копии, если файла базы нет (облако)."""
    if not owner_id or os.path.exists(db_path):
        return False
    try:
        restored = restore_from_chat(tgbot.Telegram(token), int(owner_id), db_path, logger)
        if restored:
            logger("База восстановлена из закреплённой копии в чате")
        return restored
    except Exception as error:
        logger("Восстановление базы не удалось: %s" % error)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Два бота расписания в одном процессе")
    parser.add_argument("--port", type=int, help="порт health-страницы (иначе PORT)")
    parser.add_argument("--owner-id", help="Telegram id владельца для копий баз")
    parser.add_argument("--refresh-minutes", type=int, help="частота проверки расписания")
    parser.add_argument("--bots", default="unifirst,kgasu",
                        help="каких ботов запускать: unifirst, kgasu или оба через запятую")
    arguments = parser.parse_args(argv)
    wanted = {name.strip().lower() for name in (arguments.bots or "").split(",") if name.strip()}

    owner_id = (arguments.owner_id or env_value("OWNER_ID", "")).strip()
    port = arguments.port
    if not port:
        raw_port = env_value("PORT", "").strip()
        port = int(raw_port) if raw_port.isdigit() else None
    webhook_base = (env_value("WEBHOOK_URL") or env_value("RENDER_EXTERNAL_URL") or "").strip()
    refresh = arguments.refresh_minutes
    if not refresh:
        try:
            refresh = int(env_value("REFRESH_MINUTES", "20") or 20)
        except ValueError:
            refresh = 20

    token_unifirst = clean_token(env_value("BOT_TOKEN", "")) or read_token()
    token_kgasu = (clean_token(env_value("BOT_TOKEN_KGASU", ""))
                   or read_token(token_file=KGASU_TOKEN_FILE))

    if not token_unifirst and not token_kgasu:
        print("Не найдены токены ботов (BOT_TOKEN и BOT_TOKEN_KGASU).")
        return 2

    entries = []      # состояние ботов для страницы /service.json
    bots = []

    def prepare(name, title, token, db_path, university, prefix,
                webhook_base_value="", port_value=None, refresh_value=None):
        """Проверяет токен, восстанавливает базу и создаёт бота."""
        bot_id, username, error = token_info(token)
        entry = {"name": name, "title": title, "bot_id": bot_id,
                 "username": username, "state": "запускается", "error": error}
        if error:
            auth_problem = any(word in error.lower()
                               for word in ("unauthorized", "not found", "401", "404"))
            entry["state"] = ("не запущен: неверный токен" if auth_problem
                              else "токен не проверен (сеть), пробую запустить")
            entries.append(entry)
            write_status(entries)
            print("[%s] %s: %s" % (name, entry["state"], error))
            if auth_problem:
                return None
        logger = make_logger(prefix=prefix)
        restore_if_needed(token, db_path, owner_id, logger)
        bot = ScheduleBot(token, db_path=db_path,
                          refresh_minutes=refresh_value or refresh, logger=logger,
                          owner_id=owner_id, webhook_base=webhook_base_value,
                          port=port_value, university=university)
        if entry not in entries:
            entries.append(entry)
        write_status(entries)
        return bot, entry

    if token_unifirst and "unifirst" in wanted:
        prepared = prepare("unifirst", "Поволжский ГУФКСиТ", token_unifirst, DEFAULT_DB,
                           "unifirst", "[ПГУФКСиТ]", webhook_base_value=webhook_base,
                           port_value=port)
        if prepared:
            bots.append(prepared)
    if token_kgasu and "kgasu" in wanted:
        # Второй бот получает вебхук, если первый его не занял: тогда Telegram
        # будит сервис при любом сообщении. Иначе он работал бы опросом.
        prepared = prepare("kgasu", "КГАСУ", token_kgasu, KGASU_DB, "kgasu", "[КГАСУ]",
                           webhook_base_value=webhook_base if not bots else "",
                           refresh_value=max(refresh, 30))
        if prepared:
            bots.append(prepared)

    if not bots:
        print("Ни один бот не запущен — проверьте токены.")
        return 2

    # В облаке оба бота принимают сообщения через один HTTP-сервер: у каждого
    # свой путь и секрет, а порт остаётся один. Так бесплатный сервис, который
    # засыпает, просыпается от сообщения любому из ботов.
    if webhook_base and len(bots) > 1:
        from webapp import StatusApp

        first_bot, first_entry = bots[0]
        app = StatusApp(first_bot.store, on_update=None, webhook_secret="",
                        bot_username=first_entry.get("username", ""),
                        engine=first_bot.engine, logger=first_bot.log)
        first_bot.attach_webhook(app, first_bot.webhook_path)
        for bot, _ in bots[1:]:
            bot.webhook_base = webhook_base
            bot.attach_webhook(app, "/telegram-%s" % bot.provider.name)
        actual_port = app.start(port)
        first_bot.log("Общий HTTP-сервер на порту %s, маршруты: %s"
                      % (actual_port, ", ".join(sorted(app.endpoints))))
        port = None      # сервер уже поднят — второй раз не поднимаем

    # Все боты запускаются в отдельных потоках, главный поток держит процесс живым.
    threads = []
    for bot, entry in bots:
        thread = threading.Thread(target=_run_bot, args=(bot, entry, entries),
                                  name="bot-%s" % bot.provider.name, daemon=True)
        thread.start()
        threads.append(thread)
        _watch(entry, entries, thread)
    try:
        for thread in threads:
            thread.join()
        return 0
    finally:
        for bot, _ in bots:
            try:
                bot.engine.stop()
            except Exception:
                pass


def _watch(entry, entries, thread):
    """Через несколько секунд отмечает бота как работающего, если он не упал."""
    def check():
        thread.join(timeout=15)
        if thread.is_alive():
            entry["state"] = "работает"
            write_status(entries)
    threading.Thread(target=check, daemon=True).start()


def _run_bot(bot, entry=None, entries=None, retry_seconds=60, max_retry_seconds=600):
    """Запускает бота и перезапускает его при сбое.

    Раньше одна сетевая ошибка (например сайт вуза не ответил за таймаут)
    убивала поток бота до следующего перезапуска сервиса — снаружи это выглядело
    как «бот не работает весь день». Теперь бот поднимается заново с паузой.
    """
    delay = retry_seconds
    while True:
        try:
            return bot.run()
        except Exception as error:
            message = str(error)[:160]
            if entry is not None and entries is not None:
                entry["state"] = "перезапуск после ошибки: %s" % message
                write_status(entries)
            bot.log("Бот %s упал: %s\n%s" % (bot.provider.name, error,
                                            traceback.format_exc(limit=3)))
            bot.log("Повторный запуск бота %s через %d с" % (bot.provider.name, delay))
            time.sleep(delay)
            delay = min(delay * 2, max_retry_seconds)


if __name__ == "__main__":
    sys.exit(main())
