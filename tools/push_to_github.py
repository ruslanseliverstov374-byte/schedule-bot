# -*- coding: utf-8 -*-
"""Заливка проекта в GitHub через REST API — без git, SSH и установки пакетов.

Нужна в двух случаях:
  1) когда git в системе недоступен или не может подключиться (ограничения сети,
     песочницы, отсутствует «Клиент OpenSSH»);
  2) чтобы обновлять код бота одной командой прямо из папки проекта.

Используется Contents API: по одному запросу на файл. У fine-grained токенов
Git Data API (blobs/trees/commits) часто запрещён, а Contents обычно разрешён —
поэтому выбран он.

Запуск (из корня проекта):
    python tools/push_to_github.py --repo owner/name --token-file путь\\к\\токену
    python tools/push_to_github.py --repo owner/name --dry-run      # только список файлов
    python tools/push_to_github.py --repo owner/name --message "правки" --delay 1

Токен нужен с правом Contents: Read and write (и Workflows: Read and write, если
меняются файлы в .github/workflows). Файл токена в репозиторий не попадает.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "__pycache__", "data", "logs", ".venv", "venv", ".idea", ".vscode"}
SKIP_FILES = {"token.txt", ".env", "_autostart.ps1"}
SKIP_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".zip", ".log")


class GitHubError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def api(method, url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "schedule-bot-push",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise GitHubError("HTTP %s: %s" % (error.code, detail), error.code)
    except Exception as error:
        raise GitHubError("нет связи с GitHub: %s" % error)


def collect_files(root=ROOT):
    """Список (относительный путь, байты) — без секретов, баз и служебного мусора."""
    files = []
    for current, directories, names in os.walk(root):
        # Служебные папки пропускаем, но .github (workflow с пингом) нужен.
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
        for name in sorted(names):
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(current, name)
            relative = os.path.relpath(full, root).replace("\\", "/")
            with open(full, "rb") as handle:
                files.append((relative, handle.read()))
    return files


def read_token(path=None, inline=None):
    if inline:
        return inline.strip()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    environment = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if environment:
        return environment.strip()
    local = os.path.join(ROOT, "data", "github_token.txt")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def file_sha(repo_url, token, path, branch):
    """sha текущей версии файла (None, если файла в репозитории ещё нет)."""
    url = "%s/contents/%s?ref=%s" % (repo_url, urllib.parse.quote(path), branch)
    try:
        info = api("GET", url, token)
        return info.get("sha")
    except GitHubError as error:
        if error.code == 404:
            return None
        raise


def put_file(repo_url, token, path, content, message, branch):
    """Создать или обновить один файл. Возвращает 'создан' либо 'обновлён'."""
    url = "%s/contents/%s" % (repo_url, urllib.parse.quote(path))
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    sha = file_sha(repo_url, token, path, branch)
    if sha:
        payload["sha"] = sha
    api("PUT", url, token, payload)
    return "обновлён" if sha else "создан"


def main():
    parser = argparse.ArgumentParser(description="Заливка проекта в GitHub через API")
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token", help="токен GitHub")
    parser.add_argument("--token-file", help="файл с токеном")
    parser.add_argument("--message", default="Обновление бота расписания")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="пауза между файлами, секунды (лимиты GitHub)")
    parser.add_argument("--dry-run", action="store_true", help="только показать файлы")
    parser.add_argument("--root", default=ROOT, help="папка проекта")
    arguments = parser.parse_args()

    files = collect_files(arguments.root)
    print("Файлов к заливке: %d" % len(files))
    if arguments.dry_run:
        for path, content in files:
            print("   %-42s %6d байт" % (path, len(content)))
        print("\nЭто предпросмотр (--dry-run): ничего не отправлено.")
        return 0

    token = read_token(arguments.token_file, arguments.token)
    if not token:
        print("Не найден токен GitHub: передайте --token-file или --token.")
        return 2

    repo_url = "%s/repos/%s" % (API, arguments.repo)
    try:
        repo = api("GET", repo_url, token)
        print("Репозиторий: %s | ветка: %s | права: %s"
              % (repo.get("full_name"), repo.get("default_branch"),
                 ", ".join(name for name, value in (repo.get("permissions") or {}).items()
                           if value) or "не видны"))
    except GitHubError as error:
        print("❌ Не удалось открыть репозиторий: %s" % error)
        return 1

    print("\nЗаливаю в ветку %s..." % arguments.branch)
    created = updated = 0
    for index, (path, content) in enumerate(files, 1):
        try:
            result = put_file(repo_url, token, path, content,
                              arguments.message, arguments.branch)
        except GitHubError as error:
            hint = ""
            if error.code in (403, 404):
                hint = ("\n   Похоже, у токена нет права Contents: Read and write "
                        "на этот репозиторий (или он не добавлен в доступ токена).\n"
                        "   GitHub → Settings → Developer settings → Personal access tokens "
                        "→ выбрать токен → Repository access и Contents: Read and write.")
            print("❌ %s: %s%s" % (path, error, hint))
            return 1
        if result == "создан":
            created += 1
        else:
            updated += 1
        if index % 10 == 0 or index == len(files):
            print("   %d/%d (создано %d, обновлено %d)"
                  % (index, len(files), created, updated))
        if arguments.delay:
            time.sleep(arguments.delay)

    print("\n✅ Готово: https://github.com/%s/tree/%s" % (arguments.repo, arguments.branch))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GitHubError as error:
        print("\n❌ Ошибка GitHub: %s" % error)
        sys.exit(1)
