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
import os
import sys
import threading
import traceback

from bot import (DEFAULT_DB, ROOT, ScheduleBot, env_value, make_logger, read_token)
from snapshot import restore_from_chat
import tgbot

KGASU_DB = os.path.join(ROOT, "data", "kgasu.db")
KGASU_TOKEN_FILE = os.path.join(ROOT, "token-kgasu.txt")


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

    token_unifirst = env_value("BOT_TOKEN", "").strip() or read_token()
    token_kgasu = (env_value("BOT_TOKEN_KGASU", "").strip()
                   or read_token(token_file=KGASU_TOKEN_FILE))

    if not token_unifirst and not token_kgasu:
        print("Не найдены токены ботов (BOT_TOKEN и BOT_TOKEN_KGASU).")
        return 2

    bots = []
    if token_unifirst and "unifirst" in wanted:
        logger = make_logger(prefix="[ПГУФКСиТ]")
        restore_if_needed(token_unifirst, DEFAULT_DB, owner_id, logger)
        bots.append(ScheduleBot(token_unifirst, db_path=DEFAULT_DB,
                                refresh_minutes=refresh, logger=logger,
                                owner_id=owner_id, webhook_base=webhook_base,
                                port=port, university="unifirst"))
    if token_kgasu and "kgasu" in wanted:
        logger = make_logger(prefix="[КГАСУ]")
        restore_if_needed(token_kgasu, KGASU_DB, owner_id, logger)
        # Второй бот идёт опросом: порт занят первым ботом.
        bots.append(ScheduleBot(token_kgasu, db_path=KGASU_DB,
                                refresh_minutes=max(refresh, 30), logger=logger,
                                owner_id=owner_id, university="kgasu"))

    if not bots:
        print("Нечего запускать.")
        return 2

    # Первый бот (с вебхуком) — в главном потоке, остальные — в своих.
    threads = []
    for bot in bots[1:]:
        thread = threading.Thread(target=_run_bot, args=(bot,), name="bot-%s" % bot.provider.name,
                                  daemon=True)
        thread.start()
        threads.append(thread)
    try:
        return _run_bot(bots[0])
    finally:
        for bot in bots:
            try:
                bot.engine.stop()
            except Exception:
                pass


def _run_bot(bot):
    try:
        return bot.run()
    except Exception as error:
        bot.log("Бот %s упал: %s\n%s" % (bot.provider.name, error,
                                        traceback.format_exc(limit=3)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
