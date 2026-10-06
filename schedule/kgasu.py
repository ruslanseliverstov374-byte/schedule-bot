# -*- coding: utf-8 -*-
"""Расписание КГАСУ (Казанский государственный архитектурно-строительный университет).

Источник — страница https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php: список
групп и файлы расписания (Word .doc/.docx) на семестр. Внутри файла таблица:

    Дата | Номер | Неделя | Время | <группа> (Подгруппа 1 / Подгруппа 2) | ...

На каждую пару две строки — «Чет» и «Неч» (чётная и нечётная неделя), а в ячейках
встречаются пометки «С 28.09.26.» (действует с этой даты) и «до 01.11.26.» (по эту дату).

Особенности источника, проверенные вживую:
  * у сайта неполная цепочка TLS-сертификата, поэтому для доменов kgasu.ru/st.kgasu.ru
    подключение идёт без проверки сертификата (страница публичная, авторизации нет);
  * 7 файлов в формате .docx (чистая таблица) и 13 в старом .doc — для них текст
    восстанавливается из OLE-потока и строки собираются по колонкам.

Зависимости — только стандартная библиотека (см. docxparse.py и docparse.py).
"""

import hashlib
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from schedule import docparse, docxparse

PAGE = "https://www.kgasu.ru/student/raspisanie-zanyatiy/index.php"
CACHE_DIR = "data/cache/kgasu"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIP_LESSONS = "Расписание занятий"
TIP_TESTS = "Расписание зачетов"
TIP_EXAMS = "Расписание консультаций и экзаменов"

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
            "Воскресенье"]

TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
FROM_RE = re.compile(r"[Сс]\s+(\d{2})\.(\d{2})\.(\d{2,4})")
TO_RE = re.compile(r"до\s+(\d{2})\.(\d{2})\.(\d{2,4})")
ROOM_RE = re.compile(r"(?<![\d.])(\d{1,3}\s*[-/]\s*\d{2,4}[а-яА-Я]?|\d{3,4}[а-яА-Я]?)"
                     r"(?![\d.])")
TEACHER_RE = re.compile(
    r"(?:доц|проф|ст\.?\s*пр(?:еп)?|преп|асс|ассист|ст\.?\s*преподаватель)\.?\s*"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.(?:\s*[А-ЯЁ]\.)?)?)", re.UNICODE)
TYPE_WORDS = ("лекция", "лекции", "практические", "практика", "лабораторные", "лаборатория",
              "семинар", "семинары", "консультация", "экзамен", "зачет", "зачёт",
              "курсовая работа", "контрольная работа")


class KgasuError(Exception):
    """Ошибка получения расписания КГАСУ."""


# ------------------------------------------------------------------ сеть и кэш

def fetch(url, timeout=40, retries=2, sleep=time.sleep):
    """Скачивает страницу или файл. Для kgasu.ru — без проверки сертификата."""
    last_error = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/octet-stream,*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        context = ssl._create_unverified_context() if "kgasu.ru" in url else None
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read()
        except Exception as error:
            last_error = error
            if attempt < retries:
                sleep(1.5 * (attempt + 1))
    raise KgasuError("не удалось получить %s: %s" % (url, last_error))


def fetch_text(url, **kwargs):
    return fetch(url, **kwargs).decode("utf-8", "replace")


def cache_path(file_url, directory=CACHE_DIR):
    name = file_url.split("/")[-1]
    digest = hashlib.sha1(file_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(directory, "%s-%s" % (digest, name))


def download(file_url, max_age_minutes=180, directory=CACHE_DIR):
    """Скачивает файл расписания с кэшем на диске."""
    path = cache_path(file_url, directory)
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 60.0
        if age < max_age_minutes:
            with open(path, "rb") as handle:
                return handle.read(), path
    blob = fetch(file_url, timeout=60)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)
    except OSError:
        pass
    return blob, path


# ------------------------------------------------------------- список групп

def filter_options(html, field):
    """Значения выпадающего списка фильтра: [(значение, подпись)]."""
    block = re.search(r'name="%s"[^>]*>(.*?)</select>' % re.escape(field), html, re.S)
    if not block:
        return []
    return [(value, title.strip()) for value, title in
            re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', block.group(1))]


def filter_value(options, wanted, default=""):
    for value, title in options:
        if wanted.lower() in title.lower():
            return value
    return default


def groups(tip=TIP_LESSONS, year="2026-2027", semester="Осенний", html=None):
    """Список групп с файлами расписания.

    Возвращает [{'name': '26РП01', 'file_url': ..., 'file_name': ..., 'file_ext': 'docx',
                 'shared_with': ['26РП02'], 'institute': ''}].
    """
    html = html or fetch_text(PAGE, timeout=60)
    params = {
        "arrFilter_pf[TIP_RASP]": filter_value(filter_options(html, "arrFilter_pf[TIP_RASP]"), tip),
        "arrFilter_pf[UCH_GOD]": filter_value(filter_options(html, "arrFilter_pf[UCH_GOD]"), year),
        "arrFilter_pf[SEMESTR]": filter_value(filter_options(html, "arrFilter_pf[SEMESTR]"), semester),
        "set_filter": "Y",
    }
    listing = fetch_text(PAGE + "?" + urllib.parse.urlencode(params), timeout=60)
    pattern = re.compile(
        r'href="(https://st\.kgasu\.ru/[^"]+\.(?:docx?|xlsx?|pdf))"[^>]*>([^<]{1,120})</a>',
        re.I)
    result = []
    for link, title in pattern.findall(listing):
        title = re.sub(r"\s+", " ", title).strip()
        codes = [code for code in re.split(r"[,\s]+", title) if code]
        for code in codes:
            result.append({
                "name": code,
                "file_url": link,
                "file_name": link.split("/")[-1],
                "file_ext": link.rsplit(".", 1)[-1].lower(),
                "shared_with": [other for other in codes if other != code],
            })
    return result


def find_group(groups_list, query):
    """Найти группу по коду: точное совпадение, затем начало, затем вхождение."""
    query = (query or "").strip().lower().replace(" ", "")
    if not query:
        return None
    names = [(str(item.get("name", "")), item) for item in groups_list or []]
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


# ------------------------------------------------------------------ таблица

def grid_from_bytes(blob, file_name=""):
    """Таблица расписания: {'header': [...], 'columns': [...], 'rows': [[...], ...]}.

    columns — описание столбцов занятий: [{'group': '26РП01', 'subgroup': 'Подгруппа 1'}].
    Для .docx берётся готовая таблица Word, для .doc строки собираются по времени.
    """
    if blob[:2] == b"PK" or file_name.lower().endswith(".docx"):
        tables = docxparse.docx_tables(blob)
        table = _pick_schedule_table(tables)
        if table is None:
            raise KgasuError("в файле %s не найдена таблица расписания" % file_name)
        header_index, columns = _columns_from_rows(table)
        rows = _fill_down(table, header_index)
        return {"header": table[header_index - 1] if header_index else [],
                "columns": columns, "rows": rows, "kind": "docx"}
    text = docparse.doc_text(blob)
    return {"header": [], "columns": [], "rows": [], "text": text, "kind": "doc"}


def _pick_schedule_table(tables):
    """Таблица, в которой есть «Дата»/«Номер»/«Неделя» в заголовке."""
    best = None
    for table in tables:
        flat = " ".join(" ".join(row) for row in table[:3]).lower()
        if "неделя" in flat and ("номер" in flat or "время" in flat):
            if best is None or len(table) > len(best):
                best = table
    return best


def _columns_from_rows(table):
    """Индекс строки заголовка занятий и описание столбцов групп/подгрупп."""
    header_index = 0
    for index, row in enumerate(table[:5]):
        flat = " ".join(row).lower()
        if "время" in flat and "неделя" in flat:
            header_index = index
            break
    header = table[header_index] if header_index < len(table) else []
    subgroup_row = table[header_index + 1] if header_index + 1 < len(table) else []
    columns = []
    current_group = ""
    subgroup_seen = {}
    for position in range(4, max(len(header), len(subgroup_row))):
        group = (header[position] or "").strip() if position < len(header) else ""
        if group:
            current_group = group
            subgroup_seen[current_group] = 0
        subgroup = (subgroup_row[position] or "").strip() if position < len(subgroup_row) else ""
        if subgroup:
            subgroup_seen[current_group] = subgroup_seen.get(current_group, 0) + 1
        columns.append({"group": current_group, "subgroup": subgroup,
                        "index": position})
    return header_index, columns


def _fill_down(table, header_index):
    """Протягивает вниз значения первых четырёх столбцов (в Word они объединены)."""
    rows = []
    current = {"day": "", "para": "", "parity": "", "time": ""}
    for row in table[header_index + 1:]:
        if len(row) < 4:
            continue
        values = [str(cell).strip() for cell in row]
        if values[0]:
            current["day"] = values[0]
        if values[1]:
            current["para"] = values[1]
        if values[2]:
            current["parity"] = values[2]
        if values[3]:
            current["time"] = values[3]
        rows.append({"day": current["day"], "para": current["para"],
                     "parity": current["parity"], "time": current["time"],
                     "cells": values[4:]})
    return rows


# --------------------------------------------------- чётные и нечётные недели

def monday_of(day):
    return day - timedelta(days=day.weekday())


def parse_short_date(text):
    """'28.09.26' -> date(2026, 9, 28)."""
    match = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", (text or "").strip())
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def semester_start(text):
    """Дата начала семестра из шапки: «с 01.09.2026 г. (16 недель)»."""
    match = re.search(r"с\s+(\d{2}\.\d{2}\.\d{2,4})", text or "")
    return parse_short_date(match.group(1)) if match else None


def calibrate_parity(rows):
    """Определяет связь «Чет/Неч» с календарём по пометкам «С <дата>» в ячейках.

    В расписании КГАСУ пометка «С 28.09.26.» означает «действует с этой недели»,
    а строка, где она стоит, помечена «Чет» или «Неч». Этого достаточно, чтобы
    понять, какая неделя сейчас. Если пометок нет — работает правило по умолчанию:
    первая неделя семестра считается «Неч» (проверено на файлах 2026 года).

    Возвращает (опорный понедельник, метка этой недели).
    """
    votes = {}
    for row in rows:
        parity = (row.get("parity") or "").strip().lower()
        if parity not in ("чет", "неч"):
            continue
        for cell in row.get("cells") or []:
            for match in FROM_RE.finditer(str(cell)):
                when = parse_short_date("%s.%s.%s" % match.groups())
                if not when:
                    continue
                monday = monday_of(when)
                votes.setdefault(monday, {"чет": 0, "неч": 0})
                votes[monday][parity] += 1
    if votes:
        best = max(votes, key=lambda key: sum(votes[key].values()))
        counts = votes[best]
        label = "чет" if counts.get("чет", 0) >= counts.get("неч", 0) else "неч"
        return best, label
    return None, "неч"


def parity_of(day, anchor_monday=None, anchor_label="неч"):
    """Какая неделя (Чет/Неч) у даты — по опорной неделе и чередованию."""
    if anchor_monday is None:
        return "Неч"
    weeks = (monday_of(day) - anchor_monday).days // 7
    even = (weeks % 2 == 0)
    is_chet = (anchor_label == "чет") == even
    return "Чет" if is_chet else "Неч"


def resolve_anchor(rows, header_text=""):
    """Опорная неделя для всего документа: из пометок, иначе от начала семестра."""
    anchor_monday, label = calibrate_parity(rows)
    if anchor_monday:
        return anchor_monday, label
    start = semester_start(header_text)
    if start:
        # первая неделя семестра — «Неч» (проверено на файлах 2026 года)
        return monday_of(start), "неч"
    return None, "неч"


# ------------------------------------------------------- разбор текста ячейки

def parse_cell(text):
    """Разбирает ячейку занятия: предмет, тип, преподаватели, аудитория, сроки.

    Ячейки бывают двух видов (оба встречаются в одном файле):

        'Архитектурно-реставрационное проектирование (1 уровень) (Практические)
         доц. Васильева Ю.В., асс. Карнаухова К.4-417'
        'Рисунок (Практические)\\nпроф. Чебинев А. И.\\nКИТАП 2'

    Поэтому разбор идёт не по строкам, а по смыслу: сначала даты, затем тип занятия
    в скобках, затем преподаватели, затем аудитории; что осталось — предмет и место.
    """
    source = str(text or "").strip()
    if not source or source in ("·", "-", "—"):
        return None
    result = {"raw": source, "subject": "", "type": "", "teachers": [], "rooms": [],
              "from_date": None, "to_date": None, "place": ""}
    work = re.sub(r"[ \t\u00a0]+", " ", source)

    # 1. Сроки действия: «С 28.09.26.» и «до 01.11.26.»
    from_match = FROM_RE.search(work)
    if from_match:
        result["from_date"] = parse_short_date("%s.%s.%s" % from_match.groups())
        work = work[:from_match.start()] + " " + work[from_match.end():]
    to_match = TO_RE.search(work)
    if to_match:
        result["to_date"] = parse_short_date("%s.%s.%s" % to_match.groups())
        work = work[:to_match.start()] + " " + work[to_match.end():]

    # 2. Тип занятия — последние скобки со словом типа; «(1 уровень)» убираем отдельно
    for match in reversed(list(re.finditer(r"\(([^)]{2,40})\)", work))):
        inner = match.group(1).strip()
        if any(word in inner.lower() for word in TYPE_WORDS):
            result["type"] = inner
            work = work[:match.start()] + " " + work[match.end():]
            break
    work = re.sub(r"\(\s*[12]\s*уровень\s*\)", " ", work, flags=re.I)

    # 3. Преподаватели: «доц. Иванов И. И.», «асс. Карнаухова К.»
    for match in TEACHER_RE.finditer(work):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" .,")
        name = re.sub(r"([А-ЯЁ])\.?\s*([А-ЯЁ])\.?$", r"\1. \2.", name)
        if name and name not in result["teachers"]:
            result["teachers"].append(name)
    work = TEACHER_RE.sub(" ", work)

    # 4. Аудитории: «4-417», «1-308», «203»
    for match in ROOM_RE.finditer(work):
        room = re.sub(r"\s+", "", match.group(1))
        if room not in result["rooms"]:
            result["rooms"].append(room)
    work = ROOM_RE.sub(" ", work)
    work = re.sub(r"\b[Кк]\.\s*$", " ", work, flags=re.M)

    # 5. Что осталось: первая часть — предмет, остальное — место (например «КИТАП 2»)
    parts = [re.sub(r"\s+", " ", part).strip(" .,;:—-")
             for part in work.split("\n")]
    parts = [part for part in parts if part]
    if parts:
        result["subject"] = parts[0]
    if len(parts) > 1:
        result["place"] = " ".join(parts[1:])[:60]

    # Место может стоять и внутри первой строки: «Физическая культура СК «Тезуче»»
    quoted = re.search(r"\s+(?=[«\"]|[А-ЯЁ]{2,4}\s*[«\"])", result["subject"])
    if quoted and len(result["subject"]) - quoted.start() > 3:
        result["place"] = (result["place"] + " " +
                           result["subject"][quoted.start():].strip()).strip()
        result["subject"] = result["subject"][:quoted.start()].strip(" .,;:—-")
    result["subject"] = re.sub(r"[,;]?\s*$", "", result["subject"]).strip()
    if not result["rooms"] and result["place"]:
        result["rooms"] = [result["place"]]
    result["teachers"] = result["teachers"][:3]
    result["rooms"] = result["rooms"][:2]
    return result


# ------------------------------------------------------------ пары на день

def lessons_for_day(grid, group, day, subgroup=""):
    """Пары группы на конкретную дату (с учётом «Чет/Неч» и пометок «с даты»)."""
    if grid.get("kind") != "docx":
        raise KgasuError("разбор этого файла пока не поддержан (%s)"
                         % grid.get("kind"))
    weekday = WEEKDAYS[day.weekday()]
    anchor_monday, anchor_label = grid.get("anchor") or (None, "неч")
    weekday_parity = parity_of(day, anchor_monday, anchor_label)

    columns = [column for column in grid.get("columns") or []
               if _same_group(column.get("group", ""), group)]
    if subgroup:
        wanted = [column for column in columns
                  if subgroup.lower() in (column.get("subgroup") or "").lower()]
        columns = wanted or columns
    if not columns:
        return []

    lessons = []
    for row in grid.get("rows") or []:
        if (row.get("day") or "").strip() != weekday:
            continue
        if (row.get("parity") or "").strip() != weekday_parity:
            continue
        time_match = TIME_RE.search(row.get("time") or "")
        if not time_match:
            continue
        start = "%02d:%s" % (int(time_match.group(1)), time_match.group(2))
        end = "%02d:%s" % (int(time_match.group(4)), time_match.group(5))
        for column in columns:
            position = column["index"] - 4
            cells = row.get("cells") or []
            if position < 0 or position >= len(cells):
                continue
            parsed = parse_cell(cells[position])
            if not parsed or not parsed["subject"]:
                continue
            if parsed["from_date"] and day < parsed["from_date"]:
                continue          # занятие начинается позже
            if parsed["to_date"] and day > parsed["to_date"]:
                continue          # занятие уже закончилось
            lessons.append({
                "date": day.isoformat(),
                "day_label": "%s, %d" % (weekday, day.day),
                "para": _as_int(row.get("para")),
                "start": start, "end": end,
                "subject": parsed["subject"],
                "type": parsed["type"],
                "teachers": parsed["teachers"],
                "rooms": parsed["rooms"],
                "subgroup": column.get("subgroup", ""),
                "raw": parsed["raw"],
            })
    lessons = _merge_subgroups(lessons)
    lessons.sort(key=lambda item: (item["para"] or 0, item["start"], item["subject"]))
    for lesson in lessons:
        lesson["uid"] = hashlib.sha1(
            "|".join([lesson["date"], str(lesson["para"]), lesson["start"],
                      lesson["subject"], ",".join(lesson["teachers"]),
                      ",".join(lesson["rooms"]), lesson.get("subgroup", "")]
                     ).encode("utf-8")).hexdigest()[:16]
    return lessons


def _merge_subgroups(lessons):
    """Склеивает одинаковые занятия из разных подгрупп одной группы.

    В файлах КГАСУ занятие на всю группу продублировано в столбцах подгрупп,
    поэтому без склейки студент видел бы одну и ту же пару дважды.
    """
    merged = []
    index = {}
    for lesson in lessons:
        key = (lesson["date"], lesson["para"], lesson["start"], lesson["end"],
               lesson["subject"].lower(), lesson["type"].lower(),
               ",".join(lesson["teachers"]), ",".join(lesson["rooms"]))
        if key in index:
            existing = index[key]
            subgroups = [part for part in (existing.get("subgroup", ""),
                                           lesson.get("subgroup", "")) if part]
            if len(set(subgroups)) > 1:
                existing["subgroup"] = " / ".join(sorted(set(subgroups)))
            continue
        index[key] = lesson
        merged.append(lesson)
    return merged


def _same_group(left, right):
    """Сравнение кодов групп: 26РП01 == 26РП01, 26рп01 == 26РП01."""
    return re.sub(r"\s+", "", str(left or "")).lower() == \
           re.sub(r"\s+", "", str(right or "")).lower()


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def prepare(blob, file_name="", header_text=""):
    """Готовит таблицу к работе: добавляет столбцы, опорную неделю и текст шапки."""
    grid = grid_from_bytes(blob, file_name)
    if grid.get("kind") == "docx":
        text = header_text or docxparse.docx_text(blob)
    else:
        text = grid.get("text") or ""
    grid["header_text"] = text[:2000]
    grid["anchor"] = resolve_anchor(grid.get("rows") or [], text)
    return grid
