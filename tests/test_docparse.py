# -*- coding: utf-8 -*-
"""Самопроверка чтения старых .doc (schedule/docparse.py).

Запуск из корня проекта::

    python tests/test_docparse.py

Тест офлайн, только локальные данные:

* образец ``tests/fixtures/kgasu_26SM31z-_magi_.doc`` (94 720 байт, OLE2/Word 97);
* два синтетических документа, которые собираются здесь же из ``struct`` —
  они проверяют то, чего в образце нет: 8-битный кусок текста в CP1251,
  отдельный маркер недели «Неч», время через двоеточие и старый путь
  Word 6/95 (текст подряд между fcMin и fcMac).

Итог печатается строкой ``OK: N проверок``, код возврата 0 — всё прошло,
1 — есть упавшие проверки.

Ожидания по образцу (проверено разбором файла):

* потоки ``WordDocument`` (51 252 байта) и ``1Table`` (12 340 байт);
* текст берётся штатным путём — таблица кусков CLX/PlcPcd, 3 куска, все UTF-16LE;
* время в расписании записано через точку («8.00 – 9.30»), а не через двоеточие,
  поэтому регулярка времени допускает оба разделителя; строгое ``8:00``
  проверяется на синтетическом документе;
* слова «Неч» в образце нет (установочная сессия идёт одну неделю), поэтому
  поддержка «Неч» тоже проверяется на синтетическом документе.
"""

import os
import re
import struct
import sys
import time

try:  # чтобы русские подписи не ломали вывод в старой консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from schedule.docparse import (                          # noqa: E402
    DocError,
    doc_tables,
    doc_tables_from_file,
    doc_text,
    doc_text_from_file,
    ole_streams,
)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "kgasu_26SM31z-_magi_.doc")

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]

# Время в образце записано через точку («8.00 – 9.30»), а в новых файлах КГАСУ —
# через двоеточие, поэтому мягкая регулярка допускает оба разделителя.
TIME_ANY = re.compile(r"\d{1,2}[:.]\d{2}")
TIME_COLON = re.compile(r"\d{1,2}:\d{2}")

CASES = []


def check(title, condition, detail=""):
    """Записывает результат одной проверки."""
    CASES.append((title, bool(condition), detail))
    return bool(condition)


# ------------------------------------------------------- синтетические .doc

SECTOR = 512
_DIR_ENTRY = 128
_END_OF_CHAIN = 0xFFFFFFFE
_FREE_SECTOR = 0xFFFFFFFF
_FAT_SECTOR = 0xFFFFFFFD
_NO_STREAM = 0xFFFFFFFF
_MINI_CUTOFF = 64          # маленькое значение: все потоки теста лежат в FAT


def _directory_entry(name, entry_type, start, size, child=_NO_STREAM,
                     right=_NO_STREAM):
    """Одна 128-байтная запись каталога OLE2."""
    raw = bytearray(_DIR_ENTRY)
    encoded = name.encode("utf-16-le") + b"\x00\x00"
    raw[0:len(encoded)] = encoded
    struct.pack_into("<H", raw, 64, len(encoded))
    raw[66] = entry_type            # 5 — корень, 2 — поток
    raw[67] = 1                     # цвет узла: чёрный
    struct.pack_into("<III", raw, 68, _NO_STREAM, right, child)
    struct.pack_into("<I", raw, 116, start)
    struct.pack_into("<Q", raw, 120, size)
    return bytes(raw)


def make_ole(streams):
    """Собирает минимальный корректный OLE2-контейнер из словаря потоков.

    Сектора по 512 байт, обычная FAT, мини-потока нет (порог мини-потока в
    заголовке равен 64, поэтому все потоки читаются по FAT). Записи каталога
    связаны простой цепочкой «правых» siblings — этого достаточно, чтобы
    контейнер был целым; разбор потока каталога идёт по всем записям подряд.
    """
    names = sorted(streams, key=lambda name: (len(name), name.upper()))
    contents = {}
    for name in names:
        data = streams[name]
        padded = (len(data) + SECTOR - 1) // SECTOR * SECTOR
        contents[name] = data + b"\x00" * (padded - len(data))

    stream_sectors = sum(len(contents[name]) // SECTOR for name in names)
    dir_sectors = ((len(names) + 1) * _DIR_ENTRY + SECTOR - 1) // SECTOR
    fat_sectors = 1
    while True:                     # число секторов FAT зависит от их же числа
        entries = stream_sectors + dir_sectors + fat_sectors
        needed = (entries + SECTOR // 4 - 1) // (SECTOR // 4)
        if needed <= fat_sectors:
            break
        fat_sectors = needed

    chains = {}
    next_sector = 0
    for name in names:
        count = len(contents[name]) // SECTOR
        chains[name] = list(range(next_sector, next_sector + count))
        next_sector += count
    dir_start = next_sector
    next_sector += dir_sectors
    fat_numbers = list(range(next_sector, next_sector + fat_sectors))
    total_sectors = next_sector + fat_sectors

    fat = [_FREE_SECTOR] * (fat_sectors * (SECTOR // 4))
    for name in names:
        chain = chains[name]
        for position, number in enumerate(chain):
            fat[number] = (chain[position + 1] if position + 1 < len(chain)
                           else _END_OF_CHAIN)
    for number in range(dir_start, dir_start + dir_sectors):
        fat[number] = (number + 1 if number + 1 < dir_start + dir_sectors
                       else _END_OF_CHAIN)
    for number in fat_numbers:
        fat[number] = _FAT_SECTOR

    entries = [_directory_entry("Root Entry", 5, _END_OF_CHAIN, 0,
                                child=1 if names else _NO_STREAM)]
    for index, name in enumerate(names):
        right = index + 2 if index + 1 < len(names) else _NO_STREAM
        entries.append(_directory_entry(name, 2, chains[name][0],
                                        len(streams[name]), right=right))
    directory = b"".join(entries)
    directory += b"\x00" * (dir_sectors * SECTOR - len(directory))

    header = bytearray(SECTOR)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 0x18, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 0x2C, fat_sectors)
    struct.pack_into("<I", header, 0x30, dir_start)
    struct.pack_into("<I", header, 0x38, _MINI_CUTOFF)
    struct.pack_into("<I", header, 0x3C, _END_OF_CHAIN)      # MiniFAT отсутствует
    struct.pack_into("<I", header, 0x40, 0)
    struct.pack_into("<I", header, 0x44, _END_OF_CHAIN)      # DIFAT в заголовке
    struct.pack_into("<I", header, 0x48, 0)
    difat = [_FREE_SECTOR] * 109
    for position, number in enumerate(fat_numbers[:109]):
        difat[position] = number
    struct.pack_into("<109I", header, 0x4C, *difat)

    chunks = [bytes(header)]
    for name in names:
        chunks.append(contents[name])
    chunks.append(directory)
    raw_fat = b"".join(struct.pack("<I", value) for value in fat)
    raw_fat += b"\x00" * (fat_sectors * SECTOR - len(raw_fat))
    chunks.append(raw_fat)
    payload = b"".join(chunks)
    # 512-байтный заголовок идёт до сектора 0, поэтому он не входит в total_sectors
    assert len(payload) == SECTOR * (total_sectors + 1), \
        "сборка OLE2 разошлась в секторах"
    return payload


def build_word97(pieces, table_name="1Table"):
    """Пара потоков (WordDocument, 1Table или 0Table) для Word 97.

    ``pieces`` — список ``(текст, сжат)``: куски идут подряд по CP; сжатые
    кодируются в CP1251 (как в Word), несжатые — в UTF-16LE. Имя потока таблиц
    задаёт флаг fWhichTblStm в FIB (бит 0x0200 — это 1Table).
    """
    word = bytearray(4096)
    table = bytearray(SECTOR)
    placed = []
    offset = 1024
    cp = 0
    for text, compressed in pieces:
        blob = text.encode("cp1251" if compressed else "utf-16-le")
        offset = (offset + 255) // 256 * 256       # выравнивание как в Word
        if offset + len(blob) > len(word):
            word.extend(b"\x00" * (offset + len(blob) + SECTOR - len(word)))
        word[offset:offset + len(blob)] = blob
        stored = (offset * 2 | 0x40000000) if compressed else offset
        placed.append((cp, cp + len(text), stored))
        cp += len(text)
        offset += len(blob)

    positions = [0] + [end for _, end, _ in placed]
    plc = struct.pack("<%dI" % len(positions), *positions)
    for _, _, stored in placed:
        plc += struct.pack("<HIH", 0, stored, 0)   # флаги, fc, prm
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    table[0:len(clx)] = clx

    struct.pack_into("<H", word, 0x00, 0xA5EC)     # wIdent: Word 97
    struct.pack_into("<H", word, 0x02, 193)        # nFib: Word 97
    struct.pack_into("<H", word, 0x0A,            # fWhichTblStm -> 1Table/0Table
                     0x0200 if table_name == "1Table" else 0x0000)
    struct.pack_into("<II", word, 0x18, 1024, offset)   # fcMin / fcMac
    struct.pack_into("<I", word, 0x4C, cp)         # ccpText
    struct.pack_into("<II", word, 0x1A2, 0, len(clx))   # fcClx / lcbClx
    return bytes(word), bytes(table)


def build_word95(text):
    """Поток WordDocument старого формата: текст CP1251 подряд, без CLX."""
    blob = text.encode("cp1251")
    word = bytearray(max(4096, 1024 + len(blob) + SECTOR))
    struct.pack_into("<H", word, 0x00, 0xA5DC)     # wIdent: Word 6/95
    struct.pack_into("<H", word, 0x02, 101)        # nFib < 193: таблицы кусков нет
    struct.pack_into("<II", word, 0x18, 1024, 1024 + len(blob))
    struct.pack_into("<I", word, 0x4C, len(text))
    word[1024:1024 + len(blob)] = blob
    return bytes(word)


SYNTH_HEAD = "Дни\x07Часы\x07\r"
SYNTH_DAYS = ("Понедельник\x071\x078:00\x07Неч\x07\r"
              "Вторник\x072\x079:40\x07Чет\x07\r")
SYNTH_EXPECTED = ("Дни\tЧасы\t\n"
                  "Понедельник\t1\t8:00\tНеч\t\n"
                  "Вторник\t2\t9:40\tЧет\t\n")
OLD_EXPECTED = "Дни\tЧасы\t\nПонедельник\t1\t8:00\tНеч\t\n"


# ------------------------------------------------------------------- проверки

def check_container(data):
    """Проверки контейнера OLE2 (ole_streams)."""
    streams = ole_streams(data)
    word = streams.get("WordDocument")
    table = streams.get("1Table") or streams.get("0Table")

    check("ole_streams: найден поток WordDocument", word is not None)
    check("ole_streams: размер WordDocument больше нуля",
          bool(word) and len(word) > 0, len(word or b""))
    check("ole_streams: найден поток 1Table (или 0Table)",
          table is not None, sorted(streams))
    check("ole_streams: размер потока таблиц больше нуля",
          bool(table) and len(table) > 0, len(table or b""))
    check("ole_streams: WordDocument содержит реальные данные (> 50 000 байт)",
          len(word or b"") > 50000, len(word or b""))
    check("ole_streams: найден поток Data", "Data" in streams
          and len(streams["Data"]) > 0)
    small = [name for name, blob in streams.items() if 0 < len(blob) < 4096]
    check("ole_streams: мини-поток (MiniFAT) читается — есть мелкие потоки",
          len(small) >= 2, small)
    return streams


def check_text(data, streams):
    """Проверки текста (doc_text)."""
    started = time.perf_counter()
    text = doc_text(data)
    tables = doc_tables(data)
    elapsed = time.perf_counter() - started

    check("doc_text: текст непустой и длиннее 300 символов",
          len(text) > 300, len(text))
    check("doc_text: есть «Понедельник»", "Понедельник" in text)
    check("doc_text: есть «Вторник» — ещё один день недели", "Вторник" in text)
    missing = [day for day in WEEKDAYS if day not in text]
    check("doc_text: найдены все шесть учебных дней недели",
          not missing, "нет: %s" % missing)
    check("doc_text: есть код группы «26СМ31з»", "26СМ31з" in text)
    check("doc_text: есть «Чет» (в образце — внутри слова «Четверг»)"
          ", маркер второй недели", "Чет" in text)
    check("doc_text: отдельного маркера «Неч» в образце нет (сессия одной недели)"
          " — он проверяется на синтетическом .doc ниже", "Неч" not in text)
    times = TIME_ANY.findall(text)
    check("doc_text: найдено время вида 8:00 (в образце записано «8.00 – 9.30»)",
          bool(times), times[:5])
    check("doc_text: разделители нормализованы — нет \\r и \\x07",
          "\r" not in text and "\x07" not in text)
    check("doc_text: нет символов-заменителей (текст раскодирован верно)",
          "\ufffd" not in text, text.count("\ufffd"))
    check("doc_text: абзацы и ячейки размечены (\\n и \\t)",
          "\n" in text and "\t" in text,
          "абзацев: %d, ячеек: %d" % (text.count("\n"), text.count("\t")))
    check("doc_text: инструкция поля HYPERLINK выброшена, её результат остался",
          "HYPERLINK" not in text and "Проектирование" in text)
    check("doc_text: повторный вызов даёт тот же результат", doc_text(data) == text)
    check("doc_text_from_file: совпадает с doc_text по байтам",
          doc_text_from_file(FIXTURE) == text)
    check("время разбора образца (текст + таблицы) меньше 5 секунд",
          elapsed < 5.0, "%.3f с" % elapsed)
    print("   разбор образца занял %.1f мс, символов в тексте: %d"
          % (elapsed * 1000, len(text)))
    print("   путь получения текста: %s" % _text_source(data))
    return text, tables, elapsed


def _text_source(data):
    """Служебная печать: каким путём модуль получил текст (белый ящик)."""
    from schedule import docparse
    return docparse._raw_text(data)[1]


def check_tables(data, tables):
    """Проверки таблиц (doc_tables)."""
    check("doc_tables: список таблиц получен (для образца — непустой)",
          bool(tables), "таблиц: %d" % len(tables))
    rows = [row for table in tables for row in table]
    wide = [row for row in rows if len(row) >= 3]
    check("doc_tables: есть строка минимум с 3 ячейками", bool(wide),
          [len(row) for row in rows][:8])
    check("doc_tables: есть ячейка ровно «Понедельник»",
          any(cell == "Понедельник" for row in rows for cell in row))
    check("doc_tables: в строке с «Понедельник» есть ещё ячейки со временем",
          any("Понедельник" in row and any(TIME_ANY.search(cell) for cell in row)
              for row in rows))
    check("doc_tables: ячейки без разделителей внутри (\\t и \\n)",
          all("\t" not in cell and "\n" not in cell for row in rows for cell in row))
    check("doc_tables: пустых таблиц нет", all(table for table in tables))
    check("doc_tables: повторный вызов даёт тот же результат",
          doc_tables(data) == tables)
    check("doc_tables_from_file: совпадает с doc_tables по байтам",
          doc_tables_from_file(FIXTURE) == tables)
    if rows:
        print("   таблиц: %d, строк: %d, самая широкая строка: %d ячеек"
              % (len(tables), len(rows), max(len(row) for row in rows)))


def check_synthetic():
    """Синтетический Word 97: 8-битный кусок CP1251, «Неч», строгое 8:00."""
    word, table = build_word97([(SYNTH_HEAD, True), (SYNTH_DAYS, False)])
    data = make_ole({"WordDocument": word, "1Table": table})
    streams = ole_streams(data)
    check("синтетический .doc: контейнер собирается и читается",
          set(streams) == {"WordDocument", "1Table"}, sorted(streams))

    text = doc_text(data)
    check("синтетический .doc: текст собран из кусков без потерь",
          text == SYNTH_EXPECTED, repr(text))
    check("синтетический .doc: 8-битный кусок прочитан как CP1251 "
          "(«Дни», «Часы»)", text.startswith("Дни\tЧасы"))
    check("синтетический .doc: есть «Неч» — маркер нечётной недели",
          "Неч" in text)
    check("синтетический .doc: строгая регулярка 8:00 находит время",
          bool(TIME_COLON.search(text)), TIME_COLON.findall(text))
    check("синтетический .doc: есть «Понедельник» и «Чет»",
          "Понедельник" in text and "Чет" in text)

    tables = doc_tables(data)
    rows = [row for item in tables for row in item]
    check("синтетический .doc: таблица восстановлена (3 строки)",
          len(tables) == 1 and len(tables[0]) == 3,
          [len(row) for row in rows])
    check("синтетический .doc: строка с «Понедельник» содержит «1», «8:00», «Неч»",
          ["Понедельник", "1", "8:00", "Неч"] in rows, rows)

    # Тот же документ, но таблица кусков во втором потоке таблиц (0Table):
    # реальные файлы КГАСУ встречаются с обоими значениями флага fWhichTblStm.
    other_word, other_table = build_word97([(SYNTH_HEAD, True), (SYNTH_DAYS, False)],
                                           table_name="0Table")
    other = make_ole({"WordDocument": other_word, "0Table": other_table})
    check("синтетический .doc: вариант с потоком 0Table читается так же",
          doc_text(other) == SYNTH_EXPECTED and doc_tables(other) == tables)
    return text, tables


def check_no_tables():
    """Документ без ячеек: doc_tables обязан вернуть пустой список."""
    data = make_ole({"WordDocument": build_word95(
        "КАЗАНСКИЙ ГОСУДАРСТВЕННЫЙ АРХИТЕКТУРНО-СТРОИТЕЛЬНЫЙ УНИВЕРСИТЕТ\r"
        "Расписание занятий вывешено на стенде.\r")})
    text = doc_text(data)
    tables = doc_tables(data)
    check("документ без ячеек: doc_text читается, doc_tables возвращает []",
          "УНИВЕРСИТЕТ" in text and tables == [], tables)


def check_old_format():
    """Старый путь Word 6/95: текст CP1251 подряд между fcMin и fcMac."""
    data = make_ole({"WordDocument": build_word95(
        "Дни\x07Часы\x07\rПонедельник\x071\x078:00\x07Неч\x07\r")})
    text = doc_text(data)
    check("Word 6/95: текст читается из диапазона fcMin..fcMac (CP1251)",
          text == OLD_EXPECTED, repr(text))
    check("Word 6/95: в тексте есть «Понедельник» и «Неч»",
          "Понедельник" in text and "Неч" in text)
    rows = [row for table in doc_tables(data) for row in table]
    check("Word 6/95: таблица восстанавливается из того же текста",
          ["Понедельник", "1", "8:00", "Неч"] in rows, rows)


def check_errors(data, streams):
    """Мусор и битые файлы: единое поведение — DocError."""
    from schedule import docparse

    for title, blob in (("пустые байты", b""),
                        ("строка «not a doc»", b"not a doc"),
                        ("обрезанный образец (первые 500 байт)", data[:500]),
                        ("обрезанный образец (первые 600 байт — заголовок цел)",
                         data[:600])):
        try:
            doc_text(blob)
            raised = False
        except DocError:
            raised = True
        check("мусор (%s): doc_text поднимает DocError" % title, raised)

    try:
        doc_tables(b"not a doc")
        raised = False
    except DocError:
        raised = True
    check("мусор: doc_tables поднимает DocError так же, как doc_text", raised)

    without_word = make_ole({"Bogus": b"x" * 100, "1Table": b"y" * 100})
    try:
        doc_text(without_word)
        raised = False
    except DocError as error:
        raised = "WordDocument" in str(error)
    check("OLE2 без потока WordDocument: DocError с понятным текстом", raised)

    try:
        doc_text_from_file(os.path.join(ROOT, "tests", "fixtures", "нет-такого.doc"))
        raised = False
    except DocError:
        raised = True
    check("несуществующий файл: doc_text_from_file поднимает DocError", raised)

    fallback = docparse._fallback_text(streams["WordDocument"])
    check("запасной путь (сканирование WordDocument) тоже находит «Понедельник»",
          "Понедельник" in fallback, len(fallback))


# ---------------------------------------------------------------------- запуск

def main():
    if not os.path.exists(FIXTURE):
        print("ПРОВАЛ: нет файла образца %s" % FIXTURE)
        return 1
    with open(FIXTURE, "rb") as handle:
        data = handle.read()

    print("Образец: %s (%d байт)" % (FIXTURE, len(data)))

    print("\n1. Контейнер OLE2")
    check("образец начинается с подписи OLE2 и не обрезан",
          data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" and len(data) > 512)
    streams = check_container(data)
    check("ole_streams: повторный вызов даёт те же имена и размеры",
          ole_streams(data) == streams)

    print("\n2. Текст документа")
    text, tables, _elapsed = check_text(data, streams)
    first_lines = [line for line in text.split("\n") if line.strip()][:5]
    print("   первые 5 непустых строк текста:")
    for line in first_lines:
        print("     %s" % (line.strip()[:100],))

    print("\n3. Таблицы")
    check_tables(data, tables)

    print("\n4. Синтетический Word 97 (CP1251 + UTF-16LE)")
    check_synthetic()

    print("\n5. Синтетический Word 6/95 и документ без таблиц")
    check_old_format()
    check_no_tables()

    print("\n6. Мусор и битые файлы")
    check_errors(data, streams)

    failed = 0
    print()
    for number, (title, ok, detail) in enumerate(CASES, 1):
        print("%2d. [%s] %s" % (number, "OK" if ok else "FAIL", title))
        if not ok:
            failed += 1
            if detail != "":
                print("      подробности: %r" % (detail,))
    print("-" * 60)
    if failed:
        print("ПРОВАЛ: %d из %d проверок не пройдено" % (failed, len(CASES)))
        return 1
    print("OK: %d проверок" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
