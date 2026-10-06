# -*- coding: utf-8 -*-
"""Клиент публичного API расписания Поволжского ГУФКСиТ (unifirst.ru).

API найден разбором фронтенда https://timetable.unifirst.ru (Vite-бандл
/assets/index-*.js): база `https://api.unifirst.ru/api/v1`, обязательный
заголовок `api-key`. Эндпоинты:

    GET /groups?limit=500[&name=<подстрока>]
    GET /timetable?title=<группа>[ <подгруппа>]&week=<ISO-неделя>&year=<год>
    GET /teachers?limit=20[&name=...][&after=...|&before=...]
    GET /teachers/<id>/timetable?year=<год>&week=<неделя>

Модуль зависит только от стандартной библиотеки: бот работает без pip.
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_BASE = "https://api.unifirst.ru/api/v1"
API_KEY = "777e5031c257a3d5ce7aa54aa56015cb"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


class UnifirstError(Exception):
    """Ошибка обращения к API расписания."""

    def __init__(self, message, code=None, url=None):
        super().__init__(message)
        self.code = code
        self.url = url


# ---------------------------------------------------------------- календарь

def iso_year_week(day):
    """ISO-год и номер недели для даты (так же считает сайт)."""
    year, week, _ = day.isocalendar()
    return int(year), int(week)


def week_bounds(year, week):
    """Понедельник и воскресенье недели."""
    monday = date.fromisocalendar(int(year), int(week), 1)
    return monday, monday + timedelta(days=6)


def week_days(year, week):
    """Семь дат недели, начиная с понедельника."""
    monday, _ = week_bounds(year, week)
    return [monday + timedelta(days=offset) for offset in range(7)]


def shift_week(year, week, delta):
    """Сдвиг на delta недель (может быть отрицательным)."""
    monday, _ = week_bounds(year, week)
    return iso_year_week(monday + timedelta(weeks=delta))


def week_label(year, week):
    """«Неделя с 05.10 по 11.10 2026 года» — как на сайте."""
    monday, sunday = week_bounds(year, week)
    return "Неделя с %s по %s %d года" % (
        monday.strftime("%d.%m"), sunday.strftime("%d.%m"), monday.year)


def day_label(day):
    """«Понедельник, 5 октября»."""
    return "%s, %d %s" % (WEEKDAYS[day.weekday()], day.day, MONTHS[day.month - 1])


def short_day_label(day):
    """«Пн 05.10»."""
    return "%s %s" % (WEEKDAYS[day.weekday()][:2], day.strftime("%d.%m"))


# ------------------------------------------------------------------- клиент

class Unifirst:
    """Тонкий клиент API расписания."""

    def __init__(self, base=API_BASE, api_key=API_KEY, timeout=25, retries=2,
                 user_agent=USER_AGENT, sleep=time.sleep):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self._sleep = sleep

    # -- низкий уровень ----------------------------------------------------

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        last_error = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError:
                    raise UnifirstError(
                        "API вернул не JSON (возможно, изменился ключ доступа)", url=url)
            except urllib.error.HTTPError as error:
                detail = ""
                try:
                    detail = error.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last_error = UnifirstError(
                    "HTTP %s от API%s" % (error.code, (": " + detail) if detail else ""),
                    code=error.code, url=url)
                if error.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    self._sleep(1.5 * (attempt + 1))
                    continue
                raise last_error
            except UnifirstError:
                raise
            except Exception as error:  # сеть, DNS, таймаут
                last_error = UnifirstError("Нет связи с API: %s" % error, url=url)
                if attempt < self.retries:
                    self._sleep(1.5 * (attempt + 1))
                    continue
                raise last_error
        raise last_error or UnifirstError("Не удалось получить данные", url=url)

    # -- высокий уровень ---------------------------------------------------

    def groups(self, limit=500, name=None):
        """Список учебных групп: [{'id', 'name', 'hasSubgroups', 'subgroups'}]."""
        params = {"limit": int(limit)}
        if name:
            params["name"] = name
        return self._get("/groups", params).get("data") or []

    def timetable(self, title, year, week):
        """Сырой ответ API по группе (title = «26281» или «26281 подгруппа»)."""
        return self._get("/timetable", {"title": title, "week": int(week), "year": int(year)})

    def teachers(self, limit=20, name=None, after=None, before=None):
        params = {"limit": int(limit)}
        if name:
            params["name"] = name
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return self._get("/teachers", params)

    def teacher_timetable(self, teacher_id, year, week):
        return self._get("/teachers/%s/timetable" % teacher_id,
                         {"year": int(year), "week": int(week)})


# ------------------------------------------------- нормализация расписания

def group_title(group, subgroup=None):
    """Название для запроса расписания: группа + подгруппа, если она есть."""
    name = (group or {}).get("name") or group or ""
    name = str(name).strip()
    if subgroup:
        return "%s %s" % (name, str(subgroup).strip())
    return name


def find_group(groups, query):
    """Найти группу по строке: точное совпадение, затем начало, затем вхождение."""
    query = (query or "").strip().lower()
    if not query:
        return None
    names = [(str(item.get("name", "")), item) for item in groups or []]
    for name, item in names:
        if name.lower() == query:
            return item
    for name, item in names:
        if name.lower().startswith(query):
            return item
    for name, item in names:
        if query in name.lower():
            return item
    return None


def normalize(payload):
    """Сырой ответ /timetable -> плоский список пар.

    Пара: {'date': 'YYYY-MM-DD', 'day_label': ..., 'para': int, 'start': 'HH:MM',
           'end': 'HH:MM', 'subject': str, 'type': str, 'teachers': [str],
           'rooms': [str], 'uid': str}
    """
    lessons = []
    for entry in payload.get("data") or []:
        for iso_date, day in (entry.get("lessons") or {}).items():
            label = (day or {}).get("label") or iso_date
            for item in (day or {}).get("items") or []:
                info = item.get("date") or {}
                lesson = {
                    "date": normalize_date(iso_date, info.get("date")),
                    "day_label": label,
                    "para": item.get("para"),
                    "start": (info.get("time_start") or "").strip(),
                    "end": (info.get("time_end") or "").strip(),
                    "subject": (item.get("title") or "").strip(),
                    "type": ((item.get("type") or {}).get("name") or "").strip(),
                    "teachers": [str(t.get("name", "")).strip()
                                 for t in (item.get("teachers") or []) if t.get("name")],
                    "rooms": [str(a.get("name", "")).strip()
                              for a in (item.get("auditories") or []) if a.get("name")],
                }
                lesson["uid"] = lesson_uid(lesson)
                lessons.append(lesson)
    lessons.sort(key=lambda item: (item["date"], item["para"] or 0,
                                   item["start"], item["subject"]))
    return lessons


def normalize_date(iso_date, human_date=None):
    """Любой формат даты -> YYYY-MM-DD."""
    text = str(iso_date or "").strip()
    if len(text) == 10 and text[4] == "-":
        return text
    if human_date and len(str(human_date)) == 10:
        parts = str(human_date).split(".")
        if len(parts) == 3:
            return "%s-%s-%s" % (parts[2], parts[1], parts[0])
    if len(text) == 10 and text[2] == ".":
        parts = text.split(".")
        return "%s-%s-%s" % (parts[2], parts[1], parts[0])
    return text


def lesson_uid(lesson):
    """Устойчивый идентификатор пары — по нему считаем изменения."""
    parts = [
        str(lesson.get("date", "")),
        str(lesson.get("para", "")),
        str(lesson.get("start", "")),
        str(lesson.get("end", "")),
        str(lesson.get("subject", "")).strip().lower(),
        str(lesson.get("type", "")).strip().lower(),
        ",".join(sorted(lesson.get("teachers") or [])),
        ",".join(sorted(lesson.get("rooms") or [])),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def digest(lessons):
    """Хэш всей недели — сравнение «было/стало» без разбора деталей."""
    joined = "\n".join(sorted(item.get("uid", "") for item in lessons or []))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def group_by_date(lessons):
    """{дата: [пары]} с сохранением порядка."""
    result = {}
    for lesson in lessons or []:
        result.setdefault(lesson["date"], []).append(lesson)
    return result


def group_by_slot(lessons):
    """Пары одного слота (день + номер пары + время) — это подгруппы одной пары.

    Вуз часто делит группу на подгруппы (язык, информатика, физкультура,
    медицинские знания): сайт отдаёт каждую подгруппу отдельной строкой с тем же
    временем, но другим преподавателем и аудиторией. Для студента это не две
    разные пары, а одна — со своим вариантом.

    Возвращает блоки:
        [{'date': '2026-10-06', 'para': 1, 'start': '08:30', 'end': '09:50',
          'lessons': [пара, пара, ...]}, ...]
    """
    blocks = []
    positions = {}
    for lesson in lessons or []:
        key = (lesson.get("date"), lesson.get("para"), lesson.get("start"))
        if key not in positions:
            positions[key] = len(blocks)
            blocks.append({
                "date": lesson.get("date"),
                "para": lesson.get("para"),
                "start": lesson.get("start"),
                "end": lesson.get("end"),
                "lessons": [],
            })
        blocks[positions[key]]["lessons"].append(lesson)
    return blocks


def is_split_slot(block):
    """Делится ли пара на подгруппы (несколько строк в одном слоте)."""
    return len((block or {}).get("lessons") or []) > 1


def diff_lessons(old_lessons, new_lessons):
    """Разница между двумя версиями недели.

    Возвращает список словарей:
      {'kind': 'added'|'removed'|'moved'|'changed', 'date': 'YYYY-MM-DD', 'lesson': {...}, 'was': {...}}
    Порядок: по дате и номеру пары. Учитываются пары только этой недели.
    """
    old_by_uid = {item["uid"]: item for item in old_lessons or []}
    new_by_uid = {item["uid"]: item for item in new_lessons or []}
    result = []

    for uid in sorted(set(new_by_uid) - set(old_by_uid)):
        lesson = new_by_uid[uid]
        result.append({"kind": "added", "date": lesson["date"], "lesson": lesson,
                       "was": match_by_slot(old_lessons, lesson)})
    for uid in sorted(set(old_by_uid) - set(new_by_uid)):
        lesson = old_by_uid[uid]
        result.append({"kind": "removed", "date": lesson["date"], "lesson": lesson,
                       "was": match_by_slot(new_lessons, lesson)})

    result.sort(key=lambda item: (item["date"], (item["lesson"].get("para") or 0),
                                  item["kind"]))
    return result


def match_by_slot(other_lessons, lesson):
    """Пара из другой версии, стоящая в том же слоте (дата + номер пары)."""
    for candidate in other_lessons or []:
        if (candidate.get("date") == lesson.get("date")
                and candidate.get("para") == lesson.get("para")):
            return candidate
    return None
