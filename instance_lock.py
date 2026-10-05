# -*- coding: utf-8 -*-
"""Блокировка единственного экземпляра бота.

Файл блокируется операционной системой; когда процесс умирает, блокировка
снимается автоматически. Так сторож (tools/watchdog.py) понимает, работает ли бот,
а второй экземпляр не пытается одновременно читать getUpdates у Telegram
(иначе Telegram отвечает Conflict: terminated by other getUpdates request).
"""

import os

# Блокируем байт в стороне от начала файла: так PID в начале файла
# может прочитать кто угодно (например, скрипты автозапуска), а блокировка работает.
LOCK_OFFSET = 4096

try:                                    # Windows
    import msvcrt

    def _try_lock(handle):
        handle.seek(LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
except ImportError:                     # Linux/macOS
    import fcntl

    def _try_lock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class InstanceLock:
    def __init__(self, path, pid_path=None):
        self.path = path
        self.pid_path = pid_path or (path + ".pid")
        self.handle = None

    def acquire(self):
        """True — блокировка получена (бот не запущен), False — уже занято."""
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _try_lock(handle)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(b"locked")          # содержимое не важно, важен сам факт блокировки
        handle.flush()
        self.handle = handle
        try:                             # PID — в отдельный файл, его читают скрипты
            with open(self.pid_path, "w", encoding="ascii") as pid_file:
                pid_file.write(str(os.getpid()))
        except OSError:
            pass
        return True

    @staticmethod
    def running_pid(pid_path):
        """PID работающего бота из файла (или None)."""
        try:
            with open(pid_path, "r", encoding="ascii", errors="ignore") as handle:
                value = handle.read().strip()
            return int(value) if value.isdigit() else None
        except (OSError, ValueError):
            return None

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.close()          # закрытие файла снимает блокировку
        except Exception:
            pass
        self.handle = None
        try:
            if os.path.exists(self.pid_path):
                os.remove(self.pid_path)
        except OSError:
            pass
