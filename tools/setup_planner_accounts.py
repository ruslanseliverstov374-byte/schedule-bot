# -*- coding: utf-8 -*-
"""Настройка аккаунтов облачной «Планёрки»: переименовать владельца и завести
второй аккаунт с отдельным паролем.

Запуск:
    python tools/setup_planner_accounts.py --owner-password ПАРОЛЬ
    python tools/setup_planner_accounts.py --owner-password ПАРОЛЬ --name "София"
"""

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://schedule-bot-frgz.onrender.com"


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path):
        request = urllib.request.Request(BASE + path, headers={"User-Agent": "setup"})
        try:
            with self.opener.open(request, timeout=240) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def post(self, path, payload=None, form=None):
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload or {}, ensure_ascii=False).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            BASE + path, data=data, method="POST",
            headers={"Content-Type": content_type, "User-Agent": "setup"})
        try:
            with self.opener.open(request, timeout=240) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser(description="Настройка аккаунтов Планёрки")
    parser.add_argument("--owner-password", required=True)
    parser.add_argument("--owner-name", default="Руслан")
    parser.add_argument("--name", default="Рахимова София")
    parser.add_argument("--password", default="",
                        help="пароль второго аккаунта (пусто — сгенерировать)")
    arguments = parser.parse_args()

    owner = Client()
    status, body = owner.post("/planner/login",
                              form={"password": arguments.owner_password})
    print("вход владельца: HTTP %s" % status)
    if status not in (200, 303):
        print("Не удалось войти: %s" % body[:200])
        return 1
    status, body = owner.get("/planner/api/state")
    state = json.loads(body)
    account = state.get("account") or {}
    print("аккаунт владельца: %s (id=%s, владелец=%s)"
          % (account.get("name"), account.get("id"), account.get("is_owner")))
    if account.get("name") != arguments.owner_name:
        status, body = owner.post("/planner/api/accounts/%d/name" % account["id"],
                                  {"name": arguments.owner_name})
        print("переименование владельца: HTTP %s" % status)
    status, body = owner.get("/planner/api/accounts")
    accounts = json.loads(body).get("accounts") or []
    print("аккаунтов сейчас: %d (%s)"
          % (len(accounts), ", ".join(item["name"] for item in accounts)))

    existing = next((item for item in accounts
                     if item["name"] == arguments.name), None)
    if existing:
        print("аккаунт «%s» уже есть (id=%s)" % (arguments.name, existing["id"]))
        status, body = owner.post(
            "/planner/api/accounts/%d/password" % existing["id"], {})
        password = json.loads(body).get("password") or ""
        print("новый пароль: %s" % password)
        account_id = existing["id"]
    else:
        payload = {"name": arguments.name}
        if arguments.password:
            payload["password"] = arguments.password
        status, body = owner.post("/planner/api/accounts", payload)
        created = json.loads(body).get("account") or {}
        print("создание аккаунта: HTTP %s" % status)
        password = created.get("password") or ""
        account_id = created.get("id")
        print("аккаунт: %s | пароль: %s" % (created.get("name"), password))

    second = Client()
    status, body = second.post("/planner/login", form={"password": password})
    print("вход второго аккаунта: HTTP %s" % status)
    if status in (200, 303):
        status, body = second.get("/planner/api/state")
        state = json.loads(body)
        account = state.get("account") or {}
        print("   имя: %s | владелец: %s | задач: %s | группы: %s"
              % (account.get("name"), account.get("is_owner"),
                 (state.get("counts") or {}).get("tasks_total"),
                 [item.get("code") for item in state.get("groups") or []]))
    print("\nГотово. Второй аккаунт: %s, пароль: %s" % (arguments.name, password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
