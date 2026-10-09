# -*- coding: utf-8 -*-
"""Самопроверка сохранения и восстановления базы через чат владельца (snapshot.py).

Запуск из корня проекта::

    python tests/test_snapshot.py

Тест полностью офлайн: Telegram подменяется классом FakeTelegram, база живёт
во временной папке (tempfile.mkdtemp) и убирается в конце. В интернет тест
не ходит и от текущей даты не зависит. Итог печатается строкой ``OK: N проверок``,
код возврата 0 — всё прошло, 1 — есть ошибки.
"""

import gzip
import gc
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

try:  # чтобы русские подписи и эмодзи не ломали вывод в старой консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import snapshot                                            # noqa: E402
from snapshot import (                                     # noqa: E402
    BACKUP_FILENAME,
    SnapshotError,
    SnapshotManager,
    backup_bytes,
    restore_bytes,
    restore_from_chat,
)
from store import Store                                    # noqa: E402

OWNER_ID = 900900

CASES = []


def check(title, condition, detail=""):
    """Записывает результат одной проверки."""
    CASES.append((title, bool(condition), detail))
    return bool(condition)


# ------------------------------------------------------------- фальшивый Telegram

class FakeTelegram:
    """Поддельный клиент Telegram: файлы не уходят в сеть, а остаются в памяти."""

    def __init__(self):
        self.documents = []          # «отправленные» документы вместе с байтами
        self.pinned = []             # вызовы pinChatMessage
        self.deleted = []            # вызовы deleteMessage
        self.calls = []              # все обращения к API
        self.pinned_message = None   # то, что вернёт getChat
        self.next_message_id = 100
        self.fail_pin = False        # имитация сбоя закрепления
        self.fail_delete = False     # имитация сбоя удаления

    # --- методы, которыми пользуется snapshot.py ---

    def send_document(self, chat_id, file_path, caption=None, filename=None,
                      disable_notification=False):
        with open(file_path, "rb") as handle:
            payload = handle.read()
        self.next_message_id += 1
        item = {"message_id": self.next_message_id, "chat_id": chat_id,
                "caption": caption, "filename": filename or os.path.basename(file_path),
                "data": payload, "silent": disable_notification}
        self.documents.append(item)
        return {"message_id": item["message_id"],
                "document": {"file_id": "file-%d" % item["message_id"],
                             "file_name": item["filename"]}}

    def call(self, method, params=None, **extra):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "pinChatMessage":
            if self.fail_pin:
                raise RuntimeError("pinChatMessage: имитация сбоя Telegram")
            self.pinned.append(params)
            item = self._document(params.get("message_id"))
            self.pinned_message = {
                "message_id": item["message_id"],
                "document": {"file_id": "file-%d" % item["message_id"],
                             "file_name": item["filename"]},
            }
            return True
        if method == "deleteMessage":
            if self.fail_delete:
                raise RuntimeError("deleteMessage: имитация сбоя Telegram")
            self.deleted.append(params)
            if (self.pinned_message or {}).get("message_id") == params.get("message_id"):
                self.pinned_message = None
            return True
        if method == "getChat":
            return {"id": params.get("chat_id"), "type": "private",
                    "pinned_message": self.pinned_message}
        raise AssertionError("неожиданный метод Telegram API: %s" % method)

    def download_file(self, file_id, destination):
        """«Скачивает» ранее отправленный документ в указанный путь."""
        data = self._document(int(str(file_id).split("-")[-1]))["data"]
        with open(destination, "wb") as handle:
            handle.write(data)
        return destination, len(data)

    # --- вспомогательное ---

    def _document(self, message_id):
        for item in self.documents:
            if item["message_id"] == message_id:
                return item
        raise AssertionError("документ с message_id=%s не отправлялся" % message_id)


class BrokenTelegram:
    """Клиент, у которого любой вызов API падает — проверяем устойчивость."""

    def call(self, method, params=None, **extra):
        raise RuntimeError("имитация обрыва связи с Telegram")


class GhostStore:
    """Магазин с путём к несуществующей базе: снимок сделать нельзя."""

    def __init__(self, path):
        self.path = path

    def get_meta(self, key, default=None):
        return default

    def set_meta(self, key, value):
        return None


# ------------------------------------------------------------------- утилиты

def seed_db(path, user_id=555, group="26281"):
    """Создаёт базу бота и наполняет её: пользователь, группа, ДЗ, изменения, журнал."""
    store = Store(path)
    store.ensure_user(user_id, "student", "Студент")
    store.update_user(user_id, group_title=group, evening_enabled=1, evening_time="20:00",
                      before_minutes=30, tz_offset=3)
    store.add_homework(group, "История", "§§1-2, вопросы 3-5", due_date="2030-01-15")
    store.add_homework(group, "Анатомия", "конспект главы 4", due_date="2030-01-16",
                       scope="personal", owner_id=user_id)
    store.add_change(group, 2030, 3, "2030-01-15", "replace", "Замена пары")
    store.mark_sent(user_id, "evening", "2030-01-14")
    store.set_meta("custom", "значение")
    return store


def sqlite_rows(path, sql, params=()):
    """Читает строки напрямую и только на чтение, чтобы не создать базу заново."""
    if not os.path.exists(path):
        return []
    connection = None
    try:
        try:
            uri = "file:%s?mode=ro" % path.replace("\\", "/")
            connection = sqlite3.connect(uri, uri=True, timeout=10)
        except sqlite3.Error:
            connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()


def remove_files(*paths):
    """Удаляет файлы базы.

    Перед удалением собираем мусор: на Windows соединение SQLite может оставаться
    открытым из-за циклических ссылок, а занятый файл система не отдаёт.
    """
    gc.collect()
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def temp_junk(directory):
    """Временные файлы, которые модуль обязан убирать за собой."""
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(".tmp") or name.startswith(".restore-")
                  or name.startswith("restore-") or name == BACKUP_FILENAME)


def make_foreign_sqlite(path):
    """Создаёт валидную базу SQLite без таблицы users и возвращает её байты."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT)")
        connection.execute("INSERT INTO notes(text) VALUES('чужая база')")
        connection.commit()
    finally:
        connection.close()
    with open(path, "rb") as handle:
        return handle.read()


def user_row(path, user_id=555):
    rows = sqlite_rows(path, "SELECT * FROM users WHERE tg_id=?", (user_id,))
    return rows[0] if rows else {}


def _writable(directory):
    """Проверяет, что в папку действительно можно писать."""
    try:
        probe = os.path.join(directory, "probe.tmp")
        with open(probe, "wb") as handle:
            handle.write(b"ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def make_workdir():
    """Временная папка для теста (tempfile.mkdtemp), удаляется в конце.

    Если писать в системный TEMP нельзя (так бывает в песочницах: mkdtemp создаёт
    папку с правами 0700), берём такую же временную папку рядом с проектом.
    На обычной машине работает первый же вариант.
    """
    root = tempfile.mkdtemp(prefix="snapshot-test-")
    if _writable(root):
        return root
    shutil.rmtree(root, ignore_errors=True)
    for base in (tempfile.gettempdir(), os.path.join(ROOT, "data")):
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            continue
        path = os.path.join(base, "snapshot-test-%s" % uuid.uuid4().hex[:8])
        try:
            os.mkdir(path)
        except OSError:
            continue
        if _writable(path):
            return path
        shutil.rmtree(path, ignore_errors=True)
    raise RuntimeError("не удалось создать временную папку для теста")


# ------------------------------------------------------- 1. снимок и восстановление

def check_backup_and_restore(workdir):
    prim = os.path.join(workdir, "prim")
    db = os.path.join(prim, "bot.db")
    store = seed_db(db)
    before_user = store.get_user(555)
    before_homework = store.list_homework(555, "26281", scope="all")

    blob = backup_bytes(db)
    raw = gzip.decompress(blob)
    check("backup_bytes: снимок — непустой gzip",
          blob[:2] == b"\x1f\x8b" and len(blob) > 100, "%d байт" % len(blob))
    check("backup_bytes: внутри снимка настоящая база SQLite",
          raw.startswith(b"SQLite format 3"), raw[:20])
    check("backup_bytes: снимок сжался", len(blob) < len(raw),
          "%d -> %d байт" % (len(raw), len(blob)))
    check("backup_bytes: временный файл после снимка убран",
          not temp_junk(prim), temp_junk(prim))

    raised = False
    try:
        backup_bytes(os.path.join(prim, "нет-такой-базы.db"))
    except SnapshotError:
        raised = True
    except Exception:
        raised = False
    check("backup_bytes: отсутствие базы — SnapshotError", raised)

    # Круговой прогон: стираем базу вместе с журналом WAL и поднимаем из снимка.
    remove_files(db, db + "-wal", db + "-shm")
    check("круговой прогон: база удалена перед восстановлением", not os.path.exists(db))

    check("restore_bytes: снимок принят", restore_bytes(blob, db) is True)
    restored_user = user_row(db)
    check("круговой прогон: пользователь вернулся",
          restored_user.get("tg_id") == 555 and restored_user.get("group_title") == "26281",
          restored_user)
    check("круговой прогон: настройки напоминаний вернулись",
          restored_user.get("evening_time") == "20:00"
          and restored_user.get("before_minutes") == 30,
          restored_user)
    homework = sqlite_rows(db, "SELECT * FROM homework ORDER BY id")
    check("круговой прогон: домашние задания вернулись",
          len(homework) == len(before_homework) == 2
          and any(row["subject"] == "История" for row in homework), homework)
    sent = sqlite_rows(db, "SELECT * FROM sent")
    meta = sqlite_rows(db, "SELECT value FROM meta WHERE key='custom'")
    check("круговой прогон: журнал отправок и meta вернулись",
          len(sent) == 1 and meta and meta[0]["value"] == "значение", (sent, meta))
    check("круговой прогон: рабочая копия совпадает со снимком",
          restored_user.get("username") == before_user.get("username"))

    # Дальше портим уже живую базу негодными снимками — она должна уцелеть.
    other = os.path.join(prim, "live.db")
    seed_db(other, user_id=777, group="26282")
    with open(other, "rb") as handle:
        live_before = handle.read()

    check("restore_bytes: мусор вместо gzip отклонён",
          restore_bytes("это вовсе не архив".encode("utf-8"), other) is False)
    check("restore_bytes: пустой снимок отклонён", restore_bytes(b"", other) is False)
    check("restore_bytes: gzip без SQLite внутри отклонён",
          restore_bytes(gzip.compress("привет, это не база".encode("utf-8")), other) is False)
    check("restore_bytes: SQLite без таблицы users отклонён",
          restore_bytes(gzip.compress(make_foreign_sqlite(os.path.join(prim, "foreign.db"))),
                        other) is False)
    check("restore_bytes: обрезанный gzip отклонён", restore_bytes(blob[:40], other) is False)

    # Ограничение размера: сначала по сжатому файлу, потом по распаковке.
    limit = snapshot.MAX_SNAPSHOT_BYTES
    try:
        snapshot.MAX_SNAPSHOT_BYTES = 1
        too_big_compressed = restore_bytes(blob, other)
        snapshot.MAX_SNAPSHOT_BYTES = len(blob)
        too_big_unpacked = restore_bytes(blob, other)
    finally:
        snapshot.MAX_SNAPSHOT_BYTES = limit
    check("restore_bytes: слишком большой сжатый файл отклонён", too_big_compressed is False)
    check("restore_bytes: слишком большая распаковка отклонена", too_big_unpacked is False)

    with open(other, "rb") as handle:
        live_after = handle.read()
    check("restore_bytes: текущая база не испорчена после отказов",
          live_before == live_after and user_row(other, 777).get("group_title") == "26282",
          user_row(other, 777))
    check("restore_bytes: после отказов не осталось временных файлов",
          not temp_junk(prim), temp_junk(prim))


# ------------------------------------------------- 2. менеджер, save и maybe_save

def check_manager(workdir, logs):
    mgr = os.path.join(workdir, "mgr")
    db = os.path.join(mgr, "bot.db")
    store = seed_db(db)
    tg = FakeTelegram()
    manager = SnapshotManager(tg, store, OWNER_ID, interval_minutes=30, logger=logs.append)

    check("менеджер: стартовое состояние пустое",
          manager.saves_count == 0 and manager.message_id is None
          and manager.last_saved_at == 0.0 and manager.last_error == "" and not manager.running,
          (manager.saves_count, manager.message_id, manager.last_saved_at))

    check("save(): документ отправлен владельцу",
          manager.save() is True and len(tg.documents) == 1
          and tg.documents[-1]["chat_id"] == OWNER_ID, tg.documents)
    document = tg.documents[-1]
    check("save(): имя файла — schedule-backup.db.gz",
          document["filename"] == BACKUP_FILENAME, document["filename"])
    check("save(): в чат ушёл именно снимок базы",
          gzip.decompress(document["data"]).startswith(b"SQLite format 3"))
    check("save(): подпись с датой и UTC",
          (document["caption"] or "").startswith("💾 Копия базы расписания:")
          and (document["caption"] or "").endswith("UTC"), document["caption"])
    check("save(): сообщение с копией закреплено",
          bool(tg.pinned) and tg.pinned[-1]["message_id"] == document["message_id"]
          and tg.pinned[-1]["disable_notification"] is True, tg.pinned)
    check("save(): состояние для /admin обновилось",
          manager.saves_count == 1 and manager.message_id == document["message_id"]
          and manager.last_saved_at > 0 and manager.last_error == ""
          and manager.last_saved_text().endswith("UTC"), manager.last_saved_text())
    check("save(): id сообщения записан в meta базы",
          str(store.get_meta("snapshot_message_id")) == str(document["message_id"]),
          store.get_meta("snapshot_message_id"))
    check("save(): временный файл копии удалён",
          not temp_junk(mgr), temp_junk(mgr))

    first_id = document["message_id"]
    check("повторный save(): отправлена новая копия",
          manager.save() is True and len(tg.documents) == 2, len(tg.documents))
    check("повторный save(): прошлое закреплённое сообщение удалено",
          any(item.get("message_id") == first_id for item in tg.deleted), tg.deleted)
    check("повторный save(): закреплена новая копия",
          tg.pinned[-1]["message_id"] == tg.documents[-1]["message_id"]
          and manager.message_id == tg.documents[-1]["message_id"])
    check("повторный save(): счётчик копий вырос", manager.saves_count == 2,
          manager.saves_count)

    sent_documents = len(tg.documents)
    check("maybe_save(): второй раз сразу не сохраняет",
          manager.maybe_save() is False and len(tg.documents) == sent_documents,
          len(tg.documents))

    manager.interval_minutes = 0
    check("maybe_save(): неизменившуюся базу пропускает",
          manager.maybe_save() is False and len(tg.documents) == sent_documents,
          len(tg.documents))

    store.update_user(555, group_title="26282")
    check("maybe_save(): после изменения базы сохраняет",
          manager.maybe_save() is True and len(tg.documents) == sent_documents + 1,
          len(tg.documents))
    check("maybe_save(): новая копия тоже закреплена",
          tg.pinned[-1]["message_id"] == tg.documents[-1]["message_id"])

    # Восстановление из закреплённого сообщения: стираем базу целиком.
    remove_files(db, db + "-wal", db + "-shm")
    check("restore_from_chat(): база удалена перед восстановлением", not os.path.exists(db))
    check("restore_from_chat(): база поднята из закреплённого сообщения",
          restore_from_chat(tg, OWNER_ID, db, logger=logs.append) is True)
    restored = user_row(db)
    check("restore_from_chat(): данные восстановленной базы на месте",
          restored.get("group_title") == "26282"
          and len(sqlite_rows(db, "SELECT * FROM homework")) == 2, restored)
    check("restore_from_chat(): менеджер видит восстановленную базу через store",
          manager.db_path == os.path.abspath(db)
          and Store(db).get_user(555) is not None)

    # После восстановления бот обязан запомнить, из какого сообщения взята копия:
    # иначе он считает закреплённым предыдущее (уже удалённое) сообщение, не может
    # его обновить и при каждом перезапуске отправляет новое.
    saved_id = sqlite_rows(db, "SELECT value FROM meta WHERE key='snapshot_message_id'")
    check("restore_from_chat(): запомнил id сообщения с копией",
          bool(saved_id) and str(saved_id[0]["value"]) == str(tg.pinned_message["message_id"]),
          saved_id)

    # Закреплённой копии нет — только понятный лог и False.
    tg.pinned_message = None
    logs.clear()
    check("restore_from_chat(): без закреплённого документа — False и запись в лог",
          restore_from_chat(tg, OWNER_ID, db, logger=logs.append) is False
          and bool(logs) and user_row(db).get("group_title") == "26282", logs)
    tg.pinned_message = {"message_id": 1, "text": "просто текст без файла"}
    check("restore_from_chat(): закреплён текст без документа — False",
          restore_from_chat(tg, OWNER_ID, db, logger=logs.append) is False)

    # Закреплён документ с мусором вместо снимка.
    tg.documents.append({"message_id": 999, "chat_id": OWNER_ID, "caption": "",
                         "filename": "мусор.db.gz", "data": "совсем не gzip".encode("utf-8")})
    tg.pinned_message = {"message_id": 999, "document": {"file_id": "file-999"}}
    check("restore_from_chat(): битый закреплённый файл — False, база цела",
          restore_from_chat(tg, OWNER_ID, db, logger=logs.append) is False
          and user_row(db).get("group_title") == "26282")

    check("restore_from_chat(): сбой Telegram не вылетает исключением",
          restore_from_chat(BrokenTelegram(), OWNER_ID, db, logger=logs.append) is False)
    check("restore_from_chat(): после неудач не осталось временных файлов",
          not temp_junk(mgr), temp_junk(mgr))

    # Сбой закрепления и удаления не должен ломать само сохранение.
    tg.fail_pin = True
    tg.fail_delete = True
    sent_documents = len(tg.documents)
    try:
        saved = manager.save()
    finally:
        tg.fail_pin = False
        tg.fail_delete = False
    check("save(): сбой закрепления и удаления не ломает сохранение",
          saved is True and len(tg.documents) == sent_documents + 1
          and (tg.documents[-1]["data"][:2] == b"\x1f\x8b"), len(tg.documents))

    # Недоступная база: save() обязан вернуть False и объяснить причину.
    sent_documents = len(tg.documents)
    ghost = SnapshotManager(tg, GhostStore(os.path.join(mgr, "нет-папки", "bot.db")),
                            OWNER_ID, logger=logs.append)
    check("save(): недоступная база — False и last_error вместо исключения",
          ghost.save() is False and bool(ghost.last_error)
          and len(tg.documents) == sent_documents, ghost.last_error)
    check("restore(): без базы и владельца возвращает False",
          SnapshotManager(tg, store, None, logger=logs.append).restore() is False)

    return manager


# --------------------------------------------------------- 3. фоновый поток

def check_threads(workdir, store, tg, logs):
    idle = SnapshotManager(tg, store, OWNER_ID, interval_minutes=30, logger=logs.append)
    safe = True
    try:
        idle.stop()
    except Exception:
        safe = False
    check("stop() без start() безопасен", safe and idle.running is False)

    background = SnapshotManager(tg, store, OWNER_ID, interval_minutes=0, logger=logs.append)
    threads_before = threading.active_count()
    background.start()
    background.start()                       # повторный вызов не должен плодить потоки
    time.sleep(0.2)
    started = background.running
    extra_threads = threading.active_count() - threads_before
    background.stop()
    time.sleep(0.1)
    check("start(): фоновый поток автосохранения пошёл", started is True)
    check("start(): повторный вызов не плодит потоки", extra_threads == 1, extra_threads)
    check("stop(): фоновый поток остановлен и ждёт без ошибок",
          background.running is False and background.stop() is None)


# ---------------------------------------------------------------------- запуск

def check_guards(workdir):
    """Защита от пустой копии и от копии чужого вуза.

    История: в одном сервисе работали два бота, писали копию в один и тот же
    файл и восстанавливались из чужих баз. Плюс пустая база могла затереть
    хорошую копию. Эти проверки следят, чтобы такое не повторилось.
    """
    import snapshot

    # 1. Копия чужого вуза не восстанавливается.
    primary = os.path.join(workdir, "guards")
    os.makedirs(primary, exist_ok=True)
    foreign = os.path.join(primary, "kgasu.db")
    seed_db(foreign, user_id=777, group="26ЗК01")
    store = Store(foreign)
    store.set_meta("provider", "kgasu")
    blob = backup_bytes(foreign)
    check("снимок знает свой вуз", snapshot.database_provider(foreign) == "kgasu",
          snapshot.database_provider(foreign))

    target = os.path.join(primary, "bot.db")
    seed_db(target, user_id=555, group="26281")
    check("копия чужого вуза отклонена",
          restore_bytes(blob, target, expected_provider="unifirst") is False)
    check("после отказа своя база цела", user_row(target, 555) is not None)
    check("копия своего вуза принимается",
          restore_bytes(blob, target, expected_provider="kgasu") is True)
    check("после восстановления в базе пользователь КГАСУ",
          user_row(target, 777) is not None)

    # 2. Пустая база не перезаписывает хорошую копию.
    empty_dir = os.path.join(workdir, "guards-empty")
    os.makedirs(empty_dir, exist_ok=True)
    empty_db = os.path.join(empty_dir, "bot.db")
    empty_store = Store(empty_db)
    empty_store.set_meta("provider", "unifirst")
    empty_store.set_meta(snapshot.META_SAVED_USERS, "3")
    tg = FakeTelegram()
    manager = SnapshotManager(tg, empty_store, OWNER_ID, interval_minutes=0,
                              logger=lambda message: None)
    check("пустая база не перезаписывает копию", manager.save(force=True) is False)
    check("копия при этом не отправлялась", not tg.documents)
    check("в логе объяснение про 0 пользователей",
          any("0 пользователей" in str(item) for item in [manager.last_error]),
          manager.last_error)

    # 3. Если пользователи есть — копия делается как обычно.
    seed_db(empty_db, user_id=999, group="26282")
    empty_store.set_meta("provider", "unifirst")
    check("с пользователями копия сохраняется", manager.save(force=True) is True)
    check("файл копии ушёл в чат", len(tg.documents) == 1, tg.documents)
    check("имя файла — как у обычной копии",
          str(tg.documents[0].get("filename")).endswith("schedule-backup.db.gz"),
          tg.documents[0].get("filename"))


def main():
    workdir = make_workdir()
    logs = []
    print("Временная папка: %s" % workdir)
    try:
        print("\n1. Снимок базы и восстановление из байтов")
        check_backup_and_restore(workdir)

        print("\n2. Менеджер: save(), maybe_save(), restore_from_chat()")
        manager = check_manager(workdir, logs)

        print("\n3. Фоновый поток и безопасная остановка")
        check_threads(workdir, manager.store, manager.tg, logs)

        print("\n4. Защита копий: пустая база и чужой вуз")
        check_guards(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failed = 0
    for number, (title, ok, detail) in enumerate(CASES, 1):
        print("%2d. [%s] %s" % (number, "OK" if ok else "FAIL", title))
        if not ok:
            failed += 1
            if detail:
                print("      подробности: %s" % (detail,))
    print("-" * 60)
    if failed:
        print("ПРОВАЛ: %d из %d проверок не пройдено" % (failed, len(CASES)))
        return 1
    print("OK: %d проверок" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
