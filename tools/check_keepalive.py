# -*- coding: utf-8 -*-
"""Проверка пинга Render: запускает keepalive-workflow и показывает результат.

Пинг нужен, чтобы бесплатный сервис Render не засыпал: раз в 5 минут GitHub
обращается к /health. Этот инструмент запускает проверку немедленно (workflow_dispatch)
и печатает, что ответил сервис.

Запуск:
    python tools/check_keepalive.py --repo owner/schedule-bot --token-file путь\\к\\токену
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

API = "https://api.github.com"


def api(method, url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "keepalive-check",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("HTTP %s: %s" % (error.code, detail))


def fetch_logs(url, token):
    """Скачивает zip с логами запуска и возвращает строки, где есть health."""
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "keepalive-check",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    lines = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode("utf-8", "replace")
            for line in text.splitlines():
                if "health" in line.lower() or "->" in line:
                    lines.append(line.strip())
    return lines


def main():
    parser = argparse.ArgumentParser(description="Проверка пинга Render через GitHub Actions")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", default="keepalive.yml")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token", help="токен GitHub")
    parser.add_argument("--token-file", help="файл с токеном")
    parser.add_argument("--wait", type=int, default=150, help="сколько секунд ждать запуск")
    arguments = parser.parse_args()

    token = arguments.token or ""
    if not token and arguments.token_file and os.path.exists(arguments.token_file):
        with open(arguments.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    if not token:
        print("Нужен токен GitHub (--token-file).")
        return 2

    base = "%s/repos/%s/actions" % (API, arguments.repo)
    print("Запускаю workflow %s..." % arguments.workflow)
    try:
        api("POST", "%s/workflows/%s/dispatches" % (base, arguments.workflow),
            token, {"ref": arguments.branch})
    except RuntimeError as error:
        print("⚠️ Не удалось запустить вручную: %s" % error)
        print("   Ничего страшного: расписание всё равно запустит его в ближайшие 5 минут.")
        return 1

    print("Жду появления запуска (до %d с)..." % arguments.wait)
    run = None
    deadline = time.time() + arguments.wait
    while time.time() < deadline:
        time.sleep(6)
        runs = api("GET", "%s/workflows/%s/runs?per_page=5" % (base, arguments.workflow), token)
        items = runs.get("workflow_runs") or []
        if items:
            run = items[0]
            if run.get("status") == "completed":
                break
    if not run:
        print("Запуск не появился. Проверьте вкладку Actions в репозитории.")
        return 1

    print("Запуск: %s" % run.get("html_url"))
    print("Статус: %s | результат: %s" % (run.get("status"), run.get("conclusion")))
    if run.get("conclusion") != "success":
        print("⚠️ Пинг не удался — возможно, сервис ещё просыпается. Повторите через минуту.")
        return 1
    try:
        for line in fetch_logs("%s/runs/%s/logs" % (base, run["id"]), token):
            print("   ", line)
    except Exception as error:
        print("   (логи недоступны: %s)" % error)
    print("\n✅ Пинг работает: сервис отвечает, бесплатный Render не заснёт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
