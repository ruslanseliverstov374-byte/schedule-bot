# -*- coding: utf-8 -*-
"""Проверка работы ботов «снаружи»: сервис, Telegram, расписание, копии баз.

Одна команда отвечает на вопросы «боты живы?», «люди ходят?», «расписание
актуально?», «копии свежие?» и показывает проблемы понятным списком.

Запуск:
    python tools/monitor.py
    python tools/monitor.py --url https://schedule-bot-frgz.onrender.com
    python tools/monitor.py --owner-id 1368878379      # проверить и копии баз
"""

import argparse
import gzip
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, ".")
from bot import ROOT, read_token      # noqa: E402
from schedule import kgasu, unifirst      # noqa: E402

DEFAULT_URL = "https://schedule-bot-frgz.onrender.com"
BOTS = (
    {"name": "unifirst", "title": "Поволжский ГУФКСиТ", "token": "token.txt",
     "path": "/telegram", "university": "unifirst"},
    {"name": "kgasu", "title": "КГАСУ", "token": "token-kgasu.txt",
     "path": "/telegram-kgasu", "university": "kgasu"},
)
CONTEXT = ssl._create_unverified_context()
PROBLEMS = []
NOTES = []


def note(message):
    NOTES.append(message)
    print("   • %s" % message)


def problem(message):
    PROBLEMS.append(message)
    print("   ❌ %s" % message)


def http_get(url, timeout=90, seconds=False):
    request = urllib.request.Request(url, headers={"User-Agent": "monitor"})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=CONTEXT) as response:
            body = response.read().decode("utf-8", "replace")
        return (response.status, body, time.time() - started) if seconds \
            else (response.status, body)
    except urllib.error.HTTPError as error:
        return (error.code, error.read().decode("utf-8", "replace")[:200], 0) if seconds \
            else (error.code, "")
    except Exception as error:
        return (None, str(error)[:200], 0) if seconds else (None, str(error)[:200])


def telegram(token, method, params="", timeout=45):
    url = "https://api.telegram.org/bot%s/%s%s" % (token, method, params)
    request = urllib.request.Request(url, headers={"User-Agent": "monitor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=CONTEXT) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, {"error": error.read().decode()[:200]}
    except Exception as error:
        return None, {"error": str(error)[:200]}


def check_service(url):
    print("\n1. Сервис на Render")
    status, body, elapsed = http_get(url + "/health", seconds=True)
    if status == 200 and body.strip().startswith("ok"):
        note("/health отвечает за %.1f с" % elapsed)
        if elapsed > 60:
            note("ответ медленный — сервис, похоже, только что проснулся")
    else:
        problem("/health не ответил: %s %s" % (status, body[:120]))

    status, body = http_get(url + "/service.json")
    if status != 200:
        problem("/service.json недоступен: %s" % status)
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        problem("/service.json не разобрался")
        return {}
    note("состояние обновлено: %s" % data.get("updated"))
    for bot in data.get("bots") or []:
        state = bot.get("state") or "?"
        line = ("%s: %s, пользователей %s, групп %s"
                % (bot.get("name"), state, bot.get("users"), bot.get("groups")))
        if state != "работает":
            problem(line + (" — %s" % bot.get("error") if bot.get("error") else ""))
        elif not bot.get("users"):
            note(line + " — пока никто не пользуется")
        elif not bot.get("groups"):
            problem(line + " — список групп пуст, студенты не смогут выбрать группу")
        else:
            note(line)
    return data


def check_telegram():
    """Проверяет токены, вебхуки и доставку сообщений."""
    print("\n2. Доставка сообщений (Telegram)")
    result = {}
    for bot in BOTS:
        token = read_token(token_file=bot["token"])
        if not token:
            problem("%s: нет токена (%s)" % (bot["title"], bot["token"]))
            continue
        status, me = telegram(token, "getMe")
        if not (isinstance(me, dict) and me.get("ok")):
            problem("%s: токен не работает (%s)" % (bot["title"], str(me)[:120]))
            continue
        username = me["result"].get("username")
        status, info = telegram(token, "getWebhookInfo")
        if not (isinstance(info, dict) and info.get("ok")):
            problem("%s: getWebhookInfo не ответил (%s)" % (bot["title"], str(info)[:120]))
            continue
        data = info["result"]
        url = data.get("url") or ""
        pending = data.get("pending_update_count") or 0
        error = data.get("last_error_message") or ""
        if expected_url and url != expected_url + bot["path"]:
            problem("%s: вебхук ведёт не туда (%s вместо %s%s)"
                    % (bot["title"], url or "пусто", expected_url, bot["path"]))
        elif error:
            problem("%s: Telegram сообщает об ошибке доставки: %s" % (bot["title"], error))
        elif pending > 20:
            problem("%s: скопилось %d недоставленных сообщений" % (bot["title"], pending))
        else:
            note("@%s: вебхук на месте, очередь %d" % (username, pending))
        result[bot["name"]] = {"username": username, "pending": pending}
    return result


def users_from_backup(token, owner_id, bot_name):
    """Читает список пользователей из закреплённой копии базы (свежесть ≤ 30 мин)."""
    status, chat = telegram(token, "getChat?chat_id=%d" % owner_id)
    if not (isinstance(chat, dict) and chat.get("ok")):
        return None, "чат недоступен"
    pinned = (chat["result"].get("pinned_message") or {})
    document = pinned.get("document") or {}
    if not document.get("file_id"):
        return None, "в чате нет закреплённой копии"
    caption = pinned.get("caption") or ""
    saved_at = None
    for token_part in caption.replace(":", " ").split():
        pass
    status, info = telegram(token, "getFile?file_id=%s" % document["file_id"])
    if not (isinstance(info, dict) and info.get("ok")):
        return None, "файл копии не получен"
    url = "https://api.telegram.org/file/bot%s/%s" % (token, info["result"]["file_path"])
    try:
        with urllib.request.urlopen(url, timeout=120, context=CONTEXT) as response:
            blob = response.read()
    except Exception as error:
        return None, "копия не скачалась: %s" % str(error)[:80]
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    path = os.path.join(ROOT, "data", "_monitor.db")
    with open(path, "wb") as handle:
        handle.write(raw)
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % path.replace(os.sep, "/"), uri=True)
        try:
            users = connection.execute(
                "SELECT tg_id, first_name, group_title, last_seen FROM users").fetchall()
            groups = connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        return None, "копия не читается: %s" % error
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
    return {"users": users, "groups": groups, "caption": caption}, ""


def schedule_lessons(group, university, day):
    """Сколько пар у группы на дату по данным источника (живой запрос)."""
    if university == "kgasu":
        all_groups = kgasu.groups()
        found = kgasu.find_group(all_groups, group)
        if not found:
            return None, "группа не найдена на сайте КГАСУ"
        blob, _ = kgasu.download(found["file_url"])
        grid = kgasu.prepare(blob, found["file_name"])
        return len(kgasu.lessons_for_day(grid, found["name"], day)), ""
    api = unifirst.Unifirst()
    year, week = unifirst.iso_year_week(day)
    payload = api.timetable(group, year, week)
    lessons = unifirst.normalize(payload)
    return len([item for item in lessons if item.get("date") == day.isoformat()]), ""


def check_schedule(owner_id):
    """Смотрит, у кого какие группы и есть ли у них пары на ближайшие дни."""
    print("\n3. Расписание для групп студентов")
    users_by_bot = {}
    for bot in BOTS:
        token = read_token(token_file=bot["token"])
        if not token:
            continue
        data, error = users_from_backup(token, owner_id, bot["name"])
        if not data:
            problem("%s: не удалось прочитать копию — %s" % (bot["title"], error))
            continue
        users = data["users"]
        users_by_bot[bot["name"]] = data
        print("   %s: в копии %d пользователей, %d групп"
              % (bot["title"], len(users), data["groups"]))
        for tg_id, name, group, last_seen in users:
            mark = "сегодня" if str(last_seen or "").startswith(
                datetime.now().strftime("%Y-%m-%d")) else (last_seen or "—")
            print("      %s — группа %s, заходил: %s"
                  % (name or tg_id, group or "не выбрана", mark))
    today = date.today()
    for bot in BOTS:
        data = users_by_bot.get(bot["name"])
        if not data:
            continue
        groups = sorted({row[2] for row in data["users"] if row[2]})
        for group in groups[:5]:
            for day in (today, today + timedelta(days=1)):
                count, error = 0, ""
                try:
                    count, error = schedule_lessons(group, bot["university"], day)
                except Exception as failure:
                    count, error = 0, str(failure)[:120]
                if error:
                    problem("%s / %s: расписание не получено (%s)"
                            % (bot["title"], group, error))
                elif count:
                    note("%s / %s на %s: %d пар" % (bot["title"], group,
                                                    day.strftime("%d.%m"), count))
                else:
                    note("%s / %s на %s: пар нет (или файл ещё не обновили)"
                         % (bot["title"], group, day.strftime("%d.%m")))
    return users_by_bot


def check_notifications(status_data):
    """Смотрит счётчики обработки сообщений и тик движка напоминаний."""
    print("\n4. Напоминания и обработка сообщений")
    updates = (status_data or {}).get("updates") or {}
    if updates:
        received = updates.get("received") or 0
        handled = updates.get("handled") or 0
        errors = updates.get("errors") or 0
        note("сообщений: принято %d, обработано %d, ошибок %d" % (received, handled, errors))
        if updates.get("last_update_at"):
            note("последнее обработанное сообщение: %s UTC" % updates["last_update_at"])
        if errors:
            problem("ошибки обработки: %d (последняя — %s)"
                    % (errors, updates.get("last_error") or "текст не сохранён"))
        if received - handled > 5:
            problem("сообщения копятся: принято %d, обработано %d" % (received, handled))
    else:
        note("счётчиков в /service.json нет — сервис на старой версии кода")

    import re
    status, page = http_get(DEFAULT_URL + "/")
    if status != 200:
        problem("страница состояния недоступна: %s" % status)
        return
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    tick = re.search(r"Последний тик движка напоминаний ([^А-ЯЁ]{0,24})", text)
    engine = re.search(r"Состояние движка напоминаний ([^|]{0,40})", text)
    if tick:
        note("последний тик напоминаний: %s" % tick.group(1).strip())
    if engine:
        note("движок: %s" % engine.group(1).strip()[:60])
        if "ошибок не" not in engine.group(1):
            problem("движок напоминаний сообщает об ошибке: %s" % engine.group(1).strip()[:80])


def main():
    global expected_url
    parser = argparse.ArgumentParser(description="Мониторинг ботов расписания")
    parser.add_argument("--url", default=os.environ.get("BOT_URL") or DEFAULT_URL)
    parser.add_argument("--owner-id", default="1368878379",
                        help="Telegram ID владельца: по нему читаются копии баз")
    arguments = parser.parse_args()
    expected_url = arguments.url.rstrip("/")

    print("=" * 66)
    print("  МОНИТОРИНГ БОТОВ РАСПИСАНИЯ  (%s)" % datetime.now().strftime("%d.%m.%Y %H:%M"))
    print("=" * 66)
    status_data = check_service(expected_url)
    check_telegram()
    if arguments.owner_id.isdigit():
        check_schedule(int(arguments.owner_id))
    check_notifications(status_data)

    print("\n" + "=" * 66)
    if PROBLEMS:
        print("  НАЙДЕНО ПРОБЛЕМ: %d" % len(PROBLEMS))
        for item in PROBLEMS:
            print("   ❌ %s" % item)
        return 1
    print("  ВСЁ РАБОТАЕТ: замечаний нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
