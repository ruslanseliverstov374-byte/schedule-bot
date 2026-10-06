# -*- coding: utf-8 -*-
"""Чтение текста и таблиц из старых файлов .doc (OLE2, Word 97–2003).

Сторонние библиотеки и внешние программы (antiword, LibreOffice) не нужны:
разбор идёт разбором самого контейнера. Модуль нужен провайдеру КГАСУ —
13 из 20 файлов расписания выложены именно в формате Word 97–2003.

Как устроен файл и что делает модуль
-------------------------------------
1. ``.doc`` — это OLE2-контейнер (Compound File Binary Format): заголовок 512
   байт, таблица размещения FAT, цепочки секторов, каталог из 128-байтных
   записей. Мелкие потоки (< mini_cutoff, обычно 4096 байт) лежат не в обычных
   512-байтных секторах, а в «мини-потоке» внутри корневой записи, нарезанном
   на 64-байтные мини-сектора с собственной таблицей MiniFAT. Всё это
   разбирает :func:`ole_streams`.

2. Внутри потока ``WordDocument`` лежит заголовок FIB, а сам текст — «кусками»
   (piece table): смещения кусков записаны в структуре CLX (PlcPcd) потока
   ``1Table`` или ``0Table`` (какой именно — решает флаг ``fWhichTblStm``,
   бит 0x0200 слова по смещению 0x000A). Адрес CLX — ``fcClx``/``lcbClx``
   по смещению 0x01A2. Каждый кусок — либо 8-битные символы в CP1251, либо
   UTF-16LE; признак — бит 0x40000000 в поле ``fc`` элемента PCD
   (у 8-битного куска смещение в файле вдвое меньше записанного значения).

3. Разделители Word: конец абзаца — 0x0D (``\\r``), конец ячейки таблицы —
   0x07. Концом строки таблицы тоже служит 0x07, поэтому по одному тексту
   отличить конец ячейки от конца строки нельзя. :func:`doc_text` переводит
   0x0D в ``\\n``, а 0x07 — в ``\\t`` (как требует интерфейс провайдера).

Пути получения текста (пробуются по порядку)
--------------------------------------------
* **piece table** — штатный путь для Word 97+; на образце
  ``tests/fixtures/kgasu_26SM31z-_magi_.doc`` работает именно он: CLX содержит
  3 куска, все в UTF-16LE (fc = 2048, 3072, 18944; ccpText = 3083);
* **диапазон fcMin..fcMac** — старый путь Word 6/95, когда таблицы кусков нет:
  8-битный текст читается как CP1251;
* **эвристическое сканирование** потока ``WordDocument`` — запасной режим:
  ищем самые длинные связные последовательности печатных символов отдельно для
  UTF-16LE (оба выравнивания) и для CP1251 и берём вариант, в котором больше
  слов из кириллицы. Нужен, если таблица кусков повреждена.

Эвристика восстановления таблиц (без разбора PAPX/спрмов)
--------------------------------------------------------
Точные границы строк Word хранит в свойствах абзацев (FKP/PAPX, спрм 0xD608 с
числом столбцов), но у файлов КГАСУ эти флаги расставлены непоследовательно,
поэтому строки собираются по тексту:

* текст режется по ``\\n`` (бывшие 0x0D);
* строка, в которой есть хотя бы одна ячейка (``\\t``), считается строкой
  таблицы, а ячейки — это фрагменты между ``\\t``;
* последний пустой фрагмент отбрасывается: он остаётся от знака конца строки
  таблицы (отличить его от пустой ячейки по тексту невозможно);
* непустой абзац без ячеек считается границей таблицы, поэтому расписание и
  подписной блок не слипаются: в образце выходит четыре таблицы — расписание
  (8 строк) и три однострочных блока подписей;
* ячейки обрезаются по краям (``strip``), но их порядок и пустые ячейки
  сохраняются — провайдеру важно расположение (номер пары, время, предмет).

Ограничение: если внутри ячейки несколько абзацев, строка при таком разборе
рвётся на две — так устроен блок «Вторник» в образце (одна ячейка содержит
«ОБЩЕЕ СОБРАНИЕ …» и «Цементобетон …»). Провайдер видит это как две строки
подряд и должен склеивать их по смыслу (продолжение дня — строки без названия
дня в первой ячейке).

Что именно лежит в образце
--------------------------
Разбор файла показывает, что в нём есть «Понедельник», «Вторник», «Среда»,
«Четверг», «Пятница», «Суббота» и код группы «26СМ31з», а вот маркера «Неч» и
времени через двоеточие в самом файле нет: установочная сессия идёт одну
неделю, а время записано через точку («8.00 – 9.30»). Это свойство исходного
файла, а не потеря при разборе: ни в одном потоке контейнера (проверено и как
UTF-16LE, и как CP1251) подстроки «Неч» и шаблона «цифра:две цифры» нет.
Поэтому тест проверяет «Неч» и строгое «8:00» на синтетическом документе,
собранном прямо в ``tests/test_docparse.py``.

Поведение при ошибках
---------------------
Единое правило: любой мусор (не OLE2, обрезанный файл, нет потока
``WordDocument``, нет текста) приводит к :class:`DocError` — наружу не течёт ни
``struct.error``, ни ``IndexError``, ни ``UnicodeDecodeError``.
"""

import os
import re
import struct

__all__ = [
    "DocError",
    "ole_streams",
    "doc_text",
    "doc_tables",
    "doc_text_from_file",
    "doc_tables_from_file",
]

# ------------------------------------------------------------------ константы OLE2

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Служебные ссылки OLE2: 0xFFFFFFFA — последний настоящий номер сектора,
# всё, что больше (FREESECT, ENDOFCHAIN, FATSECT, DIFSECT, NOSTREAM), —
# не сектор, а признак конца цепочки или служебной записи.
_MAXREGSECT = 0xFFFFFFFA

_HEADER_SIZE = 512
_DIR_ENTRY_SIZE = 128
_DIFAT_IN_HEADER = 109          # число записей DIFAT в заголовке
_MIN_SECTOR_SHIFTS = (9, 12)    # 512 и 4096 байт
_MAX_CHAIN = 1 << 22            # предохранитель от зацикливания цепочек

# ---------------------------------------------------------------- константы Word

_WORD_IDENT_97 = 0xA5EC         # wIdent документов Word 97+
_WORD_IDENT_95 = 0xA5DC         # wIdent документов Word 6/95
_FIB_FWHICHTBLSTM = 0x0200      # бит «таблица в 1Table, а не в 0Table»
_FIB_OFF_FLAGS = 0x000A
_FIB_OFF_FCMIN = 0x0018
_FIB_OFF_FCMAC = 0x001C
_FIB_OFF_CCPTEXT = 0x004C
_FIB_OFF_CLX = 0x01A2           # fcClx (4 байта), lcbClx (4 байта)
_FIB_OFF_CLX_END = 0x01AA
_NFIB_WORD97 = 193

_CELL_MARK = "\x07"             # конец ячейки и конец строки таблицы
_PARA_MARK = "\r"               # конец абзаца

# Служебные символы Word, которые в текст не попадают.
_DROP_CHARS = frozenset("\x00\x01\x02\x03\x04\x05\x06\x08\x13\x14\x15\x1f")

# Допустимые символы для запасного сканирования: буквы, цифры, пунктуация.
_PRINTABLE_RANGES = (
    (0x0020, 0x007E),           # латиница и пунктуация
    (0x00A0, 0x00FF),           # Latin-1 (в т.ч. № и неразрывный пробел)
    (0x0401, 0x045F),           # кириллица (без Ё/ё — они ниже)
    (0x2010, 0x2015),           # дефисы и тире
    (0x2018, 0x201F),           # кавычки
    (0x2026, 0x2026),           # многоточие
    (0x2116, 0x2116),           # знак номера
)
_SCAN_CHARS = set(" \t\x07\r\n")
for _low, _high in _PRINTABLE_RANGES:
    _SCAN_CHARS.update(chr(_code) for _code in range(_low, _high + 1))
_SCAN_CHARS.update("\u0401\u0451\u2013\u2014")

_SCAN_MIN_RUN = 25              # минимальная длина «связного» куска текста
_WORD_RE = re.compile(r"[\u0410-\u044f\u0401\u0451]{3,}")


class DocError(Exception):
    """Ошибка чтения .doc: не OLE2, обрезанный файл, нет WordDocument и т.п."""


# ==================================================================== OLE2

def _parse_header(data):
    """Разбирает заголовок OLE2-контейнера."""
    if len(data) < 8 or data[:8] != OLE_SIGNATURE:
        raise DocError("нет подписи OLE2 (D0 CF 11 E0 A1 B1 1A E1) — это не .doc "
                       "или файл повреждён")
    if len(data) < _HEADER_SIZE:
        raise DocError("файл короче 512 байт — заголовок OLE2 обрезан")
    order, sector_shift, mini_shift = struct.unpack_from("<HHH", data, 0x1C)
    if order != 0xFFFE:
        raise DocError("неизвестный порядок байт в заголовке OLE2: %#06x" % order)
    if sector_shift not in _MIN_SECTOR_SHIFTS:
        raise DocError("неизвестный размер сектора OLE2: 1 << %d" % sector_shift)
    sector_size = 1 << sector_shift
    mini_sector_size = 1 << mini_shift
    if mini_sector_size < 8 or mini_sector_size > sector_size:
        raise DocError("неизвестный размер мини-сектора OLE2: 1 << %d" % mini_shift)
    header = {
        "sector_size": sector_size,
        "mini_sector_size": mini_sector_size,
        "fat_count": struct.unpack_from("<I", data, 0x2C)[0],
        "dir_start": struct.unpack_from("<I", data, 0x30)[0],
        "mini_cutoff": struct.unpack_from("<I", data, 0x38)[0],
        "mini_start": struct.unpack_from("<I", data, 0x3C)[0],
        "mini_count": struct.unpack_from("<I", data, 0x40)[0],
        "difat_start": struct.unpack_from("<I", data, 0x44)[0],
        "difat_count": struct.unpack_from("<I", data, 0x48)[0],
    }
    if header["mini_cutoff"] <= 0:
        header["mini_cutoff"] = 4096        # значение по умолчанию из спецификации
    return header


def _reader(data, sector_size):
    """Возвращает функцию «номер сектора -> байты» с проверкой границ файла."""

    def read_sector(number):
        if number > _MAXREGSECT:
            raise DocError("цепочка секторов оборвана (ссылка %#x)" % number)
        offset = _HEADER_SIZE + number * sector_size
        chunk = data[offset:offset + sector_size]
        if len(chunk) < sector_size:
            raise DocError("сектор %d выходит за конец файла — файл обрезан" % number)
        return chunk

    return read_sector


def _chain(start, table, what):
    """Цепочка номеров секторов по таблице размещения (с защитой от циклов)."""
    if start > _MAXREGSECT:
        return []
    numbers = []
    seen = set()
    current = start
    while current <= _MAXREGSECT:
        if current in seen:
            raise DocError("цикл в цепочке секторов (%s), сектор %d" % (what, current))
        seen.add(current)
        if current >= len(table):
            raise DocError("сектор %d вне таблицы размещения (%s)" % (current, what))
        numbers.append(current)
        if len(numbers) > _MAX_CHAIN:
            raise DocError("слишком длинная цепочка секторов (%s)" % what)
        current = table[current]
    return numbers


def _read_chain(read_sector, table, start, what):
    """Собирает содержимое цепочки секторов в байты."""
    return b"".join(read_sector(number) for number in _chain(start, table, what))


def _build_fat(data, header, read_sector):
    """Собирает таблицу FAT: 109 ссылок заголовка плюс цепочка секторов DIFAT."""
    entries_per_sector = header["sector_size"] // 4
    difat = list(struct.unpack_from("<%dI" % _DIFAT_IN_HEADER, data, 0x4C))
    if header["difat_count"]:
        seen = set()
        current = header["difat_start"]
        for _ in range(header["difat_count"]):
            if current > _MAXREGSECT:
                break
            if current in seen:
                raise DocError("цикл в цепочке DIFAT, сектор %d" % current)
            seen.add(current)
            values = struct.unpack_from("<%dI" % entries_per_sector, read_sector(current), 0)
            difat.extend(values[:-1])       # последняя ссылка — следующий сектор DIFAT
            current = values[-1]
    fat_sectors = [number for number in difat if number <= _MAXREGSECT]
    if header["fat_count"] and len(fat_sectors) > header["fat_count"]:
        # В заголовке стоит точное число секторов FAT — лишние ссылки DIFAT
        # (например, оставшиеся от старой версии файла) не читаем.
        fat_sectors = fat_sectors[:header["fat_count"]]
    if not fat_sectors:
        raise DocError("в файле нет таблицы размещения (FAT)")
    fat = []
    for number in fat_sectors:
        fat.extend(struct.unpack_from("<%dI" % entries_per_sector,
                                      read_sector(number), 0))
    return fat


def _directory_entries(directory):
    """Разбирает каталог OLE2 на записи (имя, тип, начало, размер)."""
    entries = []
    for index in range(len(directory) // _DIR_ENTRY_SIZE):
        raw = directory[index * _DIR_ENTRY_SIZE:(index + 1) * _DIR_ENTRY_SIZE]
        entry_type = raw[66]
        if entry_type == 0:                 # пустая запись
            continue
        name_length = struct.unpack_from("<H", raw, 64)[0]
        if 2 <= name_length <= 64:
            name = raw[:name_length - 2].decode("utf-16-le", "replace")
        else:
            name = ""
        entries.append({
            "index": index,
            "name": name,
            "type": entry_type,
            "start": struct.unpack_from("<I", raw, 116)[0],
            "size": struct.unpack_from("<Q", raw, 120)[0],
        })
    return entries


def ole_streams(data: bytes) -> dict[str, bytes]:
    """Потоки OLE2-контейнера: имя -> содержимое.

    Возвращает все потоки файла, включая ``WordDocument``, ``1Table``/``0Table``
    и ``Data`` (у документов Word 97+ есть ещё ``\\x05SummaryInformation``).
    Мелкие потоки читаются из мини-потока через MiniFAT, крупные — из обычных
    512-байтных секторов по FAT.

    :raises DocError: файл не OLE2, обрезан или его структура повреждена.
    """
    header = _parse_header(data)
    sector_size = header["sector_size"]
    read_sector = _reader(data, sector_size)
    fat = _build_fat(data, header, read_sector)

    directory = _read_chain(read_sector, fat, header["dir_start"], "каталог")
    entries = _directory_entries(directory)
    if not entries:
        raise DocError("каталог OLE2 пуст — структура файла повреждена")

    root = None
    for entry in entries:
        if entry["type"] == 5:              # Root Entry
            root = entry
            break
    if root is None:
        raise DocError("в каталоге OLE2 нет корневой записи (Root Entry)")

    # Мини-поток и его таблица размещения (MiniFAT).
    mini_cutoff = header["mini_cutoff"]
    mini_sector_size = header["mini_sector_size"]
    mini_stream = b""
    mini_fat = []
    if root["size"]:
        mini_stream = _read_chain(read_sector, fat, root["start"], "мини-поток")
        mini_stream = mini_stream[:root["size"]]
    if header["mini_count"]:
        raw_mini_fat = _read_chain(read_sector, fat, header["mini_start"], "MiniFAT")
        count = len(raw_mini_fat) // 4
        if count:
            mini_fat = list(struct.unpack_from("<%dI" % count, raw_mini_fat, 0))

    def read_mini(number):
        offset = number * mini_sector_size
        chunk = mini_stream[offset:offset + mini_sector_size]
        if len(chunk) < mini_sector_size:
            raise DocError("мини-сектор %d выходит за конец мини-потока" % number)
        return chunk

    streams = {}
    for entry in entries:
        if entry["type"] != 2 or not entry["name"]:
            continue                        # не поток или запись без имени
        size = entry["size"]
        if size == 0:
            streams[entry["name"]] = b""
            continue
        if size < mini_cutoff:
            if not mini_fat:
                raise DocError("поток %r лежит в мини-потоке, но MiniFAT пуста"
                               % entry["name"])
            content = b"".join(read_mini(number)
                               for number in _chain(entry["start"], mini_fat,
                                                    "поток %r" % entry["name"]))
        else:
            content = _read_chain(read_sector, fat, entry["start"],
                                  "поток %r" % entry["name"])
        if len(content) < size:
            raise DocError("поток %r обрезан: есть %d байт из %d"
                           % (entry["name"], len(content), size))
        streams[entry["name"]] = content[:size]
    if not streams:
        raise DocError("в OLE2-контейнере нет ни одного потока")
    return streams


# ==================================================================== FIB и куски

def _parse_fib(word_document):
    """Читает FIB из потока WordDocument."""
    if len(word_document) < 0x20:
        raise DocError("поток WordDocument короче заголовка FIB — файл обрезан")
    ident, n_fib = struct.unpack_from("<HH", word_document, 0)
    if ident not in (_WORD_IDENT_97, _WORD_IDENT_95):
        raise DocError("неизвестный формат Word (wIdent = %#06x)" % ident)
    flags = struct.unpack_from("<H", word_document, _FIB_OFF_FLAGS)[0]
    fc_min, fc_mac = struct.unpack_from("<II", word_document, _FIB_OFF_FCMIN)
    fib = {
        "ident": ident,
        "n_fib": n_fib,
        "which_table": bool(flags & _FIB_FWHICHTBLSTM),
        "fc_min": fc_min,
        "fc_mac": fc_mac,
        "ccp_text": 0,
        "fc_clx": 0,
        "lcb_clx": 0,
    }
    if len(word_document) >= _FIB_OFF_CCPTEXT + 4:
        fib["ccp_text"] = struct.unpack_from("<I", word_document, _FIB_OFF_CCPTEXT)[0]
    if n_fib >= _NFIB_WORD97 and len(word_document) >= _FIB_OFF_CLX_END:
        fib["fc_clx"], fib["lcb_clx"] = struct.unpack_from("<II", word_document,
                                                           _FIB_OFF_CLX)
    return fib


def _word_streams(streams):
    """Достаёт из потоков WordDocument, поток таблиц и разобранный FIB."""
    word_document = streams.get("WordDocument")
    if word_document is None:
        raise DocError("в OLE2-контейнере нет потока WordDocument — "
                       "это не документ Word")
    fib = _parse_fib(word_document)
    name = "1Table" if fib["which_table"] else "0Table"
    table = streams.get(name)
    if table is None:                       # флаг врёт или потока нет — пробуем второй
        name = "0Table" if name == "1Table" else "1Table"
        table = streams.get(name, b"")
    return word_document, table, fib


def _piece_table(table, fc_clx, lcb_clx):
    """Разбирает CLX (структура PlcPcd) в список кусков текста.

    Возвращает список кортежей ``(cp_начало, cp_конец, смещение, сжат)``:
    ``сжат = True`` — 8-битный текст CP1251, ``False`` — UTF-16LE.
    """
    if lcb_clx <= 0:
        return []
    if fc_clx + lcb_clx > len(table):
        raise DocError("таблица кусков (CLX) выходит за границы потока таблиц")
    clx = table[fc_clx:fc_clx + lcb_clx]
    position = 0
    pcdt = None
    while position < len(clx):
        tag = clx[position]
        if tag == 0x01:                     # Prc (список свойств) — пропускаем
            if position + 3 > len(clx):
                raise DocError("обрыв в CLX: не дочитан блок Prc")
            size = struct.unpack_from("<H", clx, position + 1)[0]
            position += 3 + size
        elif tag == 0x02:                   # Pcdt — таблица кусков
            if position + 5 > len(clx):
                raise DocError("обрыв в CLX: не дочитан блок Pcdt")
            size = struct.unpack_from("<I", clx, position + 1)[0]
            pcdt = clx[position + 5:position + 5 + size]
            break
        else:
            raise DocError("непонятный тег %#04x в CLX — таблица кусков повреждена"
                           % tag)
    if not pcdt or len(pcdt) < 4 + 8:
        return []
    count = (len(pcdt) - 4) // 12           # 4 байта CP + 8 байт PCD на кусок
    if count <= 0:
        return []
    positions = struct.unpack_from("<%dI" % (count + 1), pcdt, 0)
    pieces = []
    base = 4 * (count + 1)
    for index in range(count):
        offset = base + index * 8
        fc = struct.unpack_from("<I", pcdt, offset + 2)[0]
        pieces.append((positions[index], positions[index + 1],
                       fc & 0x3FFFFFFF, bool(fc & 0x40000000)))
    return pieces


def _text_by_pieces(word_document, pieces, limit):
    """Собирает текст из кусков: CP1251 или UTF-16LE.

    У 8-битного куска записанное в PCD смещение вдвое больше настоящего
    (младший бит «отдан» под признак сжатия), поэтому его надо делить на 2.
    """
    parts = []
    for cp_start, cp_end, fc, compressed in pieces:
        if cp_end <= cp_start:
            continue
        if limit:
            if cp_start >= limit:
                break
            if cp_end > limit:
                cp_end = limit
        count = cp_end - cp_start
        if compressed:
            offset = fc // 2
            raw = word_document[offset:offset + count]
            if len(raw) < count:
                raise DocError("кусок текста (CP1251) выходит за конец потока")
            parts.append(raw.decode("cp1251", "replace"))
        else:
            raw = word_document[fc:fc + count * 2]
            if len(raw) < count * 2:
                raise DocError("кусок текста (UTF-16) выходит за конец потока")
            parts.append(raw.decode("utf-16-le", "replace"))
    return "".join(parts)


def _text_by_range(word_document, fib):
    """Старый путь Word 6/95: текст лежит подряд между fcMin и fcMac."""
    fc_min, fc_mac = fib["fc_min"], fib["fc_mac"]
    if not (0 < fc_min < fc_mac <= len(word_document)):
        raise DocError("диапазон fcMin..fcMac (%d..%d) не похож на текст"
                       % (fc_min, fc_mac))
    return word_document[fc_min:fc_mac].decode("cp1251", "replace")


# ============================================================ запасная эвристика

def _scan_runs(units):
    """Склеивает подряд идущие «печатные» единицы в куски текста."""
    runs = []
    current = []
    for unit in units:
        if unit in _SCAN_CHARS:
            current.append(unit)
        else:
            if len(current) >= _SCAN_MIN_RUN:
                runs.append("".join(current))
            current = []
    if len(current) >= _SCAN_MIN_RUN:
        runs.append("".join(current))
    return _PARA_MARK.join(runs)


def _scan_utf16(word_document, offset):
    """Ищет текст в UTF-16LE, начиная с чётного или нечётного байта."""
    units = []
    for position in range(offset, len(word_document) - 1, 2):
        try:
            units.append(word_document[position:position + 2].decode("utf-16-le"))
        except UnicodeDecodeError:
            units.append("\x00")
    return _scan_runs(units)


def _scan_cp1251(word_document):
    """Ищет текст в CP1251 (однобайтовая кодировка Word 6/95).

    Байты собираются как символы Latin-1 (так их видит :func:`_scan_runs`),
    а найденные куски затем переводятся в CP1251.
    """
    units = []
    for byte in word_document:
        if 0x20 <= byte <= 0xFF:
            units.append(chr(byte))         # Latin-1: обратно в байт без потерь
        elif byte == 0x07:
            units.append(_CELL_MARK)
        elif byte == 0x0D:
            units.append(_PARA_MARK)
        elif byte == 0x09:
            units.append("\t")
        else:
            units.append("\x00")
    found = _scan_runs(units)
    return found.encode("latin-1", "replace").decode("cp1251", "replace")


def _word_score(text):
    """Оценка «похоже на русский текст»: сколько букв в словах длиной 3+."""
    return sum(len(word) for word in _WORD_RE.findall(text))


def _fallback_text(word_document):
    """Запасной режим: сканирование потока WordDocument.

    Пробуются UTF-16LE (оба выравнивания) и CP1251; побеждает вариант, в
    котором больше связных кириллических слов. Такой текст содержит настоящие
    разделители Word (0x0D и 0x07), поэтому дальше он обрабатывается как обычно.
    """
    candidates = [_scan_utf16(word_document, 0), _scan_utf16(word_document, 1),
                  _scan_cp1251(word_document)]
    return max(candidates, key=_word_score)


# ================================================================ сборка текста

def _is_readable(text):
    """Похож ли результат на настоящий текст (а не на мусор из байтов)."""
    if len(text) < 8:
        return False
    probe = text[:4000]
    bad = 0
    for char in probe:
        if char == "\ufffd":
            bad += 1
        elif ord(char) < 0x20 and char not in "\r\n\t\x07\x0b\x0c":
            bad += 1
    return bad <= max(2, len(probe) // 100)


def _raw_text(data):
    """Сырой текст документа и название сработавшего пути.

    Пути пробуются по порядку: таблица кусков, диапазон fcMin..fcMac,
    эвристическое сканирование. Возвращает ``(текст, путь)``.
    """
    streams = ole_streams(data)
    word_document, table, fib = _word_streams(streams)
    problems = []

    try:
        pieces = _piece_table(table, fib["fc_clx"], fib["lcb_clx"])
        if pieces:
            limit = fib["ccp_text"] or None
            text = _text_by_pieces(word_document, pieces, limit)
            if _is_readable(text):
                return text, "таблица кусков (CLX/PlcPcd), кусков: %d" % len(pieces)
            problems.append("текст из таблицы кусков не похож на настоящий")
        else:
            problems.append("в FIB нет таблицы кусков (CLX)")
    except DocError as error:
        problems.append(str(error))

    try:
        text = _text_by_range(word_document, fib)
        if _is_readable(text):
            return text, "диапазон fcMin..fcMac (Word 6/95, CP1251)"
        problems.append("диапазон fcMin..fcMac не похож на текст")
    except DocError as error:
        problems.append(str(error))

    text = _fallback_text(word_document)
    if _is_readable(text):
        return text, "эвристическое сканирование потока WordDocument"
    problems.append("эвристическое сканирование не нашло связного текста")
    raise DocError("не удалось извлечь текст из .doc: " + "; ".join(problems))


def _normalize(raw):
    """Сырой текст Word -> текст интерфейса: абзац ``\\n``, ячейка ``\\t``.

    Дополнительно: инструкции полей (например, ``HYPERLINK "..."``) и служебные
    символы (рисунки, сноски, мягкие переносы) выбрасываются, неразрывный дефис
    превращается в обычный, разрыв строки/страницы — в конец абзаца.
    """
    output = []
    field_starts = []
    for char in raw:
        code = ord(char)
        if code == 0x0D:                    # конец абзаца
            output.append("\n")
        elif code == 0x07:                  # конец ячейки и конец строки таблицы
            output.append("\t")
        elif code in (0x0B, 0x0C):          # мягкий разрыв строки и страницы
            output.append("\n")
        elif code == 0x13:                  # начало инструкции поля
            field_starts.append(len(output))
        elif code == 0x14:                  # конец инструкции — её выбрасываем
            if field_starts:
                del output[field_starts.pop():]
        elif code in _DROP_CHARS:           # \x15 и прочая служебная мелочь
            continue
        elif code == 0x1E:                  # неразрывный дефис
            output.append("-")
        elif code < 0x20 and code not in (0x09, 0x0A):
            continue
        else:
            output.append(char)
    return "".join(output)


def doc_text(data: bytes) -> str:
    """Текст документа с разделителями: конец абзаца — ``\\n``, ячейка — ``\\t``.

    Текст берётся штатным путём Word 97+ (таблица кусков CLX/PlcPcd из потока
    ``1Table``/``0Table``); если он не сработал — из диапазона fcMin..fcMac, а
    затем эвристическим сканированием потока ``WordDocument``. На образце
    ``tests/fixtures/kgasu_26SM31z-_magi_.doc`` используется таблица кусков
    (3 куска, UTF-16LE).

    :raises DocError: файл не OLE2, обрезан, без потока WordDocument или текст
        извлечь не удалось.
    """
    return _normalize(_raw_text(data)[0])


def doc_tables(data: bytes) -> list[list[list[str]]]:
    """Таблицы из .doc: ``[таблица][строка][ячейка]``.

    Строкой таблицы считается абзац, в котором есть хотя бы одна ячейка
    (``\\t``); ячейки — фрагменты между ``\\t``, последний пустой фрагмент
    (след знака конца строки) отбрасывается. Непустой абзац без ячеек закрывает
    текущую таблицу, поэтому расписание и подписной блок дают две таблицы.

    Если ячеек нет ни в одной строке, возвращается пустой список — провайдер
    тогда разбирает текст из :func:`doc_text` сам. Так же ведёт себя модуль,
    когда структура документа восстановлению не поддаётся.

    :raises DocError: то же, что у :func:`doc_text`.
    """
    text = _normalize(_raw_text(data)[0])
    tables = []
    current = []
    for line in text.split("\n"):
        if "\t" in line:
            cells = [cell.strip() for cell in line.split("\t")]
            if cells and not cells[-1]:
                cells.pop()                 # след знака конца строки таблицы
            if cells:
                current.append(cells)
            continue
        if line.strip():                    # непустой абзац без ячеек — граница
            if current:
                tables.append(current)
                current = []
    if current:
        tables.append(current)
    if not any(len(row) >= 2 for table in tables for row in table):
        return []                           # ни одной строки с ячейками не нашлось
    return tables


def _read_file(path):
    """Читает файл целиком, превращая ошибки ввода-вывода в DocError."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise DocError("не удалось прочитать файл %s: %s"
                       % (os.fspath(path), error)) from error


def doc_text_from_file(path) -> str:
    """Текст .doc из файла (см. :func:`doc_text`)."""
    return doc_text(_read_file(path))


def doc_tables_from_file(path) -> list[list[list[str]]]:
    """Таблицы .doc из файла (см. :func:`doc_tables`)."""
    return doc_tables(_read_file(path))
