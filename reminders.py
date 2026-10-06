# -*- coding: utf-8 -*-
"""Движок напоминаний и онлайн-проверки расписания.

Работает в фоновом потоке: раз в tick секунд делает один «тик» —
обновляет устаревший кэш расписания, сравнивает версии и рассылает
уведомления. Вся логика вынесена в tick_once(), поэтому её можно
прогонять в тестах без потоков и без сети.

Часовой пояс: локальное время = UTC + tz_offset часов (Казань — UTC+3,
перехода на летнее время в России нет, поэтому смещения достаточно).
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone

import texts
from schedule import providers, unifirst

#: Сколько минут после назначенного времени ещё допустимо отправить напоминание.
#: Позже — уже неактуально: сервис мог проснуться ночью, и «вчерашний» дайджест
#: разбудил бы человека зря.
EVENING_GRACE_MINUTES = 90
HOMEWORK_GRACE_MINUTES = 210


class ReminderEngine:
    def __init__(self, store, api, telegram, logger=print, refresh_minutes=20,
                 tick_seconds=30, sleep=time.sleep, provider=None):
        self.store = store
        self.api = api
        # Источник расписания: у ПГУФКСиТ это API сайта, у КГАСУ — файлы Word.
        self.provider = provider or providers.UnifirstProvider(api)
        self.tg = telegram
        self.log = logger
        self.refresh_minutes = refresh_minutes
        self.tick_seconds = tick_seconds
        self._sleep = sleep
        self._thread = None
        self._stop = threading.Event()
        self._sent_memory = set()
        self.last_tick = None
        self.last_error = None

    # ------------------------------------------------------------ фон

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reminders", daemon=True)
        self._thread.start()
        self.log("Движок напоминаний запущен (тик %d с, обновление кэша %d мин)"
                 % (self.tick_seconds, self.refresh_minutes))

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as error:  # движок не должен падать никогда
                self.last_error = str(error)
                self.log("Ошибка в движке напоминаний: %s" % error)
            self._sleep(self.tick_seconds)

    # ------------------------------------------------------------ утилиты

    @staticmethod
    def local_now(now_utc=None, tz_offset=3):
        moment = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        return moment + timedelta(hours=int(tz_offset or 0))

    def lessons_for_date(self, group_title, target_date):
        """Пары группы на дату (из кэша; при отсутствии — None)."""
        year, week = unifirst.iso_year_week(target_date)
        row = self.store.get_timetable(group_title, year, week)
        if not row:
            return None, (year, week)
        lessons = [item for item in row["lessons"] if item.get("date") == target_date.isoformat()]
        return lessons, (year, week)

    def ensure_week(self, group_title, year, week, force=False):
        """Обновляет неделю в кэше, если она устарела. Возвращает (lessons, changed, old)."""
        age = self.store.timetable_age_minutes(group_title, year, week)
        if not force and age is not None and age < self.refresh_minutes:
            row = self.store.get_timetable(group_title, year, week)
            return row["lessons"], False, row["lessons"]
        lessons, payload = self.provider.week_lessons(group_title, year, week, force=force)
        new_digest = unifirst.digest(lessons)
        old = self.store.get_timetable(group_title, year, week)
        old_lessons = old["lessons"] if old else None
        changed = bool(old and old.get("digest") and old["digest"] != new_digest)
        self.store.save_timetable(group_title, year, week, payload, lessons, new_digest)
        if changed:
            for item in unifirst.diff_lessons(old_lessons, lessons):
                summary = "%s: %s" % (item["kind"], (item["lesson"] or {}).get("subject", ""))
                self.store.add_change(group_title, year, week, item.get("date"),
                                      item.get("kind"), summary)
        return lessons, changed, old_lessons

    # -------------------------------------------------------------- тик

    def tick_once(self, now_utc=None):
        moment = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
        self.last_tick = moment
        users = [user for user in self.store.all_users() if user.get("group_title")]
        if users:
            self.refresh_groups(sorted({user["group_title"] for user in users}))
            self.send_change_alerts()
        for user in users:
            try:
                self.tick_user(user, moment)
            except Exception as error:
                self.log("Напоминание для %s не отправлено: %s" % (user.get("tg_id"), error))
        self.daily_cleanup(moment)
        return moment

    def refresh_groups(self, group_titles):
        today = date.today()
        for group_title in group_titles:
            for delta in (0, 1):
                year, week = unifirst.iso_year_week(today + timedelta(weeks=delta))
                try:
                    _, changed, _ = self.ensure_week(group_title, year, week)
                    if changed:
                        self.log("Расписание %s изменилось (%d, неделя %d)"
                                 % (group_title, year, week))
                except (unifirst.UnifirstError, providers.ProviderError) as error:
                    self.log("Не удалось обновить %s (%d/%d): %s"
                             % (group_title, year, week, error))

    def send_change_alerts(self):
        for group_title in self.store.groups_in_use():
            pending = self.store.pending_changes(group_title)
            if not pending:
                continue
            subscribers = [user for user in self.store.users_by_group(group_title)
                           if user.get("change_alerts")]
            if not subscribers:
                self.store.mark_changes_announced([item["id"] for item in pending])
                continue
            key = "changes:" + ",".join(str(item["id"]) for item in pending)
            lines = ["⚠️ <b>Расписание изменилось</b>",
                     "Группа <b>%s</b>" % texts.esc(group_title), ""]
            for group_key, items in group_changes(pending).items():
                lines.append("<i>%s</i>" % texts.esc(group_key))
                for item in items:
                    lines.extend(texts.changes_lines([item]))
                lines.append("")
            message = "\n".join(lines).strip()
            delivered = 0
            failed = 0
            for user in subscribers:
                if self.store.was_sent(user["tg_id"], "change", key):
                    continue
                if self.send(user["tg_id"], message):
                    self.store.mark_sent(user["tg_id"], "change", key)
                    delivered += 1
                else:
                    failed += 1
            # Если кому-то не доставили (нет сети, бот заблокирован) — не помечаем
            # изменения объявленными: на следующем тике попробуем ещё раз.
            # Уже получившие сообщение повторно его не увидят — им мешает журнал отправок.
            if failed == 0:
                self.store.mark_changes_announced([item["id"] for item in pending])
            if delivered:
                self.log("Об изменениях %s уведомлено %d чел.%s"
                         % (group_title, delivered,
                            (", не доставлено: %d" % failed) if failed else ""))

    def tick_user(self, user, now_utc):
        tz = int(user.get("tz_offset") or 3)
        local = self.local_now(now_utc, tz)
        today = local.date()
        tomorrow = today + timedelta(days=1)
        group_title = user["group_title"]

        # 1. Вечерний дайджест: расписание на завтра + что сдать.
        # Отправляем только в окне EVENING_GRACE_MINUTES после назначенного времени:
        # если сервис проснулся ночью или утром, старый дайджест уже не нужен.
        if user.get("evening_enabled"):
            evening_at = user.get("evening_time") or "20:00"
            late_minutes = self.minutes_late(local, evening_at)
            if late_minutes is not None:
                key = "evening:%s" % today.isoformat()
                if late_minutes <= EVENING_GRACE_MINUTES:
                    if not self.already_sent(user["tg_id"], "evening", key):
                        lessons = self.safe_lessons(group_title, tomorrow)
                        homework = self.store.list_homework(
                            user["tg_id"], group_title, scope="group",
                            due_from=tomorrow.isoformat(), due_to=tomorrow.isoformat())
                        message = texts.evening_digest(user, tomorrow, lessons or [],
                                                       homework, group_title)
                        if self.send(user["tg_id"], message):
                            self.remember_sent(user["tg_id"], "evening", key)
                elif not self.already_sent(user["tg_id"], "evening", key):
                    # Время ушло: молча помечаем, чтобы не прислать «вчерашний» дайджест.
                    self.remember_sent(user["tg_id"], "evening", key)
                    self.log("Дайджест для %s пропущен: опоздание %d мин"
                             % (user["tg_id"], int(late_minutes)))

        # 2. Предупреждение перед парой — строго до начала пары.
        before_minutes = int(user.get("before_minutes") or 0)
        if before_minutes > 0:
            lessons = self.safe_lessons(group_title, today) or []
            # Идём по слотам: если пара делится на подгруппы, напоминание одно,
            # со списком вариантов, а не два одинаковых подряд.
            for block in unifirst.group_by_slot(lessons):
                start = parse_time(block.get("start"))
                if not start:
                    continue
                remind_at = start - timedelta(minutes=before_minutes)
                if remind_at <= local < start:
                    key = "before:%s:%s:%s" % (today.isoformat(), block.get("para"),
                                               block.get("start") or "")
                    if not self.already_sent(user["tg_id"], "before", key):
                        message = texts.before_lesson_reminder(before_minutes, block)
                        if self.send(user["tg_id"], message):
                            self.remember_sent(user["tg_id"], "before", key)

        # 3. Дедлайн ДЗ сегодня — только утром, а не в любое время суток.
        if user.get("hw_alerts"):
            late_minutes = self.minutes_late(local, "08:30")
            if late_minutes is not None:
                key = "hwday:%s" % today.isoformat()
                if late_minutes <= HOMEWORK_GRACE_MINUTES:
                    if not self.already_sent(user["tg_id"], "hwday", key):
                        items = self.store.list_homework(
                            user["tg_id"], group_title, scope="group",
                            due_from=today.isoformat(), due_to=today.isoformat())
                        if items:
                            if self.send(user["tg_id"],
                                         texts.homework_deadline_reminder(items)):
                                self.remember_sent(user["tg_id"], "hwday", key)
                elif not self.already_sent(user["tg_id"], "hwday", key):
                    self.remember_sent(user["tg_id"], "hwday", key)

    def daily_cleanup(self, now_utc):
        key = "cleanup:%s" % now_utc.date().isoformat()
        if self.store.get_meta("last_cleanup") == key:
            return
        self.store.cleanup_sent(days=30)
        self.store.drop_old_timetable(weeks_keep=8)
        self.store.set_meta("last_cleanup", key)

    # ------------------------------------------------------------ помощники

    def safe_lessons(self, group_title, day):
        """Пары на дату; при сбое сети — из кэша, если он есть."""
        year, week = unifirst.iso_year_week(day)
        try:
            lessons, _, _ = self.ensure_week(group_title, year, week)
        except (unifirst.UnifirstError, providers.ProviderError) as error:
            self.log("Расписание %s (%d/%d) недоступно: %s"
                     % (group_title, year, week, error))
            row = self.store.get_timetable(group_title, year, week)
            lessons = row["lessons"] if row else []
        return [item for item in lessons or [] if item.get("date") == day.isoformat()]

    def send(self, chat_id, message):
        try:
            self.tg.send_message(chat_id, message)
            return True
        except Exception as error:
            self.log("Не доставлено %s: %s" % (chat_id, error))
            return False

    # --------------------------------------------------- защита от повторов

    def already_sent(self, tg_id, kind, key):
        """Отправляли ли уже это напоминание.

        Проверяем и базу, и память процесса. Память важна на бесплатном хостинге:
        если запись в базу не успела попасть в резервную копию (или сорвалась),
        после пробуждения сервиса напоминание могло уйти повторно — теперь нет.
        """
        marker = (tg_id, kind, key)
        if marker in self._sent_memory:
            return True
        try:
            if self.store.was_sent(tg_id, kind, key):
                self._sent_memory.add(marker)
                return True
        except Exception as error:
            self.log("Не удалось проверить журнал отправок: %s" % error)
        return False

    def remember_sent(self, tg_id, kind, key):
        """Помечаем напоминание отправленным (в памяти всегда, в базе — по возможности)."""
        marker = (tg_id, kind, key)
        if len(self._sent_memory) > 5000:      # страховка от роста памяти
            self._sent_memory.clear()
        self._sent_memory.add(marker)
        try:
            self.store.mark_sent(tg_id, kind, key)
        except Exception as error:
            self.log("Отметка об отправке (%s) не сохранилась: %s" % (kind, error))

    def reset_memory(self):
        """Забыть отметки в памяти процесса (нужно тестам и отладке)."""
        self._sent_memory.clear()

    @staticmethod
    def minutes_late(local_dt, hhmm):
        """Сколько минут прошло после указанного локального времени (None — ещё не наступило)."""
        try:
            hours, minutes = [int(part) for part in str(hhmm).split(":")[:2]]
        except (ValueError, AttributeError):
            hours, minutes = 20, 0
        scheduled = local_dt.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        delta = (local_dt - scheduled).total_seconds() / 60.0
        return delta if delta >= 0 else None

    @staticmethod
    def time_reached(local_dt, hhmm):
        """Прошло ли указанное локальное время (HH:MM)."""
        try:
            hours, minutes = [int(part) for part in str(hhmm).split(":")[:2]]
        except (ValueError, AttributeError):
            hours, minutes = 20, 0
        return (local_dt.hour, local_dt.minute) >= (hours, minutes)


def parse_time(value):
    """'10:10' -> datetime на сегодняшней дате (только для сравнения часов)."""
    if not value:
        return None
    try:
        hours, minutes = [int(part) for part in str(value).split(":")[:2]]
    except (ValueError, AttributeError):
        return None
    base = datetime.combine(date.today(), datetime.min.time())
    return base.replace(hour=hours, minute=minutes)


def group_changes(changes):
    """Группировка изменений по неделе/дате для аккуратного сообщения."""
    result = {}
    for item in changes:
        label = unifirst.week_label(item["year"], item["week"]) if item.get("year") else "Изменения"
        result.setdefault(label, []).append(item)
    return result
