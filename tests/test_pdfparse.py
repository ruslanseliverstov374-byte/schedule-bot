# -*- coding: utf-8 -*-
"""Самопроверка разбора расписания КГАСУ из PDF-сканов (schedule/pdfparse.py).

Запуск из любой папки::

    python tests/test_pdfparse.py

Сторонних пакетов нет, сеть нужна только один раз: если фикстуры
``tests/fixtures/kgasu_26ZK01_scan.pdf`` (настоящий скан КГАСУ, 825 КБ) ещё нет,
она скачивается с сайта университета, дальше всё работает офлайн. Tesseract в
системе может отсутствовать — тогда проверки OCR идут по ветке «OCR недоступен»,
это нормальный режим, а не провал.

Основные проверки идут по фикстуре (2 страницы JPEG 2424x3476) и по
синтетическим данным: TSV-вывод tesseract, слова таблицы с той же геометрией,
что и на скане, и «повёрнутый» день недели, который OCR отдаёт отдельными
буквами. Сам конвейер table_from_pdf проверяется с подменённой функцией
распознавания (настоящий tesseract для этого не нужен): так видно, что страница
распознаётся ровно один раз, что строки разных страниц не слипаются и что в
``grid['text']`` попадает шапка документа («с 01.09.2026 г. (16 недель)») —
провайдер берёт из неё, какая неделя «Чет», а какая «Неч».

Итог печатается строками ``Проверок: N, провалов: M`` и ``OK: N проверок``;
код возврата 0 — всё прошло, 1 — есть ошибки.
"""

import os
import re
import ssl
import sys
import urllib.request
import zlib

try:  # чтобы русские подписи не ломали вывод в старой консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from schedule.pdfparse import (                          # noqa: E402
    PdfError,
    grid_from_words,
    group_rows,
    is_pdf,
    ocr_available,
    ocr_tsv,
    pdf_images,
    table_from_pdf,
    words_from_tsv,
)

#: образец настоящего скана (26ЗК01, 2 страницы 2424x3476)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "kgasu_26ZK01_scan.pdf")
FIXTURE_URL = "https://st.kgasu.ru/iblock/f09/f09ea1c307f530aa1c96804001181d6c/26ZK01.pdf"

#: шапка TSV, как её печатает tesseract
TSV_HEADER = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
              "left\ttop\twidth\theight\tconf\ttext")

#: слова шапки таблицы, которые не должны попадать в ячейки занятий
SERVICE = ("Дата", "Номер", "Неделя", "Время", "Подгруппа")

CASES = []


def check(title, condition, detail=""):
    """Записывает результат одной проверки."""
    CASES.append((title, bool(condition), detail))
    return bool(condition)


def outcome(func, *args, **kwargs):
    """Что произошло при вызове: 'PdfError', 'без исключения' или другое исключение."""
    try:
        func(*args, **kwargs)
    except PdfError:
        return "PdfError"
    except Exception as exc:                              # noqa: BLE001 — для отчёта
        return "другое исключение %s: %s" % (type(exc).__name__, exc)
    return "без исключения"


# ------------------------------------------------------------- вспомогательные

def jpeg_size(blob):
    """Размер картинки из JPEG-заголовка (ширина, высота) — как в tools/pdf_images.py."""
    index = 2
    while index < len(blob) - 9:
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = int.from_bytes(blob[index + 5:index + 7], "big")
            width = int.from_bytes(blob[index + 7:index + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(blob[index + 2:index + 4], "big")
        index += 2 + length
    return 0, 0


def word(text, left, top, width, height, conf=95.5):
    """Слово OCR с координатами (как из words_from_tsv)."""
    return {"text": text, "left": left, "top": top, "width": width, "height": height,
            "conf": conf}


def at(words, top):
    """Копия слов с другой вертикальной позицией."""
    shifted = []
    for item in words:
        copy = dict(item)
        copy["top"] = top
        shifted.append(copy)
    return shifted


def phrase(text, left, top, step=8):
    """Слова одной строки ячейки: ширина слова зависит от длины, как у OCR."""
    result = []
    position = left
    for part in text.split():
        width = 12 + 7 * len(part)
        result.append(word(part, position, top, width, 30))
        position += width + step
    return result


def pdf_object(number, dictionary, data):
    """Объект PDF с потоком (без xref — для проверки pdf_images этого достаточно)."""
    return (b"%d 0 obj\n" % number + dictionary.replace(b"/Length", b"/Length %d" % len(data)) +
            b"\nstream\n" + data + b"\nendstream\nendobj\n")


#: короткая «картинка»: начало и конец JPEG
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"

#: геометрия синтетического скана: Дата|Номер|Неделя|Время и две подгруппы
HEADER_WORDS = [
    word("с", 60, 20, 20, 30),                  # шапка документа над таблицей
    word("01.09.2026", 90, 20, 160, 30),
    word("(16", 260, 20, 40, 30),
    word("недель)", 310, 20, 110, 30),
    word("Дата", 60, 100, 80, 30),
    word("Номер", 260, 100, 80, 30),
    word("Неделя", 460, 100, 80, 30),
    word("Время", 660, 100, 80, 30),
    word("26ЗК01", 1330, 100, 160, 30),
    word("Подгруппа", 990, 145, 120, 30),
    word("2", 1120, 145, 20, 30),
    word("Подгруппа", 1740, 145, 120, 30),
    word("1", 1870, 145, 20, 30),
]
TIME_WORDS = [word("8:00:00", 600, 0, 90, 30), word("-", 700, 0, 12, 30),
              word("9:30:00", 720, 0, 90, 30)]

#: ожидаемые тексты ячеек синтетического скана
CELL_PSYCHOLOGY = "Психология социального взаимодействия\n(Практические) доц. Вылегжанина"
CELL_HISTORY = "История России (Лекции)"
CELL_PHYSICS = "Физика (Практические) доц. Яунд"
CELL_MATH = "Математика (Лекции)"
CELL_GRAPHICS = "Инженерная графика"
CELL_STATE = "Основы российской государственности"


def shift(words, offset):
    """Сдвигает все слова по вертикали (вторая страница скана)."""
    return [dict(item, top=item["top"] + offset) for item in words]


def scan_words(offset=0):
    """Слова синтетического скана: Понедельник (пары 1–2) и Вторник (пара 2)."""
    words = list(HEADER_WORDS)
    words += at([word("Понедельник", 30, 0, 130, 30), word("1", 290, 0, 20, 30),
                 word("Чет", 470, 0, 60, 30)] + TIME_WORDS, 200)
    words += phrase("Психология социального взаимодействия", 860, 200)
    words += phrase("(Практические) доц. Вылегжанина", 900, 230)
    words += phrase(CELL_HISTORY, 1500, 200)
    words += at([word("Неч", 470, 0, 60, 30)], 280)
    words += phrase(CELL_PHYSICS, 900, 280)
    words += at([word("Вторник", 35, 0, 120, 30), word("2", 290, 0, 20, 30),
                 word("Чет", 470, 0, 60, 30)] + TIME_WORDS, 360)
    words += phrase(CELL_MATH, 900, 360)
    words += phrase(CELL_GRAPHICS, 1500, 360)
    words += at([word("Неч", 470, 0, 60, 30)], 440)
    words += phrase(CELL_STATE, 860, 440)
    return shift(words, offset) if offset else words


def letter_words(day, first_top=200, step=26, left=60, reverse=False):
    """Повёрнутый на 90° день недели: OCR отдаёт его отдельными буквами."""
    letters = list(day)
    if reverse:
        letters = letters[::-1]
    return [word(letter, left, first_top + step * index, 26, 26)
            for index, letter in enumerate(letters)]


def vertical_words(reverse=False):
    """Скан, где день недели распознан по буквам: два дня по две строки."""
    words = list(HEADER_WORDS)
    words += letter_words("Понедельник", 200, 26, 60, reverse)
    words += at([word("1", 290, 0, 20, 30), word("Чет", 470, 0, 60, 30)] + TIME_WORDS, 200)
    words += phrase(CELL_PSYCHOLOGY.replace("\n", " "), 900, 200)
    words += at([word("Неч", 470, 0, 60, 30)], 290)
    words += phrase(CELL_PHYSICS, 900, 290)
    words += letter_words("Вторник", 600, 26, 60, reverse)
    words += at([word("2", 290, 0, 20, 30), word("Чет", 470, 0, 60, 30)] + TIME_WORDS, 700)
    words += phrase(CELL_MATH, 900, 700)
    words += at([word("Неч", 470, 0, 60, 30)], 790)
    words += phrase(CELL_GRAPHICS, 900, 790)
    return words


def load_fixture():
    """Байты фикстуры; если файла нет — пробуем скачать его с сайта КГАСУ."""
    if os.path.exists(FIXTURE):
        with open(FIXTURE, "rb") as handle:
            return handle.read()
    print("фикстуры нет, скачиваю: %s" % FIXTURE_URL)
    try:
        request = urllib.request.Request(
            FIXTURE_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(request, timeout=180,
                                    context=ssl._create_unverified_context()) as response:
            data = response.read()
    except Exception as exc:                              # noqa: BLE001 — сеть ненадёжна
        print("не удалось скачать фикстуру: %s: %s" % (type(exc).__name__, exc))
        return None
    if data[:4] != b"%PDF":
        print("скачался не PDF (%d байт) — фикстуру не сохраняю" % len(data))
        return None
    try:
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        print("не удалось сохранить фикстуру: %s" % exc)
    return data


# ------------------------------------------------------------------ 1. is_pdf

def check_is_pdf(data):
    """Сигнатура PDF: настоящий скан, мусор и не-байты."""
    print("\n1. is_pdf — сигнатура файла")

    check("на реальном скане КГАСУ is_pdf -> True", is_pdf(data) is True, len(data))
    check("байты начинаются с %PDF", data[:4] == b"%PDF", data[:8])
    check("минимальный PDF (%PDF-1.7) распознаётся", is_pdf(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"))
    check("пустые байты — не PDF", is_pdf(b"") is False)
    check("HTML-страница — не PDF", is_pdf(b"<!DOCTYPE html><html>no pdf</html>") is False)
    check("случайный мусор — не PDF", is_pdf(bytes(range(256))) is False)
    check("строка вместо байтов — не PDF и без исключения",
          is_pdf("26ZK01.pdf") is False and is_pdf(None) is False)


# -------------------------------------------------------------- 2. pdf_images

def check_pdf_images(data):
    """Картинки скана: две страницы JPEG 2424x3476 по порядку."""
    print("\n2. pdf_images — страницы скана")
    images = pdf_images(data)

    check("на реальном скане ровно 2 картинки", len(images) == 2, len(images))
    check("картинки — байты", all(isinstance(item, bytes) for item in images))
    check("обе картинки начинаются с FF D8",
          all(item[:2] == b"\xff\xd8" for item in images),
          [item[:2] for item in images])
    check("обе картинки заканчиваются на FF D9",
          all(item[-2:] == b"\xff\xd9" for item in images),
          [item[-2:] for item in images])
    sizes = [jpeg_size(item) for item in images]
    check("первая страница 2424x3476", sizes[0] == (2424, 3476), sizes[0])
    check("вторая страница 2424x3476", sizes[1] == (2424, 3476), sizes[1])
    check("страницы разные (это две разные картинки)",
          images[0] != images[1] and len(images[0]) > len(images[1]),
          [len(item) for item in images])
    check("картинки по размеру сравнимы с исходным PDF",
          sum(len(item) for item in images) > len(data) * 0.9,
          (sum(len(item) for item in images), len(data)))

    check("пустой blob — пустой список", pdf_images(b"") == [])
    check("не PDF — пустой список", pdf_images(b"<html>no images here</html>") == [])
    check("PDF без картинок — пустой список",
          pdf_images(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n") == [])

    raw = pdf_object(1, b"<< /Type /XObject /Subtype /Image /Filter /DCTDecode /Length >>",
                     FAKE_JPEG)
    check("объект-картинка с DCTDecode читается", pdf_images(b"%PDF-1.4\n" + raw) == [FAKE_JPEG])

    packed = zlib.compress(FAKE_JPEG)
    flate = pdf_object(1, b"<< /Subtype /Image /Filter [/FlateDecode /DCTDecode] /Length >>",
                       packed)
    check("FlateDecode-поток распаковывается перед проверкой JPEG",
          pdf_images(b"%PDF-1.4\n" + flate) == [FAKE_JPEG])

    broken = pdf_object(1, b"<< /Subtype /Image /Filter /DCTDecode /Length >>", b"NOT A JPEG")
    check("поток без FF D8 пропускается", pdf_images(b"%PDF-1.4\n" + broken) == [])

    other = pdf_object(1, b"<< /Type /XObject /Length >>", FAKE_JPEG)
    check("объект без /Image и /DCTDecode пропускается",
          pdf_images(b"%PDF-1.4\n" + other) == [])

    pair = pdf_object(1, b"<< /Subtype /Image /Filter /DCTDecode /Length >>", FAKE_JPEG) + \
        pdf_object(2, b"<< /Subtype /Image /Filter /DCTDecode /Length >>",
                   b"\xff\xd8" + b"\x01" * 10 + b"\xff\xd9")
    got = pdf_images(b"%PDF-1.4\n" + pair)
    check("картинки возвращаются по порядку объектов", len(got) == 2 and got[0] == FAKE_JPEG,
          [len(item) for item in got])
    return images


# ------------------------------------------- 3. words_from_tsv и group_rows

def check_words_from_tsv():
    """Разбор TSV tesseract: колонки по именам, отсев мусора и conf < 40."""
    print("\n3. words_from_tsv — слова из TSV")

    tsv = "\n".join([
        TSV_HEADER,
        "5\t1\t1\t1\t1\t1\t100\t200\t80\t30\t95.5\tПонедельник",
        "5\t1\t1\t1\t1\t2\t200\t200\t60\t30\t91\tЧет",
        "5\t1\t1\t1\t1\t3\t300\t200\t50\t30\t37\tмусор",
        "5\t1\t1\t1\t1\t4\t400\t200\t50\t30\t-1\tшум",
        "5\t1\t1\t1\t2\t1\t100\t260\t70\t30\t88\t1",
        "5\t1\t1\t1\t2\t2\t200\t260\t70\t30\t88\t   ",
        "",
    ])
    words = words_from_tsv(tsv)

    check("слова с conf >= 40 и непустым текстом остались", len(words) == 3, len(words))
    check("тексты слов разобраны", [item["text"] for item in words] ==
          ["Понедельник", "Чет", "1"], [item["text"] for item in words])
    check("координаты стали числами",
          (words[0]["left"], words[0]["top"], words[0]["width"], words[0]["height"])
          == (100, 200, 80, 30), words[0])
    check("conf — число с плавающей точкой",
          isinstance(words[0]["conf"], float) and abs(words[0]["conf"] - 95.5) < 0.001,
          words[0]["conf"])
    check("слово с conf 37 отброшено", "мусор" not in [item["text"] for item in words])
    check("слово с conf -1 отброшено", "шум" not in [item["text"] for item in words])
    check("строка с пустым текстом отброшена",
          all(item["text"].strip() for item in words))

    shuffled = "\n".join([
        "level\ttext\tconf\ttop\tleft\twidth\theight",
        "5\tВторник\t93\t500\t120\t90\t30",
    ])
    other = words_from_tsv(shuffled)
    check("колонки ищутся по именам, а не по порядку",
          len(other) == 1 and other[0]["text"] == "Вторник" and other[0]["left"] == 120
          and other[0]["top"] == 500 and other[0]["height"] == 30, other)

    check("пустой TSV — пустой список", words_from_tsv("") == [] and
          words_from_tsv("   \n\n") == [])
    check("не строка (None, байты) — пустой список",
          words_from_tsv(None) == [] and words_from_tsv(b"level\ttext") == [])
    check("TSV без шапки level — пустой список", words_from_tsv("просто текст\nещё строка") == [])
    check("шапка без колонки text — пустой список",
          words_from_tsv("level\tpage_num\tconf\n5\t1\t90") == [])
    check("мусорные строки (мало колонок, нечисловые значения) не ломают разбор",
          words_from_tsv("level\tleft\ttop\twidth\theight\tconf\ttext\n"
                         "мусор\n5\t1\t2\n5\tx\ty\tz\tw\t90\tСлово") ==
          [{"text": "Слово", "left": 0, "top": 0, "width": 0, "height": 0, "conf": 90.0}])
    check("шапка не обязана быть первой строкой",
          len(words_from_tsv("\n" + tsv)) == 3)


def check_group_rows():
    """Сборка строк по top: допуск, сортировка по left, устойчивость к мусору."""
    print("\n4. group_rows — строки по вертикали")

    words = [word("Б", 300, 102, 40, 30), word("А", 100, 100, 40, 30),
             word("В", 500, 100, 40, 30), word("Низ", 100, 300, 40, 30)]
    rows = group_rows(words)
    check("слова с близким top попали в одну строку", len(rows) == 2, len(rows))
    check("внутри строки слова отсортированы по left",
          [item["text"] for item in rows[0]] == ["А", "Б", "В"], rows[0])
    check("далёкое слово стало отдельной строкой",
          [item["text"] for item in rows[1]] == ["Низ"], rows[1])
    check("исходный список слов не испорчен",
          [item["text"] for item in words] == ["Б", "А", "В", "Низ"])

    strict = group_rows(words, y_tolerance=1)
    check("с уменьшенным допуском строка разбивается", len(strict) == 3, len(strict))
    loose = group_rows(words, y_tolerance=300)
    check("с большим допуском всё сливается в одну строку", len(loose) == 1, len(loose))

    check("пустой список — пустой результат",
          group_rows([]) == [] and group_rows(None) == [])
    check("не-словари и слова без координат не ломают разбор",
          [len(row) for row in group_rows([{"text": "без координат"}, "мусор", 5, None])] == [1],
          group_rows([{"text": "без координат"}, "мусор", 5, None]))
    check("строка из одного слова возвращается как одна строка",
          len(group_rows([word("Один", 10, 10, 20, 20)])) == 1)


# --------------------------------------------------- 5. grid_from_words: скан

def check_grid_from_words():
    """Таблица из слов: колонки подгрупп, протяжка значений, тексты ячеек."""
    print("\n5. grid_from_words — синтетический скан")
    grid = grid_from_words(scan_words(), group_hint="26ЗК01")
    rows = grid["rows"]
    columns = grid["columns"]

    check("колонок занятий две", len(columns) == 2, columns)
    check("первая колонка — 26ЗК01 / Подгруппа 2 / индекс 4",
          columns[0] == {"group": "26ЗК01", "subgroup": "Подгруппа 2", "index": 4}, columns[0])
    check("вторая колонка — 26ЗК01 / Подгруппа 1 / индекс 5",
          columns[1] == {"group": "26ЗК01", "subgroup": "Подгруппа 1", "index": 5}, columns[1])
    check("строк расписания 4 (два дня, пары 1 и 2, Чет/Неч)", len(rows) == 4, len(rows))
    check("в строке есть все поля расписания",
          all(set(row) == {"day", "para", "parity", "time", "cells"} for row in rows),
          [sorted(row) for row in rows])
    check("в каждой строке по две ячейки занятий",
          all(len(row["cells"]) == 2 for row in rows), [len(row["cells"]) for row in rows])

    check("первая строка: Понедельник, пара 1, Чет",
          rows[0]["day"] == "Понедельник" and rows[0]["para"] == "1"
          and rows[0]["parity"] == "Чет", rows[0])
    check("время распознано как «8:00:00 - 9:30:00»",
          rows[0]["time"] == "8:00:00 - 9:30:00", rows[0]["time"])
    check("«Неч» взято из своей строки", rows[1]["parity"] == "Неч", rows[1]["parity"])
    check("номер и время протянулись в строку «Неч»",
          rows[1]["para"] == "1" and rows[1]["time"] == "8:00:00 - 9:30:00", rows[1])
    check("день недели протянулся в строку «Неч»",
          rows[1]["day"] == "Понедельник", rows[1]["day"])
    check("во вторник номер пары стал «2»",
          rows[2]["para"] == "2" and rows[3]["para"] == "2",
          [rows[2]["para"], rows[3]["para"]])
    check("во вторник день недели «Вторник»",
          rows[2]["day"] == "Вторник" and rows[3]["day"] == "Вторник",
          [rows[2]["day"], rows[3]["day"]])

    check("текст первой ячейки не смешался со второй",
          rows[0]["cells"][0] == CELL_PSYCHOLOGY and rows[0]["cells"][1] == CELL_HISTORY,
          rows[0]["cells"])
    check("строка-продолжение дописана к предыдущей ячейке (перенос строки)",
          "\n" in rows[0]["cells"][0] and "доц. Вылегжанина" in rows[0]["cells"][0],
          rows[0]["cells"][0])
    check("строка-продолжение не стала отдельной строкой расписания",
          len(rows) == 4 and all("(Практические) доц. Вылегжанина" not in row["cells"][1]
                                 for row in rows))
    check("ячейка «Неч» получила свой текст",
          rows[1]["cells"][0] == CELL_PHYSICS and rows[1]["cells"][1] == "", rows[1]["cells"])
    check("тексты вторника на своих местах",
          rows[2]["cells"] == [CELL_MATH, CELL_GRAPHICS]
          and rows[3]["cells"] == [CELL_STATE, ""], [rows[2]["cells"], rows[3]["cells"]])
    check("служебные слова шапки не попали в ячейки занятий",
          not any(any(service.lower() in cell.lower() for service in SERVICE)
                  for row in rows for cell in row["cells"]),
          [[cell for cell in row["cells"]] for row in rows])

    without_date = [item for item in scan_words() if item["text"] != "Дата"]
    check("шапка из трёх слов тоже работает (центр достраивается)",
          len(grid_from_words(without_date)["rows"]) == 4,
          len(grid_from_words(without_date)["rows"]))

    two_groups = scan_words() + [word("26РП01", 1900, 100, 160, 30)]
    check("без подсказки каждая колонка берёт ближайший код группы",
          [column["group"] for column in grid_from_words(two_groups)["columns"]] ==
          ["26ЗК01", "26РП01"],
          grid_from_words(two_groups)["columns"])
    hinted = grid_from_words(two_groups, group_hint="26РП01")
    check("group_hint выбирает нужный код группы в шапке",
          [column["group"] for column in hinted["columns"]] == ["26РП01", "26РП01"],
          hinted["columns"])

    # без строки подгрупп вся ширина занятий — одна колонка
    single = [item for item in scan_words() if item["top"] != 145]
    single_grid = grid_from_words(single)
    check("без строки подгрупп получается одна колонка занятий",
          len(single_grid["columns"]) == 1
          and single_grid["columns"][0] == {"group": "26ЗК01", "subgroup": "", "index": 4},
          single_grid["columns"])
    check("без подгрупп текст занятия не потерялся",
          bool(single_grid["rows"]) and "Психология" in single_grid["rows"][0]["cells"][0],
          single_grid["rows"][:1])

    doubled = scan_words() + [dict(item) for item in scan_words() if item["top"] == 145]
    check("повторённые слова «Подгруппа» не создают лишних колонок",
          [column["subgroup"] for column in grid_from_words(doubled)["columns"]] ==
          ["Подгруппа 2", "Подгруппа 1"],
          grid_from_words(doubled)["columns"])

    two_pages = scan_words() + scan_words(offset=3000)
    both = grid_from_words(two_pages)
    check("слова двух страниц объединяются в одну таблицу",
          len(both["rows"]) == 8, len(both["rows"]))
    check("вторая страница повторяет дни и пары первой",
          [row["day"] for row in both["rows"][4:]] == [row["day"] for row in both["rows"][:4]]
          and [row["para"] for row in both["rows"][4:]] == [row["para"] for row in both["rows"][:4]],
          [row["day"] for row in both["rows"]])

    check("пустой список слов — пустой результат без исключения",
          grid_from_words([]) == {"columns": [], "rows": []}, grid_from_words([]))
    check("слова без шапки — пустой результат без исключения",
          grid_from_words([word("Привет", 10, 10, 50, 20)]) ==
          {"columns": [], "rows": []})
    check("мусор вместо слов не ломает разбор",
          grid_from_words([{"нет": "координат"}, "строка", None, 7]) ==
          {"columns": [], "rows": []})
    header_only = [item for item in scan_words() if item["top"] < 200]
    only = grid_from_words(header_only)
    check("шапка без данных даёт колонки, но пустой список строк",
          len(only["columns"]) == 2 and only["rows"] == [], only)


def check_vertical_day():
    """Повёрнутый день недели: буквы склеиваются в слово (в том числе наоборот)."""
    print("\n6. grid_from_words — повёрнутый день недели")

    grid = grid_from_words(vertical_words())
    rows = grid["rows"]
    check("таблица собрана: 4 строки", len(rows) == 4, len(rows))
    check("день из отдельных букв узнан: Понедельник",
          [row["day"] for row in rows[:2]] == ["Понедельник", "Понедельник"],
          [row["day"] for row in rows[:2]])
    check("следующий день тоже узнан: Вторник",
          [row["day"] for row in rows[2:]] == ["Вторник", "Вторник"],
          [row["day"] for row in rows[2:]])
    check("буквы дня не попали в ячейки занятий",
          [row["cells"][0].split(" ")[0] for row in rows] ==
          ["Психология", "Физика", "Математика", "Инженерная"],
          [row["cells"][0] for row in rows])

    reversed_grid = grid_from_words(vertical_words(reverse=True))
    check("буквы, распознанные в обратном порядке, тоже узнаются",
          [row["day"] for row in reversed_grid["rows"]] ==
          ["Понедельник", "Понедельник", "Вторник", "Вторник"],
          [row["day"] for row in reversed_grid["rows"]])


# -------------------------------------------------- 7. table_from_pdf и OCR

def check_table_from_pdf(data):
    """Конвейер целиком: фикстура, мусор, PDF без картинок, лог progress."""
    print("\n7. table_from_pdf — конвейер целиком")

    check("на не-PDF поднимается PdfError",
          outcome(table_from_pdf, b"<html>not a pdf</html>") == "PdfError")
    check("на пустом blob поднимается PdfError", outcome(table_from_pdf, b"") == "PdfError")
    check("PdfError — наследник Exception", issubclass(PdfError, Exception))

    empty = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n"
    messages = []
    grid = table_from_pdf(empty, progress=messages.append)
    check("PDF без картинок не ошибка: пустая таблица",
          grid.get("columns") == [] and grid.get("rows") == [] and grid.get("text") == "",
          grid)
    check("progress получил сообщения о страницах",
          bool(messages) and all(isinstance(item, str) for item in messages), messages[:2])
    check("progress может быть не функцией",
          table_from_pdf(empty, progress="просто строка").get("rows") == [])

    if data is None:
        check("фикстура доступна для проверки конвейера", False, FIXTURE)
        return

    if not ocr_available():
        print("   tesseract не найден — проверяем ветку «OCR недоступен»")
        try:
            table_from_pdf(data)
            check("без tesseract поднимается PdfError", False, "исключения не было")
        except PdfError as error:
            check("без tesseract поднимается PdfError", True)
            check("в тексте ошибки объяснено, что нет tesseract",
                  "tesseract" in str(error) and str(error).startswith("OCR недоступен"),
                  str(error))
        except Exception as exc:                          # noqa: BLE001 — для отчёта
            check("без tesseract поднимается PdfError", False,
                  "другое исключение %s: %s" % (type(exc).__name__, exc))
        check("без tesseract картинки скана всё равно извлекаются",
              len(pdf_images(data)) == 2, len(pdf_images(data)))
        return

    grid = table_from_pdf(data, group_hint="26ЗК01")
    rows = grid["rows"]
    days = {row["day"] for row in rows}
    parities = {row["parity"] for row in rows}
    check("распознано больше 20 строк расписания", len(rows) > 20, len(rows))
    check("среди строк есть «Понедельник»", "Понедельник" in days, sorted(days))
    check("среди строк есть «Чет» и «Неч»",
          {"Чет", "Неч"} <= parities, sorted(parities))
    check("колонки занятий найдены", len(grid["columns"]) >= 1, grid["columns"])
    check("время хотя бы одной строки похоже на пару",
          any(re.search(r"\d{1,2}:\d{2}", row["time"]) for row in rows), rows[0]["time"])


def tsv_from_words(words):
    """Собирает TSV в том виде, в каком его печатает tesseract."""
    lines = [TSV_HEADER]
    for index, item in enumerate(words, 1):
        lines.append("5\t1\t1\t1\t1\t%d\t%d\t%d\t%d\t%d\t%.1f\t%s" % (
            index, item["left"], item["top"], item["width"], item["height"],
            item["conf"], item["text"]))
    return "\n".join(lines) + "\n"


def check_pipeline_with_stub(data):
    """Конвейер table_from_pdf с подменённым OCR: вызовы, страницы, лог.

    Настоящий tesseract в системе может отсутствовать, поэтому сам конвейер
    проверяется с подменённой функцией распознавания: она отдаёт тот же TSV, что
    и заглушка tesseract. Так видно, что страница распознаётся ровно один раз и
    что строки разных страниц не слипаются (координаты top у каждой страницы
    начинаются с нуля).
    """
    print("\n8. table_from_pdf с подменённым OCR (tesseract не нужен)")
    from schedule import pdfparse as module

    if data is None:
        check("фикстура доступна для проверки конвейера", False, FIXTURE)
        return

    calls = []

    def fake_ocr(image_bytes, lang="rus", psm=6, timeout=180, dpi=300):
        calls.append(len(bytes(image_bytes)))
        return tsv_from_words(scan_words())      # обе страницы — одна и та же таблица

    original_ocr = module.ocr_tsv
    original_available = module.ocr_available
    module.ocr_tsv = fake_ocr
    module.ocr_available = lambda: True
    try:
        messages = []
        grid = table_from_pdf(data, group_hint="26ЗК01", progress=messages.append)
    finally:
        module.ocr_tsv = original_ocr
        module.ocr_available = original_available

    page_sizes = [len(item) for item in pdf_images(data)]
    check("OCR вызван ровно по одному разу на страницу (2 страницы)", len(calls) == 2, calls)
    check("в OCR ушли именно картинки страниц", calls == page_sizes, (calls, page_sizes))

    rows = grid["rows"]
    check("строки обеих страниц собраны (4 + 4)", len(rows) == 8, len(rows))
    check("колонки занятий взяты из шапки первой страницы",
          [column["subgroup"] for column in grid["columns"]] ==
          ["Подгруппа 2", "Подгруппа 1"], grid["columns"])
    check("строки страниц не слиплись: тексты ячеек без удвоения",
          rows[4]["cells"] == rows[0]["cells"] and rows[7]["cells"] == rows[3]["cells"],
          [rows[4]["cells"], rows[0]["cells"]])
    check("номер пары не склеился из двух страниц",
          [row["para"] for row in rows] == ["1", "1", "2", "2", "1", "1", "2", "2"],
          [row["para"] for row in rows])
    check("время пары не склеилось из двух страниц",
          all(row["time"] == "8:00:00 - 9:30:00" for row in rows),
          sorted({row["time"] for row in rows}))
    check("дни недели второй страницы свои",
          [row["day"] for row in rows[4:]] == ["Понедельник", "Понедельник", "Вторник", "Вторник"],
          [row["day"] for row in rows])
    check("в логе отмечены обе страницы",
          any("страница 1 из 2" in item for item in messages)
          and any("страница 2 из 2" in item for item in messages), messages)
    check("шапка документа над таблицей попала в grid['text']",
          "01.09.2026" in grid["text"] and "недель" in grid["text"], grid["text"])
    check("из шапки читается начало семестра (образец для resolve_anchor)",
          re.search(r"с\s+01\.09\.2026", grid["text"]) is not None, grid["text"])
    check("grid_from_words по-прежнему возвращает ровно columns и rows",
          set(grid_from_words(scan_words())) == {"columns", "rows"},
          sorted(grid_from_words(scan_words())))


def check_ocr_api():
    """ocr_available и ocr_tsv: не бросают исключений и не зависят от tesseract."""
    print("\n9. ocr_available и ocr_tsv")

    available = ocr_available()
    check("ocr_available() возвращает bool", isinstance(available, bool), available)
    check("ocr_available() не бросает исключение", outcome(ocr_available) == "без исключения")
    print("   tesseract: %s" % ("есть" if available else "нет"))

    check("ocr_tsv на пустых байтах — пустая строка", ocr_tsv(b"") == "")
    check("ocr_tsv на None — пустая строка", ocr_tsv(None) == "")
    result = ocr_tsv(b"\xff\xd8 not a real jpeg \xff\xd9")
    check("ocr_tsv на мусоре возвращает строку и не бросает исключение",
          isinstance(result, str), type(result).__name__)
    if not available:
        check("без tesseract ocr_tsv возвращает пустую строку", result == "", result)
    else:
        check("с tesseract ocr_tsv вызывается без ошибок", True)


# ---------------------------------------------------------------------- запуск

def main():
    print("Разбор PDF-сканов КГАСУ: самопроверка schedule/pdfparse.py")
    data = load_fixture()
    if data is None:
        check("фикстура со сканом доступна", False,
              "нет файла %s и не удалось его скачать" % FIXTURE)
    else:
        print("Образец: %s (%d байт)" % (os.path.relpath(FIXTURE, ROOT), len(data)))
        check("фикстура со сканом на месте и не пустая", len(data) > 500000, len(data))
        check_is_pdf(data)
        check_pdf_images(data)

    check_words_from_tsv()
    check_group_rows()
    check_grid_from_words()
    check_vertical_day()
    check_table_from_pdf(data)
    check_pipeline_with_stub(data)
    check_ocr_api()

    failed = 0
    for number, (title, ok, detail) in enumerate(CASES, 1):
        print("%2d. [%s] %s" % (number, "OK" if ok else "FAIL", title))
        if not ok:
            failed += 1
            if detail != "":
                print("      подробности: %s" % (detail,))
    print("-" * 60)
    print("Проверок: %d, провалов: %d" % (len(CASES), failed))
    if failed:
        print("ПРОВАЛ: %d из %d проверок не пройдено" % (failed, len(CASES)))
        return 1
    print("OK: %d проверок" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
