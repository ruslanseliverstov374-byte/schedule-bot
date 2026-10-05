# -*- coding: utf-8 -*-
"""Сторож: поднимает бота, если он не работает.

Запускается планировщиком Windows каждые 10 минут (см. install_autostart.bat).
Проверяет блокировку единственного экземпляра: если файл не заблокирован —
значит бота нет, и сторож его запускает. Второй копии не будет.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from instance_lock import InstanceLock       # noqa: E402

LOCK_PATH = os.path.join(ROOT, "data", "bot.lock")
PID_PATH = LOCK_PATH + ".pid"
LOG_PATH = os.path.join(ROOT, "logs", "watchdog.log")


def log(message):
    import datetime
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def start_bot():
    """Запускает бота в фоне, без окна консоли (Windows — pythonw)."""
    executable = sys.executable
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(windowless):
            executable = windowless
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen([executable, "bot.py"], cwd=ROOT, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
    else:
        subprocess.Popen([executable, "bot.py"], cwd=ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
    log("Бот запущен сторожем (%s)" % executable)


def main():
    lock = InstanceLock(LOCK_PATH)
    if not lock.acquire():
        pid = InstanceLock.running_pid(PID_PATH)
        log("Бот работает (pid %s) — ничего не делаю" % (pid or "?"))
        return 0
    # Блокировка досталась нам: значит бот не запущен. Отпускаем и стартуем.
    lock.release()
    log("Бот не найден — запускаю")
    start_bot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
