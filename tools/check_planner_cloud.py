# -*- coding: utf-8 -*-
"""Проверка облачной «Планёрки»: достаёт пароль из закреплённой копии базы
и проходит вход/API как настоящий пользователь.

Запуск:
    python tools/check_planner_cloud.py --owner-id 1368878379
"""

import argparse
import gzip
import http.cookiejar
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from bot import ROOT, read_token      # noqa: E402

CONTEXT = ssl._create_unverified_context()
BASE = "https://schedule-bot-frgz.onrender.com"


def api(token, method):
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        headers={"User-Agent": "planner-check"})
    with urllib.request.urlopen(request, timeout=60, context=CONTEXT) as response:
        return json.loads(response.read().decode())


def read_password_from_backup(owner_id):
    token = read_token(token_file="token.txt")
    chat = api(token, "getChat?chat_id=%d" % owner_id)
    pinned = ((chat.get("result") or {}).get("pinned_message") or {})
    document = pinned.get("document") or {}
    if not document.get("file_id"):
        return None, "в чате нет закреплённой копии базы"
    info = api(token, "getFile?file_id=%s" % document["file_id"])
    if not info.get("ok"):
        return None, "файл копии не получен"
    url = "https://api.telegram.org/file/bot%s/%s" % (token, info["result"]["file_path"])
    with urllib.request.urlopen(url, timeout=120, context=CONTEXT) as response:
        blob = response.read()
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    path = os.path.join(ROOT, "data", "_planner-check.db")
    with open(path, "wb") as handle:
        handle.write(raw)
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                                     uri=True, timeout=5)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'planner_%'")]
            if "planner_settings" not in tables:
                return None, "копия базы ещё без планировщика (ждём снапшот)"
            row = connection.execute(
                "SELECT value FROM planner_settings WHERE key='planner_password'"
            ).fetchone()
            return (row[0] if row else None), ""
        finally:
            connection.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path):
        request = urllib.request.Request(BASE + path, headers={"User-Agent": "check"})
        try:
            with self.opener.open(request, timeout=180) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def post(self, path, payload, form=False):
        if form:
            data = urllib.parse.urlencode(payload).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            BASE + path, data=data, headers={"Content-Type": content_type,
                                             "User-Agent": "check"})
        try:
            with self.opener.open(request, timeout=180) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser(description="Проверка облачной Планёрки")
    parser.add_argument("--owner-id", default="1368878379")
    parser.add_argument("--wait-minutes", type=float, default=0,
                        help="сколько минут ждать появления пароля в копии базы")
    arguments = parser.parse_args()
    deadline = time.time() + arguments.wait_minutes * 60
    password = None
    reason = ""
    while True:
        password, reason = read_password_from_backup(int(arguments.owner_id))
        if password:
            break
        if time.time() >= deadline:
            print("Пароль в копии базы не найден: %s" % reason)
            return 1
        print("… ждём снапшот базы (%s)" % reason)
        time.sleep(30)
    print("Пароль из копии базы: %s" % password)

    client = Client()
    status, body = client.get("/planner")
    print("/planner без входа: HTTP %s" % status)
    status, body = client.post("/planner/login", {"password": password}, form=True)
    print("вход: HTTP %s" % status)
    status, body = client.get("/planner")
    print("/planner после входа: HTTP %s, интерфейс: %s"
          % (status, "Планёрка" in body))
    status, body = client.get("/planner/api/state")
    state = json.loads(body)
    print("state: HTTP %s, сегодня %s, пар сегодня %d, свободно %d мин"
          % (status, (state.get("today") or {}).get("date"),
             len((state.get("today") or {}).get("pairs", [])),
             (state.get("today") or {}).get("free_minutes", 0)))
    print("      группы:", [item.get("code") for item in state.get("groups") or []])
    status, body = client.get("/planner/api/groups")
    groups = json.loads(body)
    print("groups: HTTP %s, университетов %d"
          % (status, len(groups.get("universities") or [])))
    status, body = client.get("/planner/api/plan?days=7")
    plan = json.loads(body)
    print("plan: HTTP %s, дней %d, блоков %d"
          % (status, len(plan.get("days") or []),
             (plan.get("totals") or {}).get("planned_blocks", 0)))
    status, body = client.get("/planner/api/stats")
    stats = json.loads(body)
    print("stats: HTTP %s, неделя %d, месяц %d"
          % (status, len(stats.get("week") or []), len(stats.get("month") or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
