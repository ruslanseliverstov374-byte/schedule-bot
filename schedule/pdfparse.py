# -*- coding: utf-8 -*-
"""Разбор расписания КГАСУ из PDF-сканов: картинки из PDF плюс OCR (tesseract).

С октября 2026 КГАСУ выкладывает расписания файлами PDF, и у 211 из 288 групп
внутри не текст, а скан: JPEG-страница 2424x3476 (примерно 300 dpi), шрифтов и
текстового слоя нет. Поэтому расписание приходится распознавать: достать
картинки из PDF, прогнать OCR и собрать таблицу по координатам слов.

Конвейер (см. :func:`table_from_pdf`)::

    is_pdf -> pdf_images -> ocr_tsv (по странице) -> words_from_tsv
           -> group_rows -> grid_from_words

Внешняя программа ровно одна — ``tesseract``; она вызывается через
:mod:`subprocess` и может отсутствовать: тогда :func:`ocr_available` вернёт
``False``, а :func:`table_from_pdf` поднимет :class:`PdfError`. Импорт модуля от
tesseract не зависит. Используются только модули стандартной библиотеки:
``re``, ``os``, ``zlib``, ``shutil``, ``subprocess`` и ``tempfile``.

Как устроена таблица на скане
-----------------------------
Шапка занимает две строки — как в Word-версии::

    Дата | Номер | Неделя | Время | 26ЗК01                  <- код группы на две колонки
         |       |        |       | Подгруппа 2 | Подгруппа 1
    Понедельник | 1 | Чет | 8:00:00 - 9:30:00 | <занятие> | <занятие>
                |   | Неч |                    | <занятие> | <занятие>

«Дата», «Номер» и «Время» напечатаны один раз на пару строк (объединённые
ячейки), «Чет»/«Неч» — в каждой строке. Поэтому :func:`grid_from_words` ищет
строку шапки по словам «Дата», «Номер», «Неделя», «Время», считает x-границы
первых четырёх столбцов по их центрам, а столбцы занятий — по центрам слов
«Подгруппа N» (и по правому краю значений времени). Дальше каждое слово
попадает в свой столбец по центру по x, а «Дата», «Номер» и «Время» протягиваются
вниз на строку «Неч», как это делает провайдер КГАСУ для .docx.

Две особенности сканов, которых нет в Word
-------------------------------------------
* **День недели повёрнут на 90 градусов.** В столбце «Дата» слова
  «Понедельник», «Вторник», … напечатаны вертикально, и tesseract отдаёт их
  отдельными буквами в разных строках. :func:`_day_column` склеивает такие
  буквы обратно в слово (по близкому x и малому вертикальному зазору) и узнаёт
  день недели, в том числе если буквы распознаны в обратном порядке.
* **Текст ячейки занимает 2–3 строки OCR.** Строка без «Чет»/«Неч» и без
  времени не становится отдельной строкой расписания: её текст дописывается к
  предыдущей строке (перенос как ``'\\n'``, как абзацы в ячейке Word).
  Строки шапки документа («РАСПИСАНИЕ», «УТВЕРЖДАЮ», …) при этом не приклеиваются.

Формат результата совпадает с разбором Word-файлов
(``schedule/kgasu.py``, ``grid_from_bytes``)::

    {'columns': [{'group': '26ЗК01', 'subgroup': 'Подгруппа 2', 'index': 4},
                 {'group': '26ЗК01', 'subgroup': 'Подгруппа 1', 'index': 5}],
     'rows': [{'day': 'Понедельник', 'para': '1', 'parity': 'Чет',
               'time': '8:00:00 - 9:30:00', 'cells': ['…', '…']}, …]}

``index`` — номер столбца в полной сетке таблицы (0 — «Дата», 1 — «Номер»,
2 — «Неделя», 3 — «Время», 4 и дальше — занятия), а ``cells`` содержит только
столбцы занятий в том же порядке, что и ``columns``.

Две мелочи про страницы: у каждой страницы скана своя система координат
(``top`` начинается с нуля), поэтому :func:`table_from_pdf` сдвигает слова
следующей страницы вниз на высоту предыдущей — иначе строки разных страниц
слиплись бы. И ещё: в результат :func:`table_from_pdf` добавляется ключ
``'text'`` — распознанная шапка документа над таблицей, из которой провайдер
берёт начало семестра («с 01.09.2026 г. (16 недель)»), чтобы понять, какая
неделя «Чет», а какая «Неч». Сам :func:`grid_from_words` возвращает ровно
``{'columns', 'rows'}``.

Пример::

    from schedule.pdfparse import table_from_pdf, PdfError

    blob = open("26ЗК01.pdf", "rb").read()
    grid = table_from_pdf(blob, group_hint="26ЗК01", progress=print)
"""

import os
import re
import shutil
import subprocess
import tempfile
import zlib

__all__ = [
    "PdfError",
    "is_pdf",
    "pdf_images",
    "ocr_available",
    "ocr_tsv",
    "words_from_tsv",
    "group_rows",
    "grid_from_words",
    "table_from_pdf",
]

# ------------------------------------------------------------------- константы

#: сигнатура PDF в начале файла
PDF_MAGIC = b"%PDF"

#: начало любого JPEG (SOI)
JPEG_MAGIC = b"\xff\xd8"

#: имя внешней программы OCR
TESSERACT = "tesseract"

#: объект PDF: номер, поколение, тело до endobj
_OBJECT_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)endobj", re.S)

#: прямая длина потока в словаре объекта
_LENGTH_RE = re.compile(rb"/Length\s+(\d+)")

#: слова с такой уверенностью OCR и ниже считаются мусором
MIN_CONF = 40.0

#: шаг сетки строк по вертикали по умолчанию (пиксели скана 300 dpi)
DEFAULT_Y_TOLERANCE = 14

#: зазор между страницами при склейке слов (пиксели): строки страниц не слипаются
PAGE_GAP = 240

#: параметры tesseract по умолчанию
OCR_LANG = "rus"
OCR_PSM = 6
OCR_DPI = 300
OCR_TIMEOUT = 180

#: колонки TSV, которые нужны от tesseract (ищутся по именам в шапке)
_TSV_COLUMNS = ("text", "left", "top", "width", "height", "conf")

#: столбцы шапки таблицы; в ячейки занятий эти слова не попадают
_HEADER_KEYS = ("дата", "номер", "неделя", "время")
_SERVICE_WORDS = frozenset(_HEADER_KEYS)

#: слова шапки документа: их строки не приклеиваются к ячейкам занятий
_DOCUMENT_WORDS = frozenset((
    "расписание", "утверждаю", "казанский", "университет", "направление",
    "подготовки", "семестр", "проректор", "подпись", "числа", "месяца",
))

#: дни недели в том порядке, в каком их отдаёт провайдер
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
            "Воскресенье")

#: код группы КГАСУ: 26ЗК01, 26РП01, 26СМ31з, 26ЗК0102 (с буквами внутри)
_GROUP_RE = re.compile(r"\d{2}[0-9а-яa-z]{1,4}\d{1,3}[а-яa-z0-9]{0,5}")
_GROUP_LETTER_RE = re.compile(r"[а-яa-z]")

#: цифра в подписи подгруппы: «Подгруппа 2», «Подгруппа2», «Подгруппа № 1»
_SUBGROUP_RE = re.compile(r"(\d{1,2})")

#: время пары: «8:00:00 - 9:30:00» (тире бывает разным)
_TIME_SPAN_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[-\u2010-\u2015\u2212]\s*(\d{1,2}:\d{2}(?::\d{2})?)")
_TIME_SINGLE_RE = re.compile(r"\d{1,2}:\d{2}")

#: номер пары
_NUMBER_RE = re.compile(r"\d{1,2}")

#: одиночная строчная буква кириллицы — признак повёрнутого на 90° текста
_LETTER_RE = re.compile(r"[а-я]")

_MULTISPACE_RE = re.compile(r"\s+")


class PdfError(Exception):
    """Ошибка чтения PDF-скана: не PDF, нет картинок, в системе нет tesseract."""


# ------------------------------------------------------------- служебные мелочи

def _as_bytes(blob):
    """Байты из того, что передали; всё неподходящее — пустые байты (без исключений)."""
    if isinstance(blob, (bytes, bytearray, memoryview)):
        return bytes(blob)
    return b""


def _int_value(value, default=0):
    """Целое из строки/числа TSV; мусор превращается в значение по умолчанию."""
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return int(default)


def _float_value(value, default=0.0):
    """Число с плавающей точкой из строки/числа TSV; мусор — значение по умолчанию."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(default)


def _norm_text(text):
    """Сравнимое представление слова: строчные буквы, ё -> е, без знаков препинания."""
    return re.sub(r"[^0-9a-zа-я]+", "", str(text or "").lower().replace("ё", "е"))


def _clean_code(text):
    """Код группы без пробелов и краевой пунктуации: «26 ЗК01.» -> «26ЗК01»."""
    return re.sub(r"\s+", "", str(text or "")).strip(".,;:|/")


# ------------------------------------------------------------------ 1. контейнер

def is_pdf(blob):
    """Начинается ли файл с сигнатуры ``%PDF``.

    Принимает байты (``bytes``, ``bytearray``, ``memoryview``); на пустом
    значении, строке или чём угодно другом возвращает ``False``, не поднимая
    исключение.
    """
    return _as_bytes(blob)[:4] == PDF_MAGIC


def _stream_length(dictionary):
    """Прямая длина потока из ``/Length`` или ``None``, если она косвенная."""
    match = _LENGTH_RE.search(dictionary)
    if not match:
        return None
    return _int_value(match.group(1))


def _inflate(raw):
    """Распаковка FlateDecode; при любой ошибке — пустые байты."""
    try:
        return zlib.decompress(raw)
    except zlib.error:
        try:
            # Поток мог быть обрезан по /Length — пробуем мягкий режим.
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return b""


def pdf_images(blob):
    """JPEG-страницы скана из PDF по порядку следования объектов.

    Берутся только объекты-картинки (``/Subtype /Image``) со сжатием JPEG
    (``/Filter /DCTDecode``). Поток может лежать как есть или быть сжатым
    ``FlateDecode`` — тогда он распаковывается через :mod:`zlib`. Объекты, поток
    которых не начинается с ``FF D8``, пропускаются: это не JPEG.

    На образце ``tests/fixtures/kgasu_26ZK01_scan.pdf`` возвращает две страницы
    2424x3476. На пустом значении, не-PDF и PDF без картинок — пустой список.
    """
    data = _as_bytes(blob)
    if not data:
        return []
    images = []
    for match in _OBJECT_RE.finditer(data):
        body = match.group(3)
        dictionary, marker, tail = body.partition(b"stream")
        if not marker:
            continue                        # объект без потока — не картинка
        if b"/Image" not in dictionary or b"DCTDecode" not in dictionary:
            continue
        raw = tail.lstrip(b"\r\n")
        length = _stream_length(dictionary)
        if length is not None and 0 < length <= len(raw):
            raw = raw[:length]              # точная длина надёжнее поиска endstream
        else:
            raw = raw.split(b"endstream", 1)[0].rstrip(b"\r\n")
        if b"FlateDecode" in dictionary:
            raw = _inflate(raw)
        if raw[:2] != JPEG_MAGIC:
            continue                        # не JPEG: например, чёрно-белый CCITT
        images.append(raw)
    return images


# ------------------------------------------------------------------- 2. OCR

def ocr_available():
    """Есть ли в системе ``tesseract`` (поиск по PATH через :func:`shutil.which`).

    Никогда не поднимает исключение и всегда возвращает ``bool``: если PATH
    недоступен или поиск падает, считается, что OCR нет.
    """
    try:
        return shutil.which(TESSERACT) is not None
    except Exception:                       # noqa: BLE001 — PATH может быть любым
        return False


def _tesseract_args(exe, lang, psm, dpi, source):
    """Аргументы командной строки tesseract: картинка -> TSV в stdout."""
    return [exe, source, "stdout", "-l", lang, "--psm", str(psm),
            "--dpi", str(dpi), "tsv"]


def _run_tesseract(args, image, timeout):
    """Один запуск tesseract; при любой беде (в том числе таймауте) — пустая строка."""
    try:
        done = subprocess.run(
            args,
            input=image,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:                       # noqa: BLE001 — OSError, TimeoutExpired…
        return ""
    if done.returncode != 0 or not done.stdout:
        return ""
    return done.stdout.decode("utf-8", "replace")


def ocr_tsv(image_bytes, lang=OCR_LANG, psm=OCR_PSM, timeout=OCR_TIMEOUT, dpi=OCR_DPI):
    """TSV от tesseract для картинки.

    Команда — ``tesseract stdin stdout -l rus --psm 6 --dpi 300 tsv``: картинка
    подаётся на стандартный вход, распознанный текст приходит на стандартный
    выход. Значения по умолчанию — :data:`OCR_LANG`, :data:`OCR_PSM`,
    :data:`OCR_TIMEOUT` и :data:`OCR_DPI` (русский язык, режим сегментации 6,
    три минуты, 300 dpi). Если tesseract не установлен, упал или не уложился в
    таймаут, возвращается пустая строка — исключение наружу не выходит.

    На части сборок tesseract не читает stdin, поэтому при пустом ответе
    делается вторая попытка через временный файл (файл всегда удаляется).
    """
    data = _as_bytes(image_bytes)
    if not data:
        return ""
    try:
        exe = shutil.which(TESSERACT)
    except Exception:                       # noqa: BLE001
        exe = None
    if not exe:
        return ""

    language = str(lang or OCR_LANG)
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = float(OCR_TIMEOUT)
    if seconds <= 0:
        seconds = float(OCR_TIMEOUT)

    text = _run_tesseract(_tesseract_args(exe, language, psm, dpi, "stdin"),
                          data, seconds)
    if text:
        return text

    handle = None
    name = None
    try:
        handle = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        name = handle.name
        handle.write(data)
        handle.close()
        handle = None
        return _run_tesseract(_tesseract_args(exe, language, psm, dpi, name),
                              None, seconds)
    except Exception:                       # noqa: BLE001 — временный файл не создать
        return ""
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:               # noqa: BLE001
                pass
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


# ------------------------------------------------------------------ 3. слова

def words_from_tsv(tsv):
    """Слова из TSV tesseract: ``[{'text','left','top','width','height','conf'}]``.

    Строка-заголовок TSV начинается со слова ``level``; колонки ищутся по именам
    именно в ней, поэтому порядок колонок может быть любым. Числовые поля
    приводятся к числам (``conf`` — к float), слова с пустым текстом и с
    уверенностью ниже :data:`MIN_CONF` отбрасываются. Мусорные строки (мало
    колонок, нечисловые значения) не ломают разбор, а просто пропускаются.
    """
    if not isinstance(tsv, str) or not tsv.strip():
        return []
    lines = tsv.splitlines()

    header = None
    header_index = 0
    for index, line in enumerate(lines[:5]):        # шапка TSV стоит в начале вывода
        cells = [cell.strip().lower() for cell in line.split("\t")]
        if cells and cells[0] == "level":
            header = cells
            header_index = index
            break
    if header is None:
        return []
    positions = {}
    for name in _TSV_COLUMNS:
        if name in header:
            positions[name] = header.index(name)
    if "text" not in positions:
        return []

    def field(cells, name):
        """Значение колонки по имени; если её нет или строка короче — пустая строка."""
        position = positions.get(name)
        if position is None or position >= len(cells):
            return ""
        return cells[position]

    words = []
    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        text = field(cells, "text").strip()
        if not text:
            continue
        conf_value = field(cells, "conf")
        conf = _float_value(conf_value, 100.0) if conf_value.strip() else 100.0
        if conf < MIN_CONF:
            continue
        words.append({
            "text": text,
            "left": _int_value(field(cells, "left")),
            "top": _int_value(field(cells, "top")),
            "width": _int_value(field(cells, "width")),
            "height": _int_value(field(cells, "height")),
            "conf": conf,
        })
    return words


def group_rows(words, y_tolerance=DEFAULT_Y_TOLERANCE):
    """Собирает слова в строки по вертикали (``top``), внутри строки — по ``left``.

    Слова сортируются по ``top``, новая строка начинается, когда очередное слово
    ниже начала текущей строки больше чем на ``y_tolerance`` пикселей. Внутри
    строки слова сортируются по ``left``. Не-словари и слова без координат не
    ломают разбор: отсутствующие ``top``/``left`` считаются нулями.
    """
    prepared = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        prepared.append((_float_value(word.get("top"), 0.0),
                         _float_value(word.get("left"), 0.0), word))
    prepared.sort(key=lambda item: (item[0], item[1]))

    rows = []
    current = []
    reference = None
    for top, _left, word in prepared:
        if reference is None:
            reference = top
        elif top - reference > y_tolerance:
            rows.append(current)
            current = []
            reference = top
        current.append(word)
    if current:
        rows.append(current)

    for row in rows:
        row.sort(key=lambda word: _float_value(word.get("left"), 0.0))
    return rows


# ------------------------------------------------ 4. геометрия таблицы на скане

def _center_x(word):
    """Центр слова по горизонтали (по нему слово и попадает в столбец)."""
    return _float_value(word.get("left"), 0.0) + _float_value(word.get("width"), 0.0) / 2.0


def _top_y(word):
    """Верхняя граница слова."""
    return _float_value(word.get("top"), 0.0)


def _bottom_y(word):
    """Нижняя граница слова."""
    return _top_y(word) + _float_value(word.get("height"), 0.0)


def _row_span(row):
    """Верх и низ строки слов."""
    if not row:
        return 0.0, 0.0
    return min(_top_y(word) for word in row), max(_bottom_y(word) for word in row)


def _median_height(words):
    """Медиана высот слов: по ней считаются допуски на склейку строк."""
    heights = sorted(_float_value(word.get("height"), 0.0) for word in words or []
                     if isinstance(word, dict) and _float_value(word.get("height"), 0.0) > 0)
    if not heights:
        return 0.0
    return heights[len(heights) // 2]


def _find_header(rows):
    """Строка шапки таблицы и центры слов «Дата», «Номер», «Неделя», «Время».

    Возвращает ``{'first', 'last', 'centers'}``: строки, которые занимает шапка
    (OCR иногда разбивает её на две соседние, если слова стоят на разной высоте),
    и центры найденных слов. Нужны минимум три слова из четырёх: центр
    пропавшего достраивается по шагу сетки (:func:`_header_centers`). Если шапки
    нет, возвращается ``None``.
    """
    best = None
    best_key = None
    for index in range(len(rows)):
        for width in (1, 2):
            if index + width > len(rows):
                continue
            centers = {}
            for row in rows[index:index + width]:
                for word in row:
                    key = _norm_text(word.get("text"))
                    if key in _SERVICE_WORDS and key not in centers:
                        centers[key] = _center_x(word)
            if len(centers) < 3:
                continue
            # больше слов — лучше; при равенстве выбирается самая узкая полоса
            key = (-len(centers), width, index)
            if best_key is None or key < best_key:
                best_key = key
                best = {"first": index, "last": index + width - 1, "centers": centers}
    return best


def _header_centers(centers):
    """Четыре центра шапки по x; недостающие достраиваются линейно по шагу сетки."""
    known = sorted(((_HEADER_KEYS.index(key), value) for key, value in centers.items()),
                   key=lambda item: item[1])
    first_index, first_value = known[0]
    step = 1.0
    if len(known) > 1 and known[-1][0] != first_index:
        step = (known[-1][1] - first_value) / float(known[-1][0] - first_index)
    if step <= 0:                           # слова распознаны в странном порядке
        step = 1.0
    return [first_value + step * (position - first_index) for position in range(4)]


def _subgroup_label(text):
    """Подпись подгруппы в том виде, в каком её отдаёт Word-версия: «Подгруппа 2»."""
    source = _MULTISPACE_RE.sub(" ", str(text or "")).strip()
    digits = _SUBGROUP_RE.search(source)
    if digits:
        return "Подгруппа %s" % digits.group(1)
    return source


def _subgroup_entries(row):
    """Подгруппы в строке шапки: ``[{'label': 'Подгруппа 2', 'center': x}]``.

    Цифра подгруппы часто распознаётся отдельным словом («Подгруппа» и «2»
    рядом), поэтому она приклеивается к слову шапки, если стоит сразу справа
    на той же высоте. Центр считается по всей подписи целиком — это и есть
    центр столбца занятий.
    """
    entries = []
    used = set()
    for position, word in enumerate(row):
        text = str(word.get("text") or "").strip()
        if not _norm_text(text).startswith("подгрупп"):
            continue
        left = _float_value(word.get("left"), 0.0)
        right = left + _float_value(word.get("width"), 0.0)
        height = max(1.0, _float_value(word.get("height"), 0.0))
        label = text
        if not _SUBGROUP_RE.search(text):
            for other_position, other in enumerate(row):
                if other_position == position or other_position in used:
                    continue
                other_text = str(other.get("text") or "").strip()
                if not re.fullmatch(r"\d{1,2}", other_text):
                    continue
                other_left = _float_value(other.get("left"), 0.0)
                if not 0 <= other_left - right <= max(60.0, 2.0 * height):
                    continue
                if abs(_top_y(other) - _top_y(word)) > 1.5 * height:
                    continue
                label = "%s %s" % (text, other_text)
                right = other_left + _float_value(other.get("width"), 0.0)
                used.add(other_position)
                break
        entries.append({"label": _subgroup_label(label), "center": (left + right) / 2.0})
    entries.sort(key=lambda item: item["center"])
    # Одно и то же слово OCR иногда отдаёт дважды: лишняя колонка занятий
    # удвоила бы занятия, поэтому повторы с почти совпадающим центром убираются.
    unique = []
    seen = set()
    for item in entries:
        key = (_norm_text(item["label"]), int(item["center"] / 50.0))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _find_subgroups(rows, start):
    """Строка «Подгруппа N»: её индекс и подгруппы слева направо.

    Центры этих подписей задают столбцы занятий: сколько подгрупп нашлось,
    столько колонок и будет (у КГАСУ «Подгруппа 2» стоит левее «Подгруппы 1»).
    """
    for index in range(max(0, start), len(rows)):
        if any(_norm_text(word.get("text")).startswith("подгрупп")
               for word in rows[index]):
            return index, _subgroup_entries(rows[index])
    return None, []


def _looks_like_group(code):
    """Похоже ли слово на код группы: «26зк01», «26см31з» — да, «070926» — нет."""
    if not code or not _GROUP_RE.fullmatch(code):
        return False
    return bool(_GROUP_LETTER_RE.search(code))      # в дате букв не бывает


def _find_groups(rows, first, last, left_bound):
    """Коды групп в шапке: список ``{'index','code','center'}`` слева направо.

    Ищутся только строки шапки (от строки «Дата|Номер|Неделя|Время» до строки
    подгрупп) и только правее столбца «Неделя» — иначе за код группы легко
    принять дату из ячейки занятия.
    """
    found = []
    seen = set()
    last = min(last, len(rows) - 1)
    for index in range(first, last + 1):
        for word in rows[index]:
            code = _norm_text(word.get("text"))
            if not _looks_like_group(code):
                continue
            center = _center_x(word)
            if center <= left_bound:
                continue
            key = (code, int(center / 50.0))
            if key in seen:
                continue
            seen.add(key)
            found.append({"index": index, "code": _clean_code(word.get("text")),
                          "center": center})
    found.sort(key=lambda item: item["center"])
    return found


def _pick_column(center, bounds):
    """Индекс столбца, в который попадает центр слова (``None`` — бесконечность)."""
    for index, (left, right) in enumerate(bounds):
        if (left is None or center >= left) and (right is None or center < right):
            return index
    return len(bounds) - 1


def _split_row(row, bounds):
    """Текст строки по столбцам: список длиной ``len(bounds)``.

    Слова шапки («Дата», «Номер», «Неделя», «Время», «Подгруппа …») в текст
    столбцов не попадают, внутри столбца слова склеиваются через пробел.
    """
    texts = [""] * len(bounds)
    for word in row:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        key = _norm_text(text)
        if key in _SERVICE_WORDS or key.startswith("подгрупп"):
            continue
        position = _pick_column(_center_x(word), bounds)
        texts[position] = (texts[position] + " " + text).strip()
    return texts


def _time_right_edge(rows, start, left_bound, right_bound):
    """Правый край значений времени — по нему находится левая граница занятий.

    Берётся самый правый край слова со временем в полосе от ``left_bound``
    (правая граница «Недели») до ``right_bound`` (примерно полтора шага сетки
    правее центра «Время»). Ограничение полосы нужно, чтобы «8:00» из ячейки
    занятия не отодвинуло столбцы занятий вправо.
    """
    edges = []
    for row in rows[start:]:
        for word in row:
            center = _center_x(word)
            if not left_bound < center < right_bound:
                continue
            if not _TIME_SINGLE_RE.search(str(word.get("text") or "")):
                continue
            edges.append(_float_value(word.get("left"), 0.0)
                         + _float_value(word.get("width"), 0.0))
    return max(edges) if edges else None


def _canonical_time(text):
    """Время пары в едином виде «8:00:00 - 9:30:00»; пусто, если времени нет."""
    source = _MULTISPACE_RE.sub(" ", str(text or "")).strip()
    if not source:
        return ""
    span = _TIME_SPAN_RE.search(source)
    if span:
        return "%s - %s" % (span.group(1), span.group(2))
    single = _TIME_SINGLE_RE.search(source)
    if single:
        return source                       # нестандартная запись — как распознано
    return ""


def _canonical_parity(text):
    """«Чет»/«Неч» из своей строки; пусто, если метки недели в строке нет."""
    key = _norm_text(text)
    if key.startswith("неч"):
        return "Неч"
    if key.startswith("чет") and not key.startswith("четвер"):
        return "Чет"                        # «Четверг» — это день, а не неделя
    return ""


def _canonical_para(text):
    """Номер пары цифрами: «1», «2», …; пусто, если цифр нет."""
    match = _NUMBER_RE.search(_norm_text(text))
    return str(int(match.group(0))) if match else ""


def _canonical_day(text):
    """Название дня недели из распознанного текста.

    Кроме точного совпадения учитываются две беды OCR: лишние буквы внутри
    слова и повёрнутый на 90° текст, распознанный в обратном порядке
    («кинедьнеоП» — это «Понедельник»).
    """
    key = _norm_text(text)
    if not key:
        return ""
    variants = (key, key[::-1])
    for name in WEEKDAYS:
        wanted = _norm_text(name)
        for variant in variants:
            if variant == wanted or wanted in variant:
                return name
            if (len(variant) >= 4 and abs(len(variant) - len(wanted)) <= 3
                    and (wanted.startswith(variant[:4]) or variant.startswith(wanted[:4]))):
                return name
    return ""


def _has_document_words(row):
    """Есть ли в строке слова шапки документа (тогда это не продолжение занятия)."""
    for word in row:
        key = _norm_text(word.get("text"))
        if not key:
            continue
        if key in _SERVICE_WORDS or key.startswith("подгрупп"):
            return True
        for stop in _DOCUMENT_WORDS:
            if key.startswith(stop):
                return True
    return False


def _day_column(rows, bounds, start):
    """День недели для каждой строки ниже шапки (список той же длины).

    В столбце «Дата» скана день недели напечатан вертикально, поэтому tesseract
    отдаёт его отдельными буквами в разных строках. Буквы с близким x и малым
    вертикальным зазором склеиваются обратно в слово; день недели начинает
    действовать со строки, где встретилась первая буква (или целое слово), и
    дальше протягивается вниз — как объединённая ячейка в Word. Если буквы
    распознать не удалось, день остаётся пустым (провайдер такой файл не
    использует).
    """
    column = [[word for word in row if _pick_column(_center_x(word), bounds) == 0]
              for row in rows[start:]]
    height = _median_height([word for words in column for word in words]) or 30.0
    gap_limit = max(12.0, 1.6 * height)
    left_limit = max(12.0, 0.6 * height)

    clusters = []
    for index, words in enumerate(column):
        if not words:
            continue
        top, bottom = _row_span(words)
        single = len(words) == 1 and _LETTER_RE.fullmatch(_norm_text(words[0].get("text")))
        if single:
            word = words[0]
            left = _float_value(word.get("left"), 0.0)
            if clusters:
                last = clusters[-1]
                close = (abs(left - last["x"]) <= left_limit
                         and top - last["bottom"] <= gap_limit)
                if last["letters"] and close:
                    last["letters"].append(word)
                    last["bottom"] = max(last["bottom"], bottom)
                    continue
            clusters.append({"letters": [word], "words": [], "x": left, "top": top,
                             "bottom": bottom, "index": index})
            continue
        clusters.append({"letters": [], "words": words, "x": _center_x(words[0]),
                         "top": top, "bottom": bottom, "index": index})

    pairs = []
    for cluster in clusters:
        if cluster["letters"]:
            # буквы одного повёрнутого слова склеиваются без пробелов
            text = "".join(str(word.get("text") or "").strip()
                           for word in cluster["letters"])
        else:
            text = " ".join(str(word.get("text") or "").strip()
                            for word in cluster["words"])
        text = _MULTISPACE_RE.sub(" ", text).strip()
        day = _canonical_day(text)
        if not day and len(_norm_text(text)) >= 3 and _norm_text(text).isalpha():
            day = text                       # день распознан, но с ошибками — как есть
        if day:
            pairs.append((cluster["index"], day))

    days = [""] * len(column)
    for index, day in pairs:
        if 0 <= index < len(days):
            days[index] = day
    if pairs:
        # Строки выше первой буквы дня всё ещё относятся к этому дню.
        for index in range(min(pairs[0][0], len(days))):
            if not days[index]:
                days[index] = pairs[0][1]
    return days


# ------------------------------------------------------------ 5. сборка таблицы

def grid_from_words(words, group_hint=""):
    """Собирает таблицу расписания из слов OCR.

    Порядок работы:

    1. строка шапки ищется по словам «Дата», «Номер», «Неделя», «Время», их
       центры по x задают границы первых четырёх столбцов, а строка — начало
       данных;
    2. ниже ищутся строка с кодом группы («26ЗК01») и строка с «Подгруппа N»:
       центры слов подгрупп задают столбцы занятий (нет подгрупп — одна колонка),
       каждая колонка получает код ближайшей к ней группы;
    3. слова раскладываются по столбцам по своему центру по x;
    4. «Дата», «Номер» и «Время» протягиваются вниз (в скане это объединённые
       ячейки), «Чет»/«Неч» берутся из своей строки;
    5. слова шапки в ячейки занятий не попадают;
    6. строки без «Чет»/«Неч» и без времени не становятся строками расписания:
       это либо продолжение текста ячейки (дописывается к предыдущей строке
       через ``'\\n'``), либо шапка документа.

    Если шапку найти не удалось, возвращается ``{'columns': [], 'rows': []}`` —
    без исключения.

    :param words: слова из :func:`words_from_tsv` (можно со всех страниц сразу).
    :param group_hint: ожидаемый код группы (например, «26ЗК01»): по нему
        выбирается нужная строка шапки, если кодов в файле несколько.
    """
    rows = group_rows(words)
    header = _find_header(rows)
    if header is None:
        return {"columns": [], "rows": []}

    x = _header_centers(header["centers"])
    step = x[3] - x[2]
    if step <= 0:
        step = max(1.0, (x[3] - x[0]) / 3.0)
    b1 = (x[0] + x[1]) / 2.0                # Дата | Номер
    b2 = (x[1] + x[2]) / 2.0                # Номер | Неделя
    b3 = (x[2] + x[3]) / 2.0                # Неделя | Время

    subgroup_index, subgroups = _find_subgroups(rows, header["first"])
    last_header = subgroup_index if subgroup_index is not None else header["last"] + 1
    last_header = min(last_header, len(rows) - 1)
    groups = _find_groups(rows, header["first"], last_header, b2)
    hint = _norm_text(group_hint)
    exact = [item for item in groups if hint and _norm_text(item["code"]) == hint]
    if exact:
        groups = exact

    data_start = max([header["last"]] + [index for index in
                                         (subgroup_index,
                                          groups[0]["index"] if groups else None)
                                         if index is not None]) + 1

    # Границы столбцов занятий: центры подгрупп и правый край значений времени.
    subgroup_centers = [item["center"] for item in subgroups]
    time_right = _time_right_edge(rows, data_start, b3, x[3] + 1.5 * step)
    limits = []
    if len(subgroup_centers) >= 2:
        limits.append(subgroup_centers[0] - (subgroup_centers[1] - subgroup_centers[0]) / 2.0)
    if time_right is not None:
        limits.append(time_right + 0.15 * step)
    lesson_left = min(limits) if limits else x[3] + step / 2.0
    if time_right is not None:
        # Столбец времени обязан вместить значения целиком, иначе время пары
        # разорвётся между столбцами и строка потеряет свою пару.
        lesson_left = max(lesson_left, time_right + 0.05 * step)
    lesson_left = max(lesson_left, x[3] + 0.25 * step)

    bounds = [(None, b1), (b1, b2), (b2, b3), (b3, lesson_left)]
    if subgroup_centers:
        edges = [lesson_left] + [(left + right) / 2.0 for left, right in
                                 zip(subgroup_centers, subgroup_centers[1:])]
        for position, left in enumerate(edges):
            right = edges[position + 1] if position + 1 < len(edges) else None
            bounds.append((left, right))
    else:
        bounds.append((lesson_left, None))  # одна колонка занятий на всю ширину

    def group_at(center):
        """Код группы для столбца занятий — по ближайшему слову шапки."""
        if not groups:
            return _clean_code(group_hint)
        return min(groups, key=lambda item: abs(item["center"] - center))["code"]

    columns = []
    for position, item in enumerate(subgroups):
        columns.append({"group": group_at(item["center"]),
                        "subgroup": item["label"],
                        "index": 4 + position})
    if not columns:
        # Подгрупп нет: вся ширина занятий — одна колонка найденной группы.
        columns.append({"group": groups[0]["code"] if groups else _clean_code(group_hint),
                        "subgroup": "", "index": 4})

    days = _day_column(rows, bounds, data_start)
    height = _median_height([word for row in rows[data_start:] for word in row]) or 30.0
    gap_limit = max(24.0, 2.5 * height)

    result = []
    current = {"day": "", "para": "", "time": ""}
    last_row = None
    last_bottom = 0.0
    for offset, row in enumerate(rows[data_start:]):
        texts = _split_row(row, bounds)
        if offset < len(days) and days[offset]:
            current["day"] = days[offset]   # объединённая ячейка «Дата»
        parity = _canonical_parity(texts[2])
        when = _canonical_time(texts[3])
        if not parity and not when:
            top, _bottom = _row_span(row)
            joinable = (last_row is not None and not _has_document_words(row)
                        and not (offset < len(days) and days[offset])
                        and top - last_bottom <= gap_limit)
            if joinable:
                _append_cells(last_row["cells"], texts[4:])
            continue

        para = _canonical_para(texts[1])
        if para:
            current["para"] = para
        if when:
            current["time"] = when
        new_row = {"day": current["day"], "para": current["para"], "parity": parity,
                   "time": current["time"],
                   "cells": [text.strip() for text in texts[4:]]}
        result.append(new_row)
        last_row = new_row
        last_bottom = _row_span(row)[1]
    return {"columns": columns, "rows": result}


def _append_cells(cells, extra):
    """Дописывает текст строки-продолжения в ячейки предыдущей строки расписания."""
    for position, text in enumerate(extra):
        text = str(text or "").strip()
        if not text or position >= len(cells):
            continue
        cells[position] = (cells[position] + "\n" + text) if cells[position] else text


# ---------------------------------------------------------------- 6. конвейер

def _document_header_text(words, rows, header):
    """Текст, напечатанный над таблицей: шапка документа первой страницы.

    Провайдер КГАСУ берёт из этой шапки начало семестра («с 01.09.2026 г.
    (16 недель)») и по нему определяет, какая неделя «Чет», а какая «Неч».
    Возвращаются строки, стоящие выше строки «Дата | Номер | Неделя | Время».
    """
    if header is None or not rows:
        return ""
    first = header["first"]
    if first >= len(rows) or not rows[first]:
        return ""
    top = min(_top_y(word) for word in rows[first])
    above = [word for word in words if _top_y(word) < top]
    lines = []
    for line in group_rows(above):
        text = " ".join(str(word.get("text") or "").strip() for word in line).strip()
        if text:
            lines.append(_MULTISPACE_RE.sub(" ", text))
    return "\n".join(lines)


def table_from_pdf(blob, group_hint="", progress=None):
    """Весь конвейер: PDF-скан -> таблица расписания.

    Картинки достаются через :func:`pdf_images`, каждая страница распознаётся
    один раз (:func:`ocr_tsv`), слова всех страниц складываются по порядку и
    вместе разбираются :func:`grid_from_words`.

    Важно: у каждой страницы своя система координат (``top`` начинается с нуля),
    поэтому слова следующей страницы сдвигаются вниз на высоту предыдущей. Без
    этого строки разных страниц слиплись бы в одни и те же строки таблицы.

    К результату :func:`grid_from_words` добавляется ключ ``'text'`` —
    распознанная шапка документа над таблицей (провайдер берёт из неё начало
    семестра, см. :func:`_document_header_text`).

    :param progress: необязательная функция для лога, вызывается с текстом.
    :raises PdfError: файл не PDF или в системе нет tesseract. PDF без картинок
        не ошибка: возвращается пустая таблица ``{'columns': [], 'rows': []}``.
    """
    log = progress if callable(progress) else (lambda message: None)
    if not is_pdf(blob):
        raise PdfError("это не PDF: файл не начинается с %PDF")

    pages = pdf_images(blob)
    log("страниц-картинок в PDF: %d" % len(pages))
    if not pages:
        return {"columns": [], "rows": [], "text": ""}
    if not ocr_available():
        raise PdfError("OCR недоступен: в системе нет tesseract")

    words = []
    shift = 0
    height = 0
    for number, image in enumerate(pages, 1):
        log("страница %d из %d: распознавание (tesseract)" % (number, len(pages)))
        tsv = ocr_tsv(image)
        page_words = words_from_tsv(tsv)
        log("страница %d: слов %d" % (number, len(page_words)))
        bottom = 0
        for word in page_words:
            word["top"] = _int_value(word.get("top")) + shift
            bottom = max(bottom, int(_bottom_y(word)))
        words.extend(page_words)
        height = max(height, bottom)
        shift += height + PAGE_GAP          # страницы не должны накладываться
    log("всего слов: %d" % len(words))

    grid = grid_from_words(words, group_hint=group_hint)
    rows = group_rows(words)                # для шапки документа: она выше таблицы
    grid["text"] = _document_header_text(words, rows, _find_header(rows))
    log("колонок занятий: %d, строк расписания: %d, шапка документа: %s"
        % (len(grid["columns"]), len(grid["rows"]),
           "есть" if grid["text"] else "не найдена"))
    return grid
