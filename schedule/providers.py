# -*- coding: utf-8 -*-
"""Источники расписания: один интерфейс для разных вузов.

Бот и движок напоминаний работают не с конкретным сайтом, а с провайдером.
Сейчас их два:

  * `unifirst` — Поволжский ГУФКСиТ: публичный API сайта расписания, недели ISO,
    данные приходят готовыми парами;
  * `kgasu` — КГАСУ: файлы Word (.doc/.docx) на семестр, внутри таблица
    с колонками групп и подгрупп, метками «Чет/Неч» и пометками «с даты».

Провайдер отдаёт пары в одном формате, поэтому экраны бота, напоминания и
отслеживание изменений одинаково работают для обоих вузов.
"""

import hashlib

from schedule import kgasu, unifirst


class ProviderError(Exception):
    """Ошибка получения расписания (общая для всех источников)."""


class UnifirstProvider:
    """Поволжский ГУФКСиТ: публичный API api.unifirst.ru."""

    name = "unifirst"
    title = "Поволжский ГУФКСиТ"
    city = "Казань"
    site = "https://timetable.unifirst.ru/"

    def __init__(self, api=None):
        self.api = api or unifirst.Unifirst()

    def groups(self):
        """Список групп вуза."""
        try:
            return self.api.groups(limit=500)
        except unifirst.UnifirstError as error:
            raise ProviderError(str(error))

    def week_lessons(self, group, year, week, force=False):
        """Пары группы на неделю: (список пар, сырой ответ для кэша)."""
        try:
            payload = self.api.timetable(group, year, week)
        except unifirst.UnifirstError as error:
            raise ProviderError(str(error))
        return unifirst.normalize(payload), payload

    def check(self):
        """Самопроверка источника: сколько групп и пар доступно."""
        groups = self.groups()
        return {"groups": len(groups), "sample": groups[0]["name"] if groups else ""}


class KgasuProvider:
    """КГАСУ: файлы расписания со страницы /student/raspisanie-zanyatiy/."""

    name = "kgasu"
    title = "КГАСУ"
    city = "Казань"
    site = "https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php"

    def __init__(self, store=None, cache_minutes=180, groups_cache=None):
        self.store = store
        self.cache_minutes = cache_minutes
        self._grids = {}          # ссылка на файл -> разобранная таблица (в памяти)
        self._groups = groups_cache

    # ------------------------------------------------------------- группы

    def groups(self):
        raw = kgasu.groups()
        self._groups = raw
        return [{"name": item["name"], "file_url": item["file_url"],
                 "file_ext": item["file_ext"], "hasSubgroups": False, "subgroups": []}
                for item in raw]

    def _file_url(self, group):
        """Ссылка на файл расписания для группы (из базы или со страницы)."""
        if self.store:
            row = self.store.group_by_name(group)
            if row and row.get("source_url"):
                return row["source_url"]
        if self._groups is None:
            self._groups = kgasu.groups()
        found = kgasu.find_group(self._groups, group)
        if not found:
            raise ProviderError("группа %s не найдена на сайте КГАСУ" % group)
        return found["file_url"]

    # --------------------------------------------------------- расписание

    def grid(self, group, force=False):
        """Разобранная таблица расписания группы (с кэшем в памяти и на диске)."""
        file_url = self._file_url(group)
        key = (file_url, bool(force))
        if not force and file_url in self._grids:
            return self._grids[file_url]
        blob, path = kgasu.download(file_url,
                                    max_age_minutes=0 if force else self.cache_minutes)
        grid = kgasu.prepare(blob, file_url.split("/")[-1])
        grid["file_hash"] = hashlib.sha1(blob).hexdigest()[:16]
        grid["file_url"] = file_url
        if not force:
            self._grids[file_url] = grid
        return grid

    def week_lessons(self, group, year, week, force=False):
        """Пары группы на неделю: перебираем семь дней недели."""
        grid = self.grid(group, force=force)
        lessons = []
        for day in unifirst.week_days(year, week):
            lessons.extend(kgasu.lessons_for_day(grid, group, day))
        lessons.sort(key=lambda item: (item["date"], item["para"] or 0, item["start"]))
        payload = {"kind": grid.get("kind"), "file_url": grid.get("file_url"),
                   "file_hash": grid.get("file_hash"),
                   "columns": grid.get("columns") or []}
        return lessons, payload

    def groups_in_file(self, group):
        """Какие ещё группы есть в том же файле (для подсказки)."""
        try:
            grid = self.grid(group)
        except ProviderError:
            return []
        return [column.get("group") for column in grid.get("columns") or []
                if column.get("group") and column["group"] != group]

    def check(self):
        """Самопроверка источника: список групп и разбор первой группы."""
        groups = self.groups()
        sample = next((item for item in groups if item["file_ext"] == "docx"),
                      groups[0] if groups else None)
        result = {"groups": len(groups), "sample": sample["name"] if sample else ""}
        if sample:
            year, week = unifirst.iso_year_week(__import__("datetime").date.today())
            lessons, _ = self.week_lessons(sample["name"], year, week)
            result["lessons"] = len(lessons)
        return result


def make_provider(name, store=None):
    """Создаёт провайдер по имени: 'unifirst' или 'kgasu'."""
    name = (name or "unifirst").strip().lower()
    if name in ("kgasu", "кгасу"):
        return KgasuProvider(store=store)
    if name in ("unifirst", "pgufksit", "пгуфксит"):
        return UnifirstProvider()
    raise ProviderError("неизвестный источник расписания: %s" % name)
