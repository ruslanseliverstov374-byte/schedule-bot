# -*- coding: utf-8 -*-
"""Резервная копия базы бота в закреплённом сообщении чата владельца.

Зачем это нужно
---------------
На бесплатных тарифах (Render и подобные) файловая система контейнера временная:
перезапуск — и data/bot.db исчезла. Зато чат в Telegram никуда не девается.
Поэтому бот периодически отправляет владельцу сжатый снимок базы, закрепляет это
сообщение (прошлую копию удаляет, чтобы не мусорить), а после перезапуска
поднимает базу из закреплённого файла.

Что лежит в снимке
------------------
Вся база SQLite целиком: пользователи и их группы, домашние задания с голосами,
настройки напоминаний, кэш расписания, изменения и журнал отправок (таблица sent).
Файл сжимается gzip (уровень 6) и называется schedule-backup.db.gz — такие файлы
Telegram охотно отдаёт документом.

Зависимостей нет: только стандартная библиотека. В сеть модуль ходит
исключительно через переданный клиент Telegram (см. tgbot.py).
"""

import gc
import gzip
import io
import os
import shutil
import sqlite3
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

#: Имя файла копии: по нему Telegram показывает документ в чате.
BACKUP_FILENAME = "schedule-backup.db.gz"

#: Уровень сжатия gzip для снимка.
GZIP_LEVEL = 6

#: Больше этого размера снимок не принимаем (и сжатый, и распакованный).
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024

#: Начало подписи закреплённого сообщения с копией.
CAPTION_PREFIX = "💾 Копия базы расписания:"

#: Ключ meta, в котором хранится id закреплённого сообщения с копией.
META_MESSAGE_ID = "snapshot_message_id"
META_SAVED_AT = "snapshot_saved_at"
META_SAVES_COUNT = "snapshot_saves_count"
#: Сколько пользователей было в последней сохранённой копии: нужно, чтобы
#: не перезаписать хорошую копию пустой базой после неудачного перезапуска.
META_SAVED_USERS = "snapshot_users"

#: Сколько секунд между проверками в фоновом потоке.
CHECK_INTERVAL_SECONDS = 60

#: Через сколько секунд после важного изменения (ДЗ, смена группы) обновить копию.
#: Небольшая задержка объединяет несколько быстрых правок в одну копию.
SAVE_REQUEST_DELAY_SECONDS = 45

#: Минимальная пауза между копиями по запросу — чтобы не дёргать Telegram зря.
MIN_SAVE_GAP_SECONDS = 90

#: Свой заголовок файла SQLite.
SQLITE_MAGIC = b"SQLite format 3"


class SnapshotError(Exception):
    """Снимок базы сделать не удалось (нет файла, нет доступа, сломанная база)."""


# --------------------------------------------------------------- мелкие утилиты

def _remove(path):
    """Удаляет файл, молча пропуская любые проблемы (файла нет, занят и т.п.)."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _message_id_of(message):
    """Достаёт message_id из ответа Telegram (словарь или сразу число)."""
    value = message.get("message_id") if isinstance(message, dict) else message
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(stamp):
    """Разбирает «2025-03-12 14:30:00» (UTC) в метку времени. 0.0 — не разобралось."""
    if not stamp:
        return 0.0
    try:
        moment = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return moment.timestamp()


def _utc_text(moment=None):
    """Дата и время по UTC в виде «12.03.2025 14:30» — для подписи и логов."""
    moment = moment or datetime.now(timezone.utc)
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(moment, timezone.utc)
    return moment.strftime("%d.%m.%Y %H:%M")


# ------------------------------------------------------------------ сохранение

def _consistent_copy(source, destination):
    """Делает консистентную копию базы в destination.

    Основной путь — VACUUM INTO: SQLite сам собирает целостную копию и учитывает
    данные, которые ещё лежат в журнале WAL. Если сборка SQLite старая и команду
    не понимает, сбрасываем WAL на диск и копируем файл побайтово.
    """
    vacuum_error = None
    try:
        connection = sqlite3.connect(source, timeout=15, isolation_level=None)
        try:
            connection.execute("VACUUM INTO '%s'" % destination.replace("'", "''"))
        finally:
            connection.close()
        if os.path.exists(destination) and os.path.getsize(destination) > 0:
            return
        vacuum_error = "VACUUM INTO не создал файл"
    except sqlite3.Error as err:
        vacuum_error = err

    _remove(destination)
    try:
        connection = sqlite3.connect(source, timeout=15, isolation_level=None)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.Error as err:
        raise SnapshotError("не удалось подготовить копию базы (VACUUM INTO: %s;"
                            " checkpoint: %s)" % (vacuum_error, err))
    try:
        shutil.copyfile(source, destination)
    except OSError as err:
        raise SnapshotError("не удалось скопировать базу: %s (VACUUM INTO: %s)"
                            % (err, vacuum_error))


def backup_bytes(db_path):
    """Снимок базы: консистентная копия + gzip. Бросает SnapshotError при проблеме."""
    db_path = os.path.abspath(str(db_path or ""))
    if not db_path:
        raise SnapshotError("не задан путь к базе")
    if not os.path.exists(db_path):
        raise SnapshotError("база не найдена: %s" % db_path)

    directory = os.path.dirname(db_path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_db = os.path.join(
        directory, "%s.snapshot-%s.tmp" % (os.path.basename(db_path), uuid.uuid4().hex))
    try:
        _consistent_copy(db_path, temp_db)
        try:
            with open(temp_db, "rb") as handle:
                raw = handle.read()
        except OSError as err:
            raise SnapshotError("не удалось прочитать копию базы: %s" % err)
        if not raw.startswith(SQLITE_MAGIC):
            raise SnapshotError("копия базы не похожа на SQLite")
        return gzip.compress(raw, GZIP_LEVEL)
    finally:
        _remove(temp_db)


# ---------------------------------------------------------------- восстановление

def _unpack(blob, destination):
    """Распаковывает gzip в файл, следя за размером. False — данные негодные."""
    if isinstance(blob, (bytearray, memoryview)):
        blob = bytes(blob)
    if not isinstance(blob, bytes) or not blob:
        return False
    if len(blob) > MAX_SNAPSHOT_BYTES:
        return False
    total = 0
    try:
        with gzip.open(io.BytesIO(blob), "rb") as source, open(destination, "wb") as target:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SNAPSHOT_BYTES:
                    # Защита от «бомбы»: сжатый файл маленький, а распаковка огромная.
                    return False
                target.write(chunk)
    except Exception:
        return False
    return total > 0


def _looks_like_bot_db(path):
    """Проверяет, что файл — целая база SQLite с таблицей пользователей."""
    try:
        connection = sqlite3.connect(path, timeout=15)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).strip().lower() != "ok":
            return False
        found = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        return bool(found)
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def database_provider(db_path):
    """Вуз, которому принадлежит база (meta.provider) — '' если не определить.

    Нужно при восстановлении: в одном сервисе живут два бота, и база одного вуза
    не должна подниматься у другого.
    """
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(db_path)),
            uri=True, timeout=15)
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='provider'").fetchone()
        finally:
            connection.close()
        return str(row[0]).strip() if row and row[0] else ""
    except sqlite3.Error:
        return ""


def restore_bytes(blob, db_path, expected_provider=""):
    """Распаковать, проверить целостность SQLite, атомарно заменить базу.

    True — восстановлено, False — снимок негодный (база не тронута).
    ``expected_provider`` — имя вуза бота: если в копии другой вуз, восстановление
    отменяется, иначе бот КГАСУ работал бы с расписанием ПГУФКСиТ.
    """
    db_path = os.path.abspath(str(db_path or ""))
    if not db_path:
        return False
    try:
        directory = os.path.dirname(db_path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_db = os.path.join(directory, ".restore-%s.tmp" % uuid.uuid4().hex)
        try:
            if not _unpack(blob, temp_db):
                return False
            if not _looks_like_bot_db(temp_db):
                return False
            if expected_provider:
                found = database_provider(temp_db)
                if found and found != expected_provider:
                    return False
            # Рабочую базу трогаем только после всех проверок — замена атомарная.
            try:
                os.replace(temp_db, db_path)
            except PermissionError:
                # На Windows файл может остаться занятым «мусорным» соединением
                # SQLite: такие объекты живут до сборщика мусора. Собираем мусор
                # и пробуем ещё раз — если база занята по-настоящему, вернём False.
                gc.collect()
                os.replace(temp_db, db_path)
        finally:
            _remove(temp_db)
        # Журнал старой базы больше не нужен и только помешал бы новой.
        _remove(db_path + "-wal")
        _remove(db_path + "-shm")
        return True
    except Exception:
        return False


def remember_source(db_path, pinned, logger=print):
    """Запомнить, из какого сообщения восстановлена база.

    Без этого после перезапуска бот считал закреплённым предыдущее (уже удалённое)
    сообщение, не мог обновить его и отправлял новое — то есть каждый перезапуск
    добавлял в чат лишнюю копию и служебное сообщение о закреплении.
    """
    log = logger or (lambda message: None)
    message_id = _message_id_of(pinned)
    if not message_id:
        return False
    stamp = pinned.get("date")
    if stamp:
        saved_at = datetime.fromtimestamp(int(stamp), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
    else:
        saved_at = _utc_text()
    try:
        connection = sqlite3.connect(db_path, timeout=15)
        try:
            for key, value in ((META_MESSAGE_ID, str(message_id)),
                               (META_SAVED_AT, saved_at)):
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as err:
        log("не удалось запомнить сообщение с копией: %s" % err)
        return False
    log("копия взята из сообщения %s — следующее сохранение обновит именно его"
        % message_id)
    return True


def restore_from_chat(tg, owner_id, db_path, logger=print, expected_provider=""):
    """Найти у владельца ЗАКРЕПЛЁННОЕ сообщение с копией и восстановить базу.

    Ничего не бросает наружу: при любой неудаче пишет понятную строку в лог
    и возвращает False, оставляя текущую базу нетронутой.
    ``expected_provider`` — вуз бота: копия другого вуза не восстанавливается.
    """
    log = logger or (lambda message: None)
    db_path = os.path.abspath(str(db_path or ""))
    temp_path = ""
    try:
        chat = tg.call("getChat", {"chat_id": owner_id}) or {}
        pinned = chat.get("pinned_message") or {}
        document = pinned.get("document") or {}
        file_id = document.get("file_id")
        if not file_id:
            log("в чате владельца нет закреплённого сообщения с файлом копии базы")
            return False

        directory = os.path.dirname(db_path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = os.path.join(directory, "restore-%s.db.gz" % uuid.uuid4().hex)
        tg.download_file(file_id, temp_path)
        with open(temp_path, "rb") as handle:
            blob = handle.read()

        if restore_bytes(blob, db_path, expected_provider=expected_provider):
            remember_source(db_path, pinned, log)
            log("база восстановлена из закреплённой копии в чате владельца (%d КБ)"
                % (len(blob) // 1024))
            return True
        log("закреплённая копия не подошла (%d КБ): база оставлена без изменений"
            % (len(blob) // 1024))
        return False
    except Exception as err:
        log("не удалось восстановить базу из чата владельца: %s" % err)
        return False
    finally:
        _remove(temp_path)


# -------------------------------------------------------------------- менеджер

class SnapshotManager:
    """Периодически сохраняет базу в закреплённое сообщение чата владельца.

    Использование в bot.py:

        manager = SnapshotManager(tg, store, owner_id, interval_minutes=30, logger=log)
        manager.restore()      # после перезапуска — поднять базу из чата
        manager.start()        # дальше копии делаются сами
        ...
        manager.save(force=True)   # кнопка «сохранить сейчас» в /admin
        manager.stop()             # на выходе

    Публичное состояние для /admin: last_saved_at, last_error, saves_count, message_id.
    """

    def __init__(self, tg, store, owner_id, interval_minutes=30, logger=print):
        self.tg = tg
        self.store = store
        self.owner_id = owner_id
        self.interval_minutes = float(interval_minutes or 0)
        self.log = logger or (lambda message: None)

        # публичное состояние для /admin
        self.last_saved_at = 0.0
        self.last_error = ""
        self.saves_count = 0
        self.message_id = None

        self._lock = threading.RLock()
        self._thread = None
        self._stopping = False
        self._signature = None
        self._save_requested = 0
        self._load_meta()

    # ------------------------------------------------------------ состояние

    @property
    def db_path(self):
        """Путь к базе, за которой следит менеджер (берётся из store.path)."""
        return os.path.abspath(str(getattr(self.store, "path", "") or ""))

    @property
    def running(self):
        """True, если фоновый поток автосохранения сейчас работает."""
        return bool(self._thread and self._thread.is_alive())

    def last_saved_text(self):
        """Время последней копии по UTC — удобная строка для /admin."""
        if not self.last_saved_at:
            return "копий ещё не делали"
        return _utc_text(self.last_saved_at) + " UTC"

    def _meta(self, key, default=None):
        getter = getattr(self.store, "get_meta", None)
        if not callable(getter):
            return default
        try:
            return getter(key, default)
        except Exception:
            return default

    def _set_meta(self, key, value):
        setter = getattr(self.store, "set_meta", None)
        if not callable(setter):
            return
        try:
            setter(key, value)
        except Exception as err:
            self.log("не удалось записать %s в базу: %s" % (key, err))

    def _load_meta(self):
        """Поднимает состояние прошлых запусков: id сообщения, счётчик, время."""
        stored = self._meta(META_MESSAGE_ID)
        try:
            self.message_id = int(stored) if stored else None
        except (TypeError, ValueError):
            self.message_id = None
        count = self._meta(META_SAVES_COUNT)
        try:
            self.saves_count = int(count) if count else 0
        except (TypeError, ValueError):
            self.saves_count = 0
        self.last_saved_at = _parse_utc(self._meta(META_SAVED_AT))

    def _stored_message_id(self):
        """id прошлого закреплённого сообщения — из meta, с запасным значением."""
        stored = self._meta(META_MESSAGE_ID)
        try:
            return int(stored) if stored else None
        except (TypeError, ValueError):
            return self.message_id

    def _signature_of_db(self):
        """Отпечаток базы: время правки и размер самого файла и журнала WAL."""
        parts = []
        for path in (self.db_path, self.db_path + "-wal"):
            try:
                info = os.stat(path)
                parts.append((info.st_mtime_ns, info.st_size))
            except OSError:
                parts.append((0, 0))
        return tuple(parts)

    def _db_unchanged(self):
        """True, если база выглядит так же, как в момент прошлого снимка."""
        if self._signature is None:
            return False
        return self._signature_of_db() == self._signature

    def _fail(self, message):
        self.last_error = str(message)
        self.log("копия базы: %s" % message)

    # ----------------------------------------------------------- сохранение

    def save(self, force=False):
        """Сделать и закрепить копию базы (удалив предыдущую).

        force=True — сохранить даже если база не менялась с прошлого снимка;
        save() и так делает копию всегда, параметр нужен для явных вызовов
        из /admin и после восстановления.
        """
        with self._lock:
            return self._save(force=force)

    def _save(self, force=False):
        db_path = self.db_path
        if not db_path or not self.owner_id:
            self._fail("не задан путь к базе или id владельца")
            return False

        if not force and self._db_unchanged():
            self.log("база не менялась с прошлого снимка — всё равно делаю копию")

        # Защита от «отравления» копии: если в базе не осталось пользователей,
        # а раньше они были, копию не перезаписываем. Иначе один неудачный
        # перезапуск стирает данные навсегда.
        users_now = self._users_count()
        users_before = self._last_saved_users()
        if users_now == 0 and users_before:
            self._fail("в базе 0 пользователей, а в прошлой копии было %d — "
                       "копию не перезаписываю" % users_before)
            return False

        try:
            blob = backup_bytes(db_path)
        except Exception as err:
            self._fail("снимок не сделан: %s" % err)
            return False

        directory = os.path.dirname(db_path) or "."
        # Имя временного файла делаем уникальным: в одном сервисе могут работать
        # два бота, и раньше они писали копию в один и тот же файл — из-за гонки
        # бот КГАСУ отправлял в свой чат базу ПГУФКСиТ (и наоборот).
        temp_path = os.path.join(
            directory, "%s-%s" % (uuid.uuid4().hex[:8], BACKUP_FILENAME))
        caption = "%s %s UTC" % (CAPTION_PREFIX, _utc_text())
        try:
            os.makedirs(directory, exist_ok=True)
            with open(temp_path, "wb") as handle:
                handle.write(blob)
        except OSError as err:
            self._fail("не удалось подготовить файл копии: %s" % err)
            return False

        previous_id = self._stored_message_id()
        try:
            message = None
            edited = False
            if previous_id and hasattr(self.tg, "edit_document"):
                # Копия уже есть в чате: обновляем её, а не шлём новую —
                # иначе чат засоряется сообщениями каждые полчаса.
                try:
                    message = self.tg.edit_document(
                        self.owner_id, previous_id, temp_path,
                        caption=caption, filename=BACKUP_FILENAME)
                    edited = True
                except Exception as err:
                    self.log("не удалось обновить прежнюю копию (%s) — отправляю новую" % err)
                    message = None
            if message is None:
                try:
                    message = self.tg.send_document(
                        self.owner_id, temp_path, caption=caption, filename=BACKUP_FILENAME,
                        disable_notification=True)   # копии не должны звонить в чат
                except Exception as err:
                    self._fail("не удалось отправить копию в чат: %s" % err)
                    return False
        finally:
            _remove(temp_path)

        message_id = _message_id_of(message) or (previous_id if edited else None)
        if not message_id:
            self._fail("Telegram не вернул id сообщения с копией")
            return False

        # Закрепление и удаление прошлой копии — вспомогательные шаги: их сбой
        # не должен отменять уже сделанное сохранение, поэтому только логируем.
        if not edited:
            self._pin(message_id)
            if previous_id and previous_id != message_id:
                self._delete(previous_id)

        self.message_id = message_id
        self.last_error = ""
        self.last_saved_at = time.time()
        self.saves_count += 1
        self._save_requested = 0
        self._set_meta(META_MESSAGE_ID, message_id)
        self._set_meta(META_SAVED_AT, datetime.fromtimestamp(
            self.last_saved_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self._set_meta(META_SAVES_COUNT, self.saves_count)
        self._set_meta(META_SAVED_USERS, users_now)
        self._signature = self._signature_of_db()
        if edited:
            self.log("копия базы обновлена в прежнем сообщении: %d КБ, сообщение %s"
                     % (len(blob) // 1024, message_id))
        else:
            self.log("копия базы закреплена в чате владельца: %d КБ, сообщение %s"
                     % (len(blob) // 1024, message_id))
        return True

    def _users_count(self):
        """Сколько пользователей в текущей базе (0 при любой проблеме)."""
        try:
            return int(self.store.count_users())
        except Exception:
            return 0

    def _last_saved_users(self):
        """Сколько пользователей было в прошлой сохранённой копии."""
        try:
            raw = self.store.get_meta(META_SAVED_USERS)
        except Exception:
            return 0
        return int(raw) if str(raw or "").strip().isdigit() else 0

    def request_save(self, delay_seconds=SAVE_REQUEST_DELAY_SECONDS):
        """Попросить сохранить базу после важных изменений (ДЗ, смена группы).

        Раньше копия делалась строго раз в полчаса, и на бесплатном хостинге
        правки могли пропасть при засыпании сервиса. Теперь копия обновляется
        в прежнем (уже закреплённом) сообщении — чат от этого не засоряется.
        """
        moment = time.time() + max(0, delay_seconds)
        if not self._save_requested or moment < self._save_requested:
            self._save_requested = moment
        return self._save_requested

    def _pin(self, message_id):
        try:
            self.tg.call("pinChatMessage", {
                "chat_id": self.owner_id,
                "message_id": message_id,
                "disable_notification": True,
            })
        except Exception as err:
            self.log("не удалось закрепить копию, сама копия отправлена: %s" % err)

    def _delete(self, message_id):
        """Убирает прошлую копию, чтобы чат не зарастал старыми файлами."""
        try:
            delete = getattr(self.tg, "delete_message", None)
            if callable(delete):
                delete(self.owner_id, message_id)
            else:
                self.tg.call("deleteMessage",
                             {"chat_id": self.owner_id, "message_id": message_id})
        except Exception as err:
            self.log("не удалось удалить прошлую копию из чата: %s" % err)

    def maybe_save(self):
        """Сохранить, если прошёл интервал и база менялась с прошлого снимка."""
        now = time.time()
        if self.interval_minutes > 0 and self.last_saved_at:
            if now - self.last_saved_at < self.interval_minutes * 60:
                return False
        if self._db_unchanged():
            # Журнал отправок в базе всё равно меняется, поэтому долго молчать
            # не даём: раз в интервал копия обновится даже без правок данных.
            self.log("база не менялась с прошлого снимка — копия не нужна")
            return False
        return self.save(force=True)

    # -------------------------------------------------------- восстановление

    def restore(self):
        """Поднять базу из закреплённой копии в чате владельца (для старта бота)."""
        db_path = self.db_path
        if not db_path or not self.owner_id:
            self._fail("не задан путь к базе или id владельца")
            return False
        ok = restore_from_chat(self.tg, self.owner_id, db_path, logger=self.log)
        if ok:
            self.last_error = ""
            self._signature = self._signature_of_db()
        return ok

    # ------------------------------------------------------------ фоновый поток

    def start(self):
        """Запускает поток-демон: раз в минуту проверяет, не пора ли сохранить."""
        with self._lock:
            if self.running:
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="snapshot", daemon=True)
            self._thread.start()
        self.log("автосохранение базы включено: раз в %g мин." % self.interval_minutes)

    def stop(self):
        """Останавливает поток. Безопасно вызывать даже без start()."""
        self._stopping = True
        with self._lock:
            thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                self.log("поток автосохранения не успел остановиться")

    def _sleep(self, seconds):
        """Спит короткими шагами, чтобы stop() срабатывал быстро. True — пора выходить."""
        for _ in range(int(seconds)):
            if self._stopping:
                return True
            time.sleep(1)
        return self._stopping

    def _loop(self):
        # Первую минуту молчим: даём боту время поднять базу из копии, иначе
        # пустая база после перезапуска затрёт хороший снимок в чате.
        if self._sleep(CHECK_INTERVAL_SECONDS):
            return
        while not self._stopping:
            try:
                self._save_if_requested()
                self.maybe_save()
            except Exception as err:
                self._fail("ошибка автосохранения: %s" % err)
            if self._sleep(CHECK_INTERVAL_SECONDS):
                return

    def _save_if_requested(self):
        """Сохранение по запросу (после добавления ДЗ, смены группы и т.п.)."""
        moment = self._save_requested
        if not moment:
            return False
        now = time.time()
        if now < moment:
            return False
        if self.last_saved_at and now - self.last_saved_at < MIN_SAVE_GAP_SECONDS:
            return False           # слишком часто не дёргаем: подождём следующей проверки
        return self.save(force=True)
