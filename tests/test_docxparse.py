# -*- coding: utf-8 -*-
"""Самопроверка разбора расписания КГАСУ из .docx (schedule/docxparse.py).

Запуск из корня проекта::

    python tests/test_docxparse.py

Тест офлайн: сеть не нужен, сторонних пакетов нет. Основные проверки идут по
реальному образцу ``tests/fixtures/kgasu_26RP01_02.docx`` (3 таблицы, главная —
71 строка на 8 столбцов), остальные — по «мусору» и по маленьким .docx, которые
собираются тут же в памяти (zipfile + строка XML). Итог печатается строкой
``OK: N проверок``, код возврата 0 — всё прошло, 1 — есть ошибки.
"""

import io
import os
import pathlib
import re
import sys
import zipfile

try:  # чтобы русские подписи не ломали вывод в старой консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from schedule.docxparse import (                          # noqa: E402
    DocxError,
    docx_tables,
    docx_tables_from_file,
    docx_text,
)

#: образец настоящего расписания КГАСУ (26РП01 / 26РП02)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "kgasu_26RP01_02.docx")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: время пары в расписании: «8:00:00 - 9:30:00»
TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2} - \d{1,2}:\d{2}:\d{2}$")

CASES = []


def check(title, condition, detail=""):
    """Записывает результат одной проверки."""
    CASES.append((title, bool(condition), detail))
    return bool(condition)


# ------------------------------------------------------------- вспомогательные файлы

def make_docx(body_xml):
    """Собирает минимальный .docx в памяти: внутри только word/document.xml."""
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (W_NS, body_xml))
    return make_zip([("word/document.xml", xml)])


def make_zip(entries):
    """Собирает zip-архив в памяти из пар (имя, содержимое)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def outcome(func, *args):
    """Что произошло при вызове: 'DocxError', 'без исключения' или другое исключение."""
    try:
        func(*args)
    except DocxError:
        return "DocxError"
    except Exception as exc:                              # noqa: BLE001 — для отчёта в тесте
        return "другое исключение %s: %s" % (type(exc).__name__, exc)
    return "без исключения"


# ------------------------------------------------------------------- 1. структура

def check_structure(data):
    """Общие размеры: сколько таблиц, строк и столбцов."""
    print("\n1. Структура документа")
    tables = docx_tables(data)

    check("таблиц ровно 3", len(tables) == 3, len(tables))
    check("все ячейки — строки",
          all(isinstance(cell, str) for table in tables for row in table for cell in row))
    check("внутри каждой таблицы все строки одной длины",
          all(table and len({len(row) for row in table}) == 1 for table in tables),
          [sorted({len(row) for row in table}) for table in tables])

    widths = [len(table[0]) for table in tables]
    check("ширина таблиц 2, 8, 5 (шапка, расписание, подписи)", widths == [2, 8, 5], widths)

    main = tables[1]
    check("в главной таблице 71 строка", len(main) == 71, len(main))
    check("в главной таблице 8 столбцов", len(main[0]) == 8, len(main[0]))
    check("каждая строка главной таблицы длиной 8",
          all(len(row) == 8 for row in main), sorted({len(row) for row in main}))
    check("лишних пробелов в ячейках нет (края, двойные пробелы, табы)",
          all(cell == cell.strip() and "  " not in cell and "\t" not in cell
              for table in tables for row in table for cell in row))
    return tables


# --------------------------------------------------------------------- 2. заголовок

def check_header(main):
    """Заголовок главной таблицы и разворот горизонтальных объединений."""
    print("\n2. Заголовок главной таблицы")
    header = main[0]

    check("заголовок начинается с «Дата, Номер, Неделя, Время»",
          header[:4] == ["Дата", "Номер", "Неделя", "Время"], header[:4])
    check("заголовок содержит группы «26РП01» и «26РП02»",
          "26РП01" in header and "26РП02" in header, header)
    check("gridSpan развёрнут: у 26РП01 и 26РП02 по два столбца",
          header.count("26РП01") == 2 and header.count("26РП02") == 2, header)
    check("вторая строка заголовка — подгруппы",
          main[1] == ["", "", "", "", "Подгруппа 2", "Подгруппа 1", "Подгруппа 1", "Подгруппа 2"],
          main[1])
    check("vMerge-объединение заголовка даёт пустые ячейки, а не мусор",
          main[1][:4] == ["", "", "", ""], main[1][:4])


# ------------------------------------------------------------------------ 3. данные

def check_data(tables, main):
    """Строки занятий: дни недели, пары, чётность, время, ячейки «С 28.09.26.»."""
    print("\n3. Данные главной таблицы")

    weekdays = [row[0] for row in main if row[0] and row[0] != "Дата"]
    check("есть строка с «Понедельник»", "Понедельник" in weekdays, weekdays)
    check("дни недели идут по порядку от понедельника до субботы",
          weekdays == ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"],
          weekdays)

    parity = [row[2] for row in main]
    check("есть строки «Чет» и «Неч»",
          parity.count("Чет") > 0 and parity.count("Неч") > 0,
          {"Чет": parity.count("Чет"), "Неч": parity.count("Неч")})
    check("чётных и нечётных строк поровну (34 и 34)",
          parity.count("Чет") == 34 and parity.count("Неч") == 34,
          {"Чет": parity.count("Чет"), "Неч": parity.count("Неч")})

    first_day = [row for row in main[2:] if row[1] == "1"]
    check("первая пара понедельника: «Понедельник», «1», «Чет», «8:00:00 - 9:30:00»",
          bool(first_day) and first_day[0][:4] == ["Понедельник", "1", "Чет", "8:00:00 - 9:30:00"],
          first_day[:1])
    check("время каждой пары вида «8:00:00 - 9:30:00»",
          all(TIME_RE.match(row[3]) for row in main[2:] if row[1]),
          [row[3] for row in main[2:] if row[1] and not TIME_RE.match(row[3])])

    cells = [cell for table in tables for row in table for cell in row]
    check("в ячейках есть время «8:00:00 - 9:30:00»", "8:00:00 - 9:30:00" in cells)
    check("в ячейках есть номер пары «1»", "1" in cells)

    dated = [cell for cell in cells if cell.startswith("С 28.09.26.")]
    check("найдена ячейка, начинающаяся с «С 28.09.26.»", bool(dated), dated[:1])
    check("в этой ячейке есть «Социология культуры»",
          bool(dated) and "Социология культуры" in dated[0], dated[:1])
    check("внутри ячейки абзацы разделены переносом строки '\\n'",
          bool(dated) and "\n" in dated[0], dated[:1])

    # «Неч»-строка пары: номер и время объединены со строкой «Чет» и потому пусты
    monday = [row for row in main[2:] if row[2] in ("Чет", "Неч")]
    check("строка «Неч» повторяет день, но номер и время берёт из вертикального объединения",
          bool(monday) and monday[1][:4] == ["", "", "Неч", ""], monday[1][:4])


# ------------------------------------------------------------------ 4. служебные

def check_service_tables(tables):
    """Две служебные таблицы (шапка и подписи) тоже возвращаются, порядок сохранён."""
    print("\n4. Служебные таблицы")
    header, signatures = tables[0], tables[2]

    check("первая таблица — шапка «с 01.09.2026 (16 недель)»",
          "с 01.09.2026 (16 недель)" in header[0], header[0])
    check("в первой таблице есть «У Т В Е Р Ж Д А Ю»",
          any("У Т В Е Р Ж Д А Ю" in cell for cell in header[0]), header[0])
    check("третья таблица — подписи (текст и пустые ячейки)",
          any("Начальник учебного отдела" in cell for cell in signatures[0])
          and "" in signatures[0], signatures[0])


# ------------------------------------------------------------------- 5. docx_text

def check_text(data):
    """Полный текст документа: абзацы вне таблиц и содержимое ячеек."""
    print("\n5. docx_text")
    text = docx_text(data)

    check("docx_text вернул непустую строку", isinstance(text, str) and len(text) > 1000,
          len(text) if isinstance(text, str) else type(text).__name__)
    check("текст начинается с шапки университета",
          text.startswith("КАЗАНСКИЙ ГОСУДАРСТВЕННЫЙ АРХИТЕКТУРНО-СТРОИТЕЛЬНЫЙ УНИВЕРСИТЕТ"),
          text[:80])
    check("в тексте есть абзацы вне таблиц («Реконструкция и реставрация…»)",
          "Реконструкция и реставрация архитектурного наследия" in text)
    check("в тексте есть содержимое ячеек («Понедельник»)", "Понедельник" in text)
    check("в тексте есть «Социология культуры»", "Социология культуры" in text)
    check("лишних пробелов в тексте нет", "  " not in text and "\u00a0" not in text)


# ------------------------------------------------------ 6. мусор и битые контейнеры

def check_errors():
    """На любом мусоре — понятное DocxError, а не случайное исключение."""
    print("\n6. Мусор и битые контейнеры")

    check("DocxError — наследник Exception", issubclass(DocxError, Exception))

    junk = b"\x00\x01\x02 not a zip at all \xff\xfe PK\x03\x04"
    result = outcome(docx_tables, junk)
    check("случайные байты: DocxError", result == "DocxError", result)

    result = outcome(docx_tables, b"")
    check("пустые данные: DocxError", result == "DocxError", result)

    result = outcome(docx_tables, make_zip([]))
    check("пустой zip: DocxError", result == "DocxError", result)

    result = outcome(docx_tables, make_zip([("word/styles.xml", "<a/>")]))
    check("zip без word/document.xml: DocxError", result == "DocxError", result)

    broken = make_zip([("word/document.xml", "<w:document><не закрыт")])
    result = outcome(docx_tables, broken)
    check("битый XML внутри document.xml: DocxError", result == "DocxError", result)

    result = outcome(docx_text, junk)
    check("docx_text на мусоре тоже даёт DocxError", result == "DocxError", result)

    result = outcome(docx_tables, "это строка, а не байты")
    check("строка вместо байтов: DocxError", result == "DocxError", result)

    missing = os.path.join(ROOT, "tests", "fixtures", "такого-файла-нет.docx")
    result = outcome(docx_tables_from_file, missing)
    check("несуществующий файл: DocxError", result == "DocxError", result)


# ------------------------------------------- 7. корректный .docx без таблиц и синтетика

def check_synthetic():
    """Маленькие .docx, собранные в памяти: объединения, переносы, добор строк."""
    print("\n7. Синтетические документы")

    plain = make_docx("<w:p><w:r><w:t>Привет</w:t></w:r></w:p>")
    check("корректный .docx без таблиц даёт пустой список", docx_tables(plain) == [])
    check("текст такого .docx читается", docx_text(plain) == "Привет")

    table = ("<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>"
             "<w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr>"
             "<w:p><w:r><w:t>Лекция</w:t></w:r></w:p>"
             "<w:p><w:r><w:t>доц. Иванов</w:t></w:r></w:p></w:tc>"
             "<w:tc><w:p><w:r><w:t>Б</w:t></w:r></w:p></w:tc></w:tr>"
             "<w:tr><w:trPr><w:gridBefore w:val=\"1\"/></w:trPr>"
             "<w:tc><w:p><w:r><w:t>В</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    rows = docx_tables(make_docx(table))[0]

    check("gridSpan=2 дублирует текст ячейки в оба столбца",
          rows[0][0] == rows[0][1] == "Лекция\nдоц. Иванов", rows[0])
    check("короткая строка добита пустыми ячейками до ширины таблицы",
          [len(row) for row in rows] == [3, 3], rows)
    check("gridBefore сдвигает строку вправо пустой ячейкой",
          rows[1] == ["", "В", ""], rows[1])

    line_break = ("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Первая</w:t><w:br/>"
                  "<w:t>вторая</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    check("принудительный перенос <w:br/> становится '\\n'",
          docx_tables(make_docx(line_break))[0][0][0] == "Первая\nвторая",
          docx_tables(make_docx(line_break))[0][0][0])

    check("текст синтетического .docx собирается по порядку",
          docx_text(make_docx(table)) == "Лекция\nдоц. Иванов\nБ\nВ",
          docx_text(make_docx(table)))


# ----------------------------------------------------------------- 8. чтение файла

def check_from_file(tables):
    """docx_tables_from_file: путь строкой и os.PathLike."""
    print("\n8. Чтение из файла")

    check("docx_tables_from_file совпадает с docx_tables",
          docx_tables_from_file(FIXTURE) == tables)
    check("docx_tables_from_file принимает os.PathLike (pathlib.Path)",
          docx_tables_from_file(pathlib.Path(FIXTURE)) == tables)
    check("файл-образец на месте", os.path.getsize(FIXTURE) > 0)


# ---------------------------------------------------------------------- запуск

def main():
    if not os.path.exists(FIXTURE):
        print("НЕТ ОБРАЗЦА: %s" % FIXTURE)
        return 1
    with open(FIXTURE, "rb") as handle:
        data = handle.read()
    print("Образец: %s (%d байт)" % (os.path.relpath(FIXTURE, ROOT), len(data)))

    tables = check_structure(data)
    check_header(tables[1])
    check_data(tables, tables[1])
    check_service_tables(tables)
    check_text(data)
    check_errors()
    check_synthetic()
    check_from_file(tables)

    failed = 0
    for number, (title, ok, detail) in enumerate(CASES, 1):
        print("%2d. [%s] %s" % (number, "OK" if ok else "FAIL", title))
        if not ok:
            failed += 1
            if detail != "":
                print("      подробности: %s" % (detail,))
    print("-" * 60)
    if failed:
        print("ПРОВАЛ: %d из %d проверок не пройдено" % (failed, len(CASES)))
        return 1
    print("OK: %d проверок" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
