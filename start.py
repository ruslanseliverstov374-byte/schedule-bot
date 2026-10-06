# -*- coding: utf-8 -*-
"""Точка входа для запуска на сервере: один бот или сразу два.

Логика простая:
  * если задан `BOT_TOKEN_KGASU` — запускаем оба бота в одном процессе (multi.py),
    чтобы уложиться в бесплатный лимит часов Render;
  * иначе — обычный одиночный бот (bot.py).

Так один и тот же образ подходит и для одного вуза, и для двух.
"""

import os
import sys

from bot import ROOT, env_value, main as single_main

KGASU_TOKEN_FILE = os.path.join(ROOT, "token-kgasu.txt")


def main():
    has_second_bot = bool(env_value("BOT_TOKEN_KGASU", "").strip()
                          or os.path.exists(KGASU_TOKEN_FILE))
    if has_second_bot:
        import multi
        return multi.main()
    return single_main()


if __name__ == "__main__":
    sys.exit(main())
