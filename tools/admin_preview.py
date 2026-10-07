# -*- coding: utf-8 -*-
"""Показывает админские экраны на реальной базе — без Telegram.

Запуск:
    python tools/admin_preview.py                 # база по умолчанию (data/bot.db)
    python tools/admin_preview.py data/kgasu.db   # база бота КГАСУ
    python tools/admin_preview.py data/bot.db --query софа
"""

import argparse
import os
import re
import sys

sys.path.insert(0, ".")
import report      # noqa: E402
import texts       # noqa: E402
from store import Store      # noqa: E402


def plain(markup):
    """Убирает HTML-теги: в консоли видно то же, что в Telegram, но без разметки."""
    return re.sub(r"<[^>]+>", "", markup or "")


def main():
    parser = argparse.ArgumentParser(description="Предпросмотр админ-экранов")
    parser.add_argument("db", nargs="?", default="data/bot.db", help="файл базы бота")
    parser.add_argument("--query", default="", help="поиск по пользователям")
    parser.add_argument("--page", type=int, default=0)
    arguments = parser.parse_args()

    if not os.path.exists(arguments.db):
        print("Нет базы %s" % arguments.db)
        return 2
    store = Store(arguments.db)
    provider = store.get_meta("provider") or "unifirst"
    title = report.BOT_TITLES.get(provider, provider)

    print("=" * 66)
    print("  БАЗА: %s | вуз: %s | пользователей: %d"
          % (arguments.db, title, store.count_users()))
    print("=" * 66)

    print("\n----- /stats -----\n")
    print(plain(texts.admin_stats_page(report.summary(store), title)))

    limit = 10
    total = report.count_users(store, arguments.query)
    pages = max(1, (total + limit - 1) // limit)
    rows = report.users_rows(store, limit=limit, offset=arguments.page * limit,
                             query=arguments.query)
    print("\n----- /users -----\n")
    print(plain(texts.users_page(rows, arguments.page, pages, total, title,
                                 arguments.query)))

    print("\n----- /users csv (первые строки) -----\n")
    body = report.users_csv(report.users_rows(store, order="created"),
                            bot_title=title, include_bot=True)
    for line in body.lstrip("\ufeff").splitlines()[:6]:
        print("  " + line)
    print("  ... всего строк: %d" % (len(body.strip().splitlines()) - 1))

    print("\n----- /broadcasts -----\n")
    print(plain(texts.broadcasts_history(store.recent_broadcasts(5), title)))

    print("\n----- выбор получателей (/broadcast) -----\n")
    groups = {row["group_title"]: row["n"] for row in store.query(
        "SELECT group_title, COUNT(*) AS n FROM users WHERE group_title<>''"
        " GROUP BY group_title ORDER BY n DESC LIMIT 6")}
    counts = {"all": len(store.all_users()), "active": len(store.active_users(7))}
    print(plain(texts.broadcast_targets(counts, groups)))
    for name, count in groups.items():
        print("   [%s — %d]" % (name, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
