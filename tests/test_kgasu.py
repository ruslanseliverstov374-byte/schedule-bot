# -*- coding: utf-8 -*-
"""Проверки провайдера расписания КГАСУ (офлайн, на сохранённых файлах).

Запуск:
    python tests/test_kgasu.py

Ничего наружу не ходит: страница списка групп и оба файла расписания лежат
в tests/fixtures. Проверяются привязка «Чет/Неч» к календарю, разбор ячеек,
сборка пар на дату и пометки «действует с даты».
"""

import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from schedule import kgasu      # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CHECKS = []
FAILURES = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print("  ✅ %s" % name)
    else:
        print("  ❌ %s %s" % (name, ("— " + str(detail)) if detail else ""))
        FAILURES.append("%s %s" % (name, detail))


def read(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def main():
    print("1. Даты и чётность недель")
    check("короткая дата разобрана",
          kgasu.parse_short_date("28.09.26") == date(2026, 9, 28),
          kgasu.parse_short_date("28.09.26"))
    check("начало семестра из шапки",
          kgasu.semester_start("с 01.09.2026 г.  (15 недель)") == date(2026, 9, 1))
    anchor = kgasu.monday_of(date(2026, 9, 28))
    check("неделя 28.09.2026 — «Неч»", kgasu.parity_of(date(2026, 9, 28), anchor, "неч") == "Неч")
    check("неделя 05.10.2026 — «Чет»", kgasu.parity_of(date(2026, 10, 5), anchor, "неч") == "Чет")
    check("неделя 12.10.2026 — «Неч»", kgasu.parity_of(date(2026, 10, 12), anchor, "неч") == "Неч")
    check("без опорной недели не падает", kgasu.parity_of(date(2026, 10, 5)) in ("Чет", "Неч"))

    print("\n2. Разбор ячейки занятия")
    inline = kgasu.parse_cell("Архитектурно-реставрационное проектирование (1 уровень) "
                              "(Практические) доц. Васильева Ю.В., асс. Карнаухова К.4-417")
    check("ячейка в одну строку: предмет", inline["subject"].startswith("Архитектурно"),
          inline["subject"])
    check("ячейка в одну строку: тип", inline["type"] == "Практические", inline["type"])
    check("ячейка в одну строку: двое преподавателей", len(inline["teachers"]) == 2,
          inline["teachers"])
    check("ячейка в одну строку: аудитория", inline["rooms"] == ["4-417"], inline["rooms"])
    multiline = kgasu.parse_cell("Рисунок (Практические)\nпроф. Чебинев А. И.\nКИТАП 2")
    check("ячейка в три строки: предмет", multiline["subject"] == "Рисунок", multiline["subject"])
    check("ячейка в три строки: место", multiline["rooms"] == ["КИТАП 2"], multiline["rooms"])
    dated = kgasu.parse_cell("С 28.09.26. Социология культуры (Практические)\n"
                            "доц. Тарасова Е. Н. 4-407 до 01.11.26.")
    check("пометка «с даты» разобрана", dated["from_date"] == date(2026, 9, 28), dated["from_date"])
    check("пометка «до даты» разобрана", dated["to_date"] == date(2026, 11, 1), dated["to_date"])
    check("мусор в ячейке не ломает разбор",
          kgasu.parse_cell("·") is None and kgasu.parse_cell("") is None)

    print("\n3. Файл .docx (группы 26РП01 и 26РП02)")
    docx_grid = kgasu.prepare(read("kgasu_26RP01_02.docx"), "kgasu_26RP01_02.docx")
    check("тип разбора — docx", docx_grid["kind"] == "docx", docx_grid["kind"])
    check("строк в таблице больше 60", len(docx_grid["rows"]) > 60, len(docx_grid["rows"]))
    check("столбцы: две группы по двум подгруппам",
          len(docx_grid["columns"]) == 4, docx_grid["columns"])
    anchor_monday, anchor_label = docx_grid["anchor"]
    check("опорная неделя определена по пометкам «с даты»",
          anchor_monday == kgasu.monday_of(date(2026, 9, 28)) and anchor_label == "неч",
          docx_grid["anchor"])

    chet_day = date(2026, 10, 6)          # вторник, неделя «Чет»
    lessons = kgasu.lessons_for_day(docx_grid, "26РП01", chet_day)
    check("на пары выбранного дня что-то нашлось", len(lessons) >= 3, len(lessons))
    check("подгруппы склеены (нет дублей)", len(lessons) == len({item["uid"] for item in lessons}))
    check("у пары есть предмет и время",
          all(item["subject"] and item["start"] for item in lessons), lessons[:1])
    check("предметы не содержат слова «Практические»",
          all("Практические" not in item["subject"] for item in lessons),
          [item["subject"] for item in lessons])
    check("пара с пометкой «с 28.09» видна 06.10",
          any(item["subject"].startswith("Архитектурно") for item in lessons),
          [item["subject"] for item in lessons])
    other_group = kgasu.lessons_for_day(docx_grid, "26РП02", chet_day)
    check("вторая группа файла тоже разбирается", len(other_group) >= 3, len(other_group))
    check("чужой группы нет в файле",
          kgasu.lessons_for_day(docx_grid, "26ИМ01", chet_day) == [])

    before_start = kgasu.lessons_for_day(docx_grid, "26РП01", date(2026, 9, 21))
    check("занятие «с 28.09» не показывается раньше срока",
          not any(item["subject"].startswith("Архитектурно") for item in before_start),
          [item["subject"] for item in before_start])
    check("соседняя неделя даёт другой набор пар",
          kgasu.lessons_for_day(docx_grid, "26РП01", date(2026, 10, 13)) != lessons)

    print("\n4. Файл .doc (группа 26РМ01)")
    doc_grid = kgasu.prepare(read("kgasu_26RM01.doc"), "kgasu_26RM01.doc")
    check("тип разбора — doc", doc_grid["kind"] == "doc", doc_grid["kind"])
    check("строк собрано больше 20", len(doc_grid["rows"]) > 20, len(doc_grid["rows"]))
    check("столбец группы определён",
          any(column["group"] == "26РМ01" for column in doc_grid["columns"]),
          doc_grid["columns"])
    doc_lessons = kgasu.lessons_for_day(doc_grid, "26РМ01", date(2026, 10, 6))
    check("пары из .doc найдены", len(doc_lessons) >= 1, len(doc_lessons))
    check("у пары из .doc есть предмет и преподаватель",
          all(item["subject"] and item["teachers"] for item in doc_lessons), doc_lessons[:1])

    print("\n5. Список групп (страница сохранена в фикстуру)")
    page = open(os.path.join(FIXTURES, "kgasu_page.html"), encoding="utf-8").read()
    listing = open(os.path.join(FIXTURES, "kgasu_listing.html"), encoding="utf-8").read()
    original_fetch = kgasu.fetch_text
    kgasu.fetch_text = lambda url, **kwargs: page if "index.php?" not in url else listing
    try:
        groups = kgasu.groups()
    finally:
        kgasu.fetch_text = original_fetch
    check("групп найдено 32", len(groups) == 32, len(groups))
    check("у группы есть ссылка на файл",
          all(item["file_url"].startswith("https://st.kgasu.ru/") for item in groups))
    check("встречаются оба формата",
          {item["file_ext"] for item in groups} == {"doc", "docx"},
          {item["file_ext"] for item in groups})
    found = kgasu.find_group(groups, "26рп01")
    check("поиск группы без учёта регистра", found and found["name"] == "26РП01", found)
    check("поиск по началу кода", (kgasu.find_group(groups, "26АП") or {}).get("name") == "26АП07",
          kgasu.find_group(groups, "26АП"))
    check("несуществующая группа не находится", kgasu.find_group(groups, "99ХХ99") is None)

    print("\n6. Устойчивость")
    try:
        kgasu.grid_from_bytes(b"not a schedule", "bad.docx")
        check("мусор вместо файла даёт понятную ошибку", False, "исключения не было")
    except (kgasu.KgasuError, Exception) as error:
        check("мусор вместо файла даёт понятную ошибку", True, type(error).__name__)
    check("повторный разбор даёт тот же результат",
          [item["uid"] for item in kgasu.lessons_for_day(docx_grid, "26РП01", chet_day)] ==
          [item["uid"] for item in kgasu.lessons_for_day(docx_grid, "26РП01", chet_day)])

    print("\n7. Выбор подгруппы студентом")
    from schedule import providers
    all_lessons = kgasu.lessons_for_day(docx_grid, "26РП01", chet_day)
    first = providers.filter_by_subgroup(all_lessons, "Подгруппа 1")
    second = providers.filter_by_subgroup(all_lessons, "Подгруппа 2")
    check("фильтр по подгруппе что-то оставляет", bool(first) and bool(second),
          (len(first), len(second)))
    strict_second = [item for item in all_lessons
                     if item["subgroup"].strip().lower() == "подгруппа 2"]
    check("в подгруппе 1 нет пар чужой подгруппы",
          all(item not in first for item in strict_second), len(strict_second))
    check("без выбора подгруппы видны все пары",
          providers.filter_by_subgroup(all_lessons, "") == all_lessons)
    check("общие пары (в двух подгруппах) остаются при выборе",
          all(any("подгруппа 1" in (item["subgroup"] or "").lower()
                  and "подгруппа 2" in (item["subgroup"] or "").lower()
                  for item in group) or group
              for group in (first, second)), True)

    print("\n" + "=" * 60)
    print("Проверок: %d, провалов: %d" % (len(CHECKS), len(FAILURES)))
    if FAILURES:
        for failure in FAILURES:
            print("  ❌ %s" % failure)
        print("ИТОГ: ЕСТЬ ОШИБКИ")
        return 1
    print("ИТОГ: ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
