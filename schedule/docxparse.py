# -*- coding: utf-8 -*-
"""Разбор расписания КГАСУ из файлов .docx (Word) на стандартной библиотеке.

Файл .docx — это обычный zip-архив, внутри которого лежит ``word/document.xml``
с текстом документа, а таблицы расписания описаны элементами WordprocessingML:
``w:tbl`` — таблица, ``w:tr`` — строка, ``w:tc`` — ячейка, ``w:p`` — абзац,
``w:t`` — кусок текста.

Объединённые ячейки Word помечает так:

* ``<w:gridSpan w:val="N"/>`` — ячейка растянута на N столбцов по горизонтали;
* ``<w:vMerge w:val="restart"/>`` — начало вертикального объединения;
* ``<w:vMerge/>`` (или ``w:val="continue"``) — продолжение объединения, текст
  в такой ячейке не повторяется, значение лежит в первой строке объединения.

Публичный интерфейс (его ждёт провайдер КГАСУ)::

    docx_tables(data)           -> [таблица][строка][ячейка]
    docx_text(data)             -> весь текст документа по порядку
    docx_tables_from_file(path) -> то же, что docx_tables, но из файла

Ошибки чтения (не zip, нет ``word/document.xml``, битый XML, нет файла)
поднимаются одним исключением :class:`DocxError`; корректный .docx без таблиц
даёт пустой список.

Как разворачиваются объединения (длина каждой строки равна ширине таблицы):

* ``gridSpan`` — текст ячейки повторяется столько раз, сколько столбцов она
  занимает: занятие, выданное на всю группу, попадает в обе подгруппы;
* ``vMerge`` — ячейки-продолжения остаются пустыми, как в самом документе.

ВНИМАНИЕ (важно для провайдера): из-за вертикальных объединений у каждой пары
две строки — «Чет» и «Неч», — и во второй строке пустыми оказываются не только
«Дата», но и «Номер» и «Время»: эти ячейки объединены с первой строкой пары.
Более того, «Чет»/«Неч» иногда растянуто на несколько строк (так, у пары 5 в
понедельник «Чет» занимает две строки), поэтому строки не обязаны строго
чередоваться. Надёжный приём — протянуть последнее непустое значение сверху
вниз по первым четырём столбцам (Дата, Номер, Неделя, Время); столбцы занятий
трогать нельзя, иначе занятие скопируется в чужую строку::

    last = {}
    for row in main:
        for i in range(4):                 # Дата, Номер, Неделя, Время
            if row[i]:
                last[i] = row[i]
            else:
                row[i] = last.get(i, "")   # «Неч» получает номер и время от «Чет»

Используются только модули стандартной библиотеки: ``zipfile``,
``xml.etree.ElementTree``, ``re``, ``os`` и ``io.BytesIO`` (чтобы открыть zip
прямо из байтов, без временного файла).

Пример::

    from schedule.docxparse import docx_tables, DocxError

    tables = docx_tables(open("26РП01_02.docx", "rb").read())
    main = tables[1]                       # 71 строка, 8 столбцов
    header = main[0]                       # Дата, Номер, Неделя, Время, 26РП01…

Столбцы главной таблицы: 0 — «Дата», 1 — «Номер», 2 — «Неделя», 3 — «Время»,
4 и 5 — «26РП01» (подгруппа 2 и подгруппа 1), 6 и 7 — «26РП02» (подгруппа 1 и
подгруппа 2). После разворота ``gridSpan`` эта нумерация одинакова у всех 71
строки, поэтому столбцы занятий можно смело брать как ``row[4:]``.
"""

import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

__all__ = ["DocxError", "docx_tables", "docx_text", "docx_tables_from_file"]

# --------------------------------------------------------------- пространство имён

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = "{%s}" % _W_NS

#: путь к основному XML документа внутри zip-архива
DOCUMENT_XML = "word/document.xml"

_W_P = _W + "p"
_W_T = _W + "t"
_W_TAB = _W + "tab"
_W_BR = _W + "br"
_W_CR = _W + "cr"
_W_NO_BREAK_HYPHEN = _W + "noBreakHyphen"
_W_TBL = _W + "tbl"
_W_TR = _W + "tr"
_W_TC = _W + "tc"
_W_TCPR = _W + "tcPr"
_W_TRPR = _W + "trPr"
_W_TBL_GRID = _W + "tblGrid"
_W_GRID_COL = _W + "gridCol"
_W_GRID_SPAN = _W + "gridSpan"
_W_GRID_BEFORE = _W + "gridBefore"
_W_GRID_AFTER = _W + "gridAfter"
_W_SDT = _W + "sdt"
_W_SDT_CONTENT = _W + "sdtContent"
_W_VAL = _W + "val"

#: то, что в текст не попадает: удалённый при правках текст и коды полей
_SKIP_TAGS = frozenset((_W + "del", _W + "instrText", _W + "delText"))

#: обёртки «контент-контролов», сквозь которые видно обычное содержимое
_SDT_WRAPPERS = (_W_SDT, _W_SDT_CONTENT)

#: предел для gridSpan — защита от битого или враждебного значения
_MAX_SPAN = 1000

#: подряд идущие пробелы (в том числе неразрывные) сжимаются в один
_MULTISPACE = re.compile(r"[ \t\f\v\u00a0]+")


class DocxError(Exception):
    """Ошибка чтения .docx: не zip, нет word/document.xml, битый XML, нет файла."""


# ------------------------------------------------------------------- мелкие утилиты

def _local_name(tag):
    """Локальное имя тега без пространства имён: ``{ns}tbl`` -> ``tbl``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(element, tag):
    """Дочерние элементы с нужным тегом; содержимое w:sdt раскрывается.

    Обычные ячейки, строки и абзацы лежат прямо в родителе, но Word умеет
    заворачивать их в контент-контрол ``w:sdt`` — такие обёртки прозрачны.
    """
    found = []
    for child in element:
        if child.tag == tag:
            found.append(child)
        elif child.tag in _SDT_WRAPPERS:
            found.extend(_children(child, tag))
    return found


def _int_attr(element, default=0):
    """Целое значение атрибута ``w:val`` (у gridSpan, gridBefore, gridAfter)."""
    if element is None:
        return default
    raw = element.get(_W_VAL)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _fragment_text(element):
    """Текст фрагмента XML по порядку следования (абзаца, ячейки, строки)."""
    if element.tag in _SKIP_TAGS:
        return ""                              # удалённый текст и коды полей
    if element.tag == _W_T:
        return element.text or ""
    if element.tag in (_W_BR, _W_CR):
        return "\n"                            # принудительный перенос строки
    if element.tag == _W_TAB:
        return " "
    if element.tag == _W_NO_BREAK_HYPHEN:
        return "-"
    if element.tag == _W_TBL:
        return ""                              # вложенную таблицу разбираем отдельно
    return "".join(_fragment_text(child) for child in element)


def _clean_text(raw):
    """Убирает лишние пробелы: пустые строки выбрасываются, внутри строки — один пробел."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = _MULTISPACE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _cell_text(cell):
    """Текст ячейки: абзацы склеиваются переносом строки, лишние пробелы убираются."""
    parts = []
    for paragraph in _children(cell, _W_P):
        parts.append(_fragment_text(paragraph))
    return _clean_text("\n".join(parts))


def _cell_span(cell):
    """Сколько столбцов занимает ячейка (``w:gridSpan``), иначе один."""
    properties = cell.find(_W_TCPR)
    span = 1
    if properties is not None:
        span = _int_attr(properties.find(_W_GRID_SPAN), 1)
    if span < 1:
        span = 1
    return min(span, _MAX_SPAN)


def _row_edge(row, tag):
    """Сколько столбцов пропущено в начале (gridBefore) или в конце (gridAfter) строки."""
    properties = row.find(_W_TRPR)
    if properties is None:
        return 0
    value = _int_attr(properties.find(tag), 0)
    return value if value > 0 else 0


# ----------------------------------------------------------------- разбор документа

def _document_root(data):
    """Проверяет, что ``data`` — это .docx, и возвращает корень ``word/document.xml``."""
    if isinstance(data, str):
        raise DocxError("ожидались байты .docx, а получена строка; "
                        "для файла есть docx_tables_from_file(path)")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise DocxError("ожидались байты .docx, получен %s" % type(data).__name__)
    payload = bytes(data)
    if not payload:
        raise DocxError("пустые данные: это не .docx")

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            name = _document_xml_name(names)
            if name is None:
                raise DocxError("в архиве нет %s — это не .docx (файлов в архиве: %d)"
                                % (DOCUMENT_XML, len(names)))
            xml = archive.read(name)
    except DocxError:
        raise
    except Exception as exc:  # noqa: BLE001 — BadZipFile, zlib.error, OSError, RuntimeError…
        # Любая беда с zip-контейнером означает одно: нам подсунули не .docx.
        raise DocxError("не удалось открыть zip-контейнер .docx: %s" % exc) from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocxError("битый XML в %s: %s" % (DOCUMENT_XML, exc)) from exc
    if _local_name(root.tag) != "document":
        raise DocxError("ожидался корневой элемент w:document, а найден %r" % (root.tag,))
    return root


def _document_xml_name(names):
    """Имя ``word/document.xml`` в архиве (с учётом регистра и ведущего слэша)."""
    for name in names:
        if name.lstrip("/").lower() == DOCUMENT_XML:
            return name
    return None


def _collect_tables(element, out):
    """Собирает таблицы в порядке появления; вложенные таблицы не дублируются."""
    for child in element:
        if child.tag == _W_TBL:
            out.append(child)                  # внутрь таблицы не заходим
        else:
            _collect_tables(child, out)


def _table_width(table, rows):
    """Число столбцов таблицы: по ``w:tblGrid``, а если его нет — по самой широкой строке."""
    width = 0
    grid = table.find(_W_TBL_GRID)
    if grid is not None:
        width = len(grid.findall(_W_GRID_COL))
    for row in rows:
        if len(row) > width:
            width = len(row)
    return width


def _table_rows(table):
    """Строки таблицы, развёрнутые до одинаковой длины (ширины таблицы).

    Ячейки с ``gridSpan`` дублируются, пропущенные из-за ``gridBefore`` и
    ``gridAfter`` столбцы и короткие «хвосты» дополняются пустыми строками.
    """
    rows = []
    for row_element in _children(table, _W_TR):
        row = [""] * _row_edge(row_element, _W_GRID_BEFORE)
        for cell in _children(row_element, _W_TC):
            row.extend([_cell_text(cell)] * _cell_span(cell))
        row.extend([""] * _row_edge(row_element, _W_GRID_AFTER))
        rows.append(row)

    width = _table_width(table, rows)
    for row in rows:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
    return rows


def _walk_text(element, out):
    """Обходит документ по порядку и складывает текст абзацев (в том числе в ячейках)."""
    for child in element:
        if child.tag in _SKIP_TAGS:
            continue                           # удалённый текст и коды полей
        if child.tag == _W_P:
            out.append(_clean_text(_fragment_text(child)))
        else:
            _walk_text(child, out)


# ------------------------------------------------------------------ публичный API

def docx_tables(data):
    """Таблицы из .docx: ``[таблица][строка][ячейка]``.

    Текст ячеек — как в документе: переносы строк внутри ячейки сохраняются как
    ``'\\n'``, лишние пробелы убираются. Объединения разворачиваются так, чтобы
    длина каждой строки совпадала с шириной таблицы: ``gridSpan`` дублирует
    содержимое ячейки, ячейки-продолжения ``vMerge`` остаются пустыми.

    Порядок таблиц — как в документе. Если таблиц нет, возвращается пустой
    список; если ``data`` не .docx — поднимается :class:`DocxError`.
    """
    root = _document_root(data)
    tables = []
    _collect_tables(root, tables)
    return [_table_rows(table) for table in tables]


def docx_text(data):
    """Весь текст документа по порядку (абзацы и ячейки) — для отладки и поиска."""
    root = _document_root(data)
    lines = []
    _walk_text(root, lines)
    return "\n".join(line for line in lines if line)


def docx_tables_from_file(path):
    """То же, что :func:`docx_tables`, но читает файл с диска.

    Принимает строку или ``os.PathLike``. Если файла нет или он недоступен,
    поднимается :class:`DocxError`.
    """
    try:
        filename = os.fspath(path)
    except TypeError as exc:
        raise DocxError("неверный путь к файлу: %r" % (path,)) from exc
    try:
        with open(filename, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise DocxError("не удалось прочитать файл %s: %s" % (filename, exc)) from exc
    return docx_tables(data)
