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

SKIP_DIRS = {".git", "__pycache__", "data", "logs", ".venv", "venv", ".idea", ".vscode",
             ".cache"}
SKIP_FILES = {"token.txt", "token-kgasu.txt", ".env", "_autostart.ps1"}
SKIP_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".zip", ".log", ".gz", ".token")


def is_secret(name):
    """Файлы, которые НИКОГДА не должны уезжать в репозиторий.

    Отдельная проверка появилась после случая, когда файл с токеном второго бота
    (token-kgasu.txt) не попал в список исключений и оказался в публичном
    репозитории — теперь любые «token*», «*secret*», «*.key» и «*.pem» отсекаются.
    """
    lowered = name.lower()
    return (lowered in SKIP_FILES or "token" in lowered or "secret" in lowered
            or lowered.endswith((".key", ".pem", ".env")))


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


def push_tree(repo_url, token, files, message, branch="main", log=print):
    """Заливает все файлы одним коммитом через Git Data API.

    Так надёжнее и быстрее, чем по одному файлу через Contents API: GitHub
    иногда отвечает 409/500 на «правила репозитория» при файловой заливке,
    а один коммит (blobs -> tree -> commit -> ref) проходит целиком.
    """
    import base64

    ref = api("GET", "%s/git/ref/heads/%s" % (repo_url, branch), token)
    base_commit = ref["object"]["sha"]
    commit = api("GET", "%s/git/commits/%s" % (repo_url, base_commit), token)
    base_tree = commit["tree"]["sha"]
    log("Базовый коммит: %s" % base_commit[:10])

    tree = []
    for index, (path, blob) in enumerate(files, start=1):
        payload = {"content": base64.b64encode(blob).decode("ascii"),
                   "encoding": "base64"}
        result = api("POST", "%s/git/blobs" % repo_url, token, payload)
        tree.append({"path": path, "mode": "100644", "type": "blob",
                     "sha": result["sha"]})
        if index % 20 == 0 or index == len(files):
            log("  подготовлено файлов: %d/%d" % (index, len(files)))

    new_tree = api("POST", "%s/git/trees" % repo_url, token,
                   {"base_tree": base_tree, "tree": tree})
    new_commit = api("POST", "%s/git/commits" % repo_url, token,
                     {"message": message, "tree": new_tree["sha"],
                      "parents": [base_commit]})
    api("PATCH", "%s/git/refs/heads/%s" % (repo_url, branch), token,
        {"sha": new_commit["sha"]})
    return new_commit["sha"]


def collect_files(root=ROOT):
    """Список (относительный путь, байты) — без секретов, баз и служебного мусора."""
    files = []
    for current, directories, names in os.walk(root):
        # Служебные папки пропускаем, но .github (workflow с пингом) нужен.
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
        for name in sorted(names):
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES) or is_secret(name):
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

    print("\nЗаливаю в ветку %s одним коммитом..." % arguments.branch)
    try:
        commit_sha = push_tree(repo_url, token, files, arguments.message,
                               arguments.branch, log=print)
    except GitHubError as error:
        hint = ""
        if error.code in (403, 404):
            hint = ("\n   Похоже, у токена нет права Contents: Read and write "
                    "на этот репозиторий (или он не добавлен в доступ токена).\n"
                    "   GitHub → Settings → Developer settings → Personal access tokens "
                    "→ выбрать токен → Repository access и Contents: Read and write.")
        print("❌ Заливка не удалась: %s%s" % (error, hint))
        return 1
    print("\n✅ Готово (%d файлов, коммит %s): https://github.com/%s/tree/%s"
          % (len(files), commit_sha[:10], arguments.repo, arguments.branch))
    return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GitHubError as error:
        print("\n❌ Ошибка GitHub: %s" % error)
        sys.exit(1)
