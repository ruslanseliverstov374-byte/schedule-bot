# -*- coding: utf-8 -*-
"""Все тексты и клавиатуры бота расписания (русский язык).

Здесь только представление: функции принимают данные и возвращают строки
(HTML для Telegram) либо разметку кнопок. Логика — в bot.py.
"""

from datetime import date, datetime

from schedule import unifirst
from schedule.unifirst import day_label, week_label

# ------------------------------------------------------------- подписи кнопок

BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "📅 Завтра"
BTN_WEEK = "🗓 Неделя"
BTN_HOMEWORK = "📝 ДЗ"
BTN_REMINDERS = "⏰ Напоминания"
BTN_REFRESH = "🔄 Обновить"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"

MAIN_BUTTONS = [
    [BTN_TODAY, BTN_TOMORROW],
    [BTN_WEEK, BTN_HOMEWORK],
    [BTN_REMINDERS, BTN_REFRESH],
    [BTN_SETTINGS, BTN_HELP],
]

BTN_BACK = "⬅️ Назад"
BTN_MENU = "🏠 Меню"
BTN_CANCEL = "✖️ Отмена"
#: Кнопка админ-панели: показывается только админам бота.
BTN_ADMIN = "👑 Админ"

TYPE_ICONS = {
    "л.": "📖", "лек": "📖", "лекция": "📖",
    "пр.": "✏️", "практ": "✏️", "семинар": "✏️",
    "лаб.": "🔬", "лаб": "🔬",
    "мет.": "🧭", "зач": "📋", "экз": "🎓",
}


def esc(value):
    """Экранирование под HTML-разметку Telegram."""
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def plural(number, one, few, many):
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def pair_word(number):
    return "%d %s" % (number, plural(number, "пара", "пары", "пар"))


# ------------------------------------------------------------------ онбординг

def hello(first_name="", university="Поволжского ГУФКСиТ", city="Казань"):
    name = (", " + esc(first_name)) if first_name else ""
    return (
        "👋 Привет%s!\n\n"
        "Я бот расписания <b>%s</b> (%s).\n"
        "Показываю пары прямо с официального сайта расписания — всегда актуальные, "
        "с аудиториями и преподавателями.\n\n"
        "Что умею:\n"
        "📅 расписание на сегодня, завтра и любую неделю\n"
        "📝 общая домашка группы и личные заметки\n"
        "⏰ напоминания вечером и перед парой\n"
        "🔔 сообщу, если расписание поменяют\n\n"
        "<b>Выбери свою группу</b> — и всё заработает."
        % (name, esc(university), esc(city)))


def group_list_page(groups, page, pages, query="", current="", example="26281"):
    header = "🎓 <b>Выбор группы</b>\n\n"
    if query:
        header += "Поиск: <b>%s</b>\n" % esc(query)
    else:
        header += ("Найди свою группу в списке или просто напиши её номер "
                   "(например <code>%s</code>).\n" % esc(example))
    if not groups:
        return (header + "\n😕 Ничего не нашлось. Напиши номер группы иначе — "
                "например только цифры.", [])
    lines = []
    for group in groups:
        mark = " ✅" if str(group.get("name")) == str(current) else ""
        subgroups = group.get("subgroups") or []
        extra = " (+подгруппы)" if subgroups else ""
        lines.append("• <b>%s</b>%s%s" % (esc(group.get("name")), extra, mark))
    footer = "\n\nСтраница %d из %d" % (page + 1, max(pages, 1))
    return header + "\n" + "\n".join(lines) + footer, groups


def group_confirmed(group_title, subgroups=None):
    text = "✅ Группа <b>%s</b> выбрана.\n\n" % esc(group_title)
    if subgroups:
        text += "В группе есть подгруппы — выбери свою:"
    else:
        text += "Теперь я буду показывать твоё расписание."
    return text


# ---------------------------------------------------------------- расписание

def lesson_block(lesson, index=None):
    """Одна пара подробно."""
    icon = TYPE_ICONS.get((lesson.get("type") or "").strip().lower(), "📘")
    title = esc(lesson.get("subject"))
    kind = esc(lesson.get("type") or "")
    time_text = ""
    if lesson.get("start"):
        time_text = lesson["start"]
        if lesson.get("end"):
            time_text += "–" + lesson["end"]
    head_parts = []
    if index:
        head_parts.append("%d." % index)
    if lesson.get("para"):
        head_parts.append("%s пара" % lesson["para"])
    if time_text:
        head_parts.append(time_text)
    head = " · ".join(head_parts)
    lines = []
    if head:
        lines.append("<b>%s</b>" % head)
    lines.append("%s %s%s" % (icon, title, (" (%s)" % kind) if kind else ""))
    if lesson.get("teachers"):
        lines.append("👤 " + esc(", ".join(lesson["teachers"])))
    if lesson.get("rooms"):
        lines.append("🚪 " + esc(", ".join(lesson["rooms"])))
    return "\n".join(lines)


def slot_block(block, index=None):
    """Пара целиком: если она делится на подгруппы — показываем варианты выбора.

    Раньше каждая подгруппа рисовалась отдельным блоком с одинаковым временем и
    предметом, и расписание выглядело так, будто предмет задублирован.
    """
    lessons = (block or {}).get("lessons") or []
    if not lessons:
        return ""
    if len(lessons) == 1:
        return lesson_block(lessons[0], index)

    head_parts = []
    if index:
        head_parts.append("%d." % index)
    if block.get("para"):
        head_parts.append("%s пара" % block["para"])
    time_text = block.get("start") or ""
    if time_text and block.get("end"):
        time_text += "–" + block["end"]
    if time_text:
        head_parts.append(time_text)
    lines = []
    if head_parts:
        lines.append("<b>%s</b>" % " · ".join(head_parts))

    subjects = {(lesson.get("subject") or "").strip() for lesson in lessons}
    if len(subjects) == 1:
        first = lessons[0]
        icon = TYPE_ICONS.get((first.get("type") or "").strip().lower(), "📘")
        lines.append("%s %s%s" % (
            icon, esc(first.get("subject")),
            (" (%s)" % esc(first.get("type"))) if first.get("type") else ""))
        lines.append("<i>🔀 делится на подгруппы — уточни свою:</i>")
        for lesson in lessons:
            who = esc(", ".join(lesson.get("teachers") or [])) or "преподаватель не указан"
            rooms = esc(", ".join(lesson.get("rooms") or []))
            lines.append("   • %s%s" % (who, (" · 🚪 " + rooms) if rooms else ""))
        return "\n".join(lines)

    # В одном слоте разные предметы — значит это варианты по подгруппам:
    # показываем каждый отдельно и подписываем, чья это подгруппа.
    for lesson in lessons:
        icon = TYPE_ICONS.get((lesson.get("type") or "").strip().lower(), "📘")
        subgroup = (lesson.get("subgroup") or "").strip()
        prefix = "[%s] " % esc(subgroup) if subgroup and "/" not in subgroup else ""
        lines.append("%s %s%s%s" % (
            icon, prefix, esc(lesson.get("subject")),
            (" (%s)" % esc(lesson.get("type"))) if lesson.get("type") else ""))
        if lesson.get("teachers"):
            lines.append("   👤 " + esc(", ".join(lesson["teachers"])))
        if lesson.get("rooms"):
            lines.append("   🚪 " + esc(", ".join(lesson["rooms"])))
    return "\n".join(lines)


def slot_compact(block):
    """Одна строка для недельного списка: с подгруппами — без дублей."""
    lessons = (block or {}).get("lessons") or []
    if not lessons:
        return ""
    para = block.get("para") or "•"
    if len(lessons) == 1:
        lesson = lessons[0]
        bits = [("✏️ " + esc(lesson.get("subject", ""))).strip()]
        if lesson.get("start"):
            bits.append(lesson["start"])
        if lesson.get("rooms"):
            bits.append(esc(", ".join(lesson["rooms"])))
        if lesson.get("teachers"):
            bits.append(esc(", ".join(lesson["teachers"])))
        return "   <b>%s</b> %s" % (para, " · ".join(bits))

    subjects = {(lesson.get("subject") or "").strip() for lesson in lessons}
    subject = esc(lessons[0].get("subject", ""))
    bits = []
    if lessons[0].get("start"):
        bits.append(lessons[0]["start"])
    if len(subjects) == 1:
        rooms = []
        for lesson in lessons:
            room = esc(", ".join(lesson.get("rooms") or []))
            if room and room not in rooms:
                rooms.append(room)
        bits.append("подгруппы: %d" % len(lessons))
        if rooms:
            bits.append(" / ".join(rooms))
    else:
        subject = " · ".join(sorted(esc(item.get("subject", "")) for item in lessons))
    return "   <b>%s</b> ✏️ %s · %s" % (para, subject, " · ".join(bits))


def day_schedule(day, lessons, group_title, label=None):
    """Подробное расписание одного дня (с учётом деления на подгруппы)."""
    title = label or day_label(day)
    header = "📅 <b>%s</b>\n" % esc(title)
    if group_title:
        header += "Группа <b>%s</b>\n" % esc(group_title)
    if not lessons:
        return header + "\n🎉 Занятий нет — отдыхай!"
    blocks = unifirst.group_by_slot(lessons)
    return header + "\n" + "\n\n".join(slot_block(block) for block in blocks)


def day_compact(day, lessons):
    """Компактный день для недельного списка."""
    lines = ["<b>%s</b>" % esc(day_label(day))]
    if not lessons:
        return "\n".join(lines + ["   🎉 занятий нет"])
    for block in unifirst.group_by_slot(lessons):
        lines.append(slot_compact(block))
    return "\n".join(lines)


def week_schedule(lessons, group_title, year, week, current_note=""):
    header = "🗓 <b>%s</b>\n" % esc(week_label(year, week))
    if group_title:
        header += "Группа <b>%s</b>" % esc(group_title)
    if current_note:
        header += " · %s" % esc(current_note)
    if not lessons:
        return header + "\n\n🎉 На эту неделю занятий нет."
    days = {}
    for lesson in lessons:
        days.setdefault(lesson["date"], []).append(lesson)
    blocks = []
    for day_key in sorted(days):
        try:
            day = datetime.strptime(day_key, "%Y-%m-%d").date()
        except ValueError:
            day = date.today()
        blocks.append(day_compact(day, days[day_key]))
    return header + "\n\n" + "\n\n".join(blocks)


def week_short_list(lessons):
    """Очень краткая сводка недели: пары, которые вообще есть."""
    if not lessons:
        return "занятий нет"
    days = sorted({item["date"] for item in lessons})
    return "%s, %s" % (pair_word(len(lessons)), plural(len(days), "день", "дня", "дней"))


def no_group_hint():
    return ("⚠️ Сначала выбери группу — иначе я не знаю, чьё расписание показывать.\n"
            "Нажми /start или «⚙️ Настройки» → «Сменить группу».")


def cache_note(fetched_at=None, online=True):
    if not online:
        return "📴 Показал сохранённую копию — сейчас нет связи с сайтом расписания."
    return ""


def refresh_result(title, found_new, changes=(), week_note=""):
    lines = ["🔄 <b>Проверка расписания</b>", ""]
    lines.append("Группа <b>%s</b>" % esc(title))
    if week_note:
        lines.append(esc(week_note))
    lines.append("")
    if found_new:
        lines.append("⚠️ <b>Расписание изменилось!</b>")
        for change in changes:
            lines.append(change)
    else:
        lines.append("✅ Всё актуально, изменений нет.")
    return "\n".join(lines)


def changes_lines(changes):
    """Человекочитаемые строки изменений (added/removed/moved)."""
    lines = []
    for change in changes:
        lesson = change.get("lesson") or {}
        kind = change.get("kind")
        subject = esc(lesson.get("subject", ""))
        time_text = lesson.get("start") or ""
        rooms = esc(", ".join(lesson.get("rooms") or []))
        try:
            day = datetime.strptime(change.get("date", ""), "%Y-%m-%d").date()
            when = day_label(day)
        except ValueError:
            when = change.get("date", "")
        if kind == "added":
            lines.append("➕ %s: %s%s%s" % (
                esc(when), subject,
                (" · " + time_text) if time_text else "",
                (" · " + rooms) if rooms else ""))
        elif kind == "removed":
            lines.append("➖ %s: %s%s%s" % (
                esc(when), subject,
                (" · " + time_text) if time_text else "",
                (" · " + rooms) if rooms else ""))
        else:
            was = change.get("was") or {}
            detail = ""
            if was.get("rooms") and was.get("rooms") != lesson.get("rooms"):
                detail = " (аудитория: %s)" % esc(", ".join(was["rooms"]))
            lines.append("🔁 %s: %s%s" % (esc(when), subject, detail))
    return lines


# ------------------------------------------------------------------------ ДЗ

def homework_intro():
    return ("📝 <b>Домашние задания</b>\n\n"
            "Общая база группы: что задали, к какому сроку, преподаватель и аудитория. "
            "Любой может добавить — остальные увидят.\n"
            "Личные заметки видишь только ты.")


def homework_item(item, votes=0, mine=False, show_scope=False):
    scope_icon = "🗒" if item.get("scope") == "personal" else "📘"
    lines = ["%s <b>%s</b>" % (scope_icon, esc(item.get("subject") or "Без предмета"))]
    due = item.get("due_date") or ""
    if due:
        try:
            day = datetime.strptime(due, "%Y-%m-%d").date()
            due_text = day_label(day)
        except ValueError:
            due_text = due
        if item.get("due_time"):
            due_text += ", к " + item["due_time"]
        days_left = ""
        try:
            delta = (datetime.strptime(due, "%Y-%m-%d").date() - date.today()).days
            if delta == 0:
                days_left = " — 🔥 сегодня!"
            elif delta == 1:
                days_left = " — завтра"
            elif delta < 0:
                days_left = " — просрочено"
            else:
                days_left = " — через %d %s" % (delta, plural(delta, "день", "дня", "дней"))
        except ValueError:
            days_left = ""
        lines.append("   🕒 сдать: %s%s" % (esc(due_text), days_left))
    if item.get("task"):
        lines.append("   ✍️ %s" % esc(item["task"]))
    extra = []
    if item.get("teacher"):
        extra.append("👤 " + esc(item["teacher"]))
    if item.get("room"):
        extra.append("🚪 " + esc(item["room"]))
    if extra:
        lines.append("   " + " · ".join(extra))
    if votes:
        lines.append("   👍 подтвердили: %d" % votes)
    if show_scope:
        lines.append("   <i>%s</i>" % ("личная заметка" if item.get("scope") == "personal"
                                       else "общее задание группы"))
    return "\n".join(lines)


def explain_homework_how_to_add():
    return ("✍️ <b>Как добавить ДЗ</b>\n\n"
            "Отправь одним сообщением:\n"
            "<code>предмет | задание | срок</code>\n\n"
            "Примеры:\n"
            "<code>История | §§1-2, вопросы 3-5 | 08.10</code>\n"
            "<code>Русский | упражнение 45 | завтра</code>\n"
            "<code>Математика | задачи 1-10</code>\n\n"
            "Срок понимаю как <code>08.10</code>, <code>08.10.2026</code>, "
            "<code>завтра</code>, <code>сегодня</code>, <code>пн</code>.\n"
            "Куда добавить — общая база группы или личная заметка: кнопки ниже.")


def homework_created(item, scope):
    where = "в личные заметки" if scope == "personal" else "в общую базу группы"
    return ("✅ Добавил %s:\n\n%s" % (where, homework_item(item)))


def homework_empty(scope):
    if scope == "personal":
        return ("🗒 Личных заметок пока нет.\n\n"
                "Нажми «➕ Добавить ДЗ» и выбери «Личная заметка» — увидишь только ты.")
    return ("📭 ДЗ пока никто не добавил.\n\n"
            "Будь первым: «➕ Добавить ДЗ» — и вся группа увидит.")


def homework_menu(subject_count=0, open_count=0, personal_count=0):
    return ("📝 <b>ДЗ</b>\n\n"
            "Открытых заданий в группе: <b>%d</b>\n"
            "Личных заметок: <b>%d</b>" % (open_count, personal_count))


# ------------------------------------------------------------------ напоминания

def reminders_screen(user):
    evening = "вкл" if user.get("evening_enabled") else "выкл"
    before = user.get("before_minutes") or 0
    before_text = "выкл" if not before else "за %d мин" % before
    return ("⏰ <b>Напоминания</b>\n\n"
            "🌙 Вечерний дайджест: <b>%s</b>%s\n"
            "   Присылаю расписание на завтра и что сдать.\n\n"
            "🔔 Перед парой: <b>%s</b>\n"
            "   Предупрежу, чтобы не проспать.\n\n"
            "📣 Об изменениях: <b>%s</b>\n"
            "   Сообщу, если расписание поправят.\n\n"
            "📚 О дедлайнах ДЗ: <b>%s</b>" % (
                evening,
                (" в %s" % user.get("evening_time")) if user.get("evening_enabled") else "",
                before_text,
                "вкл" if user.get("change_alerts") else "выкл",
                "вкл" if user.get("hw_alerts") else "выкл"))


def evening_digest(user, day, lessons, homework, group_title):
    lines = ["🌙 <b>Завтра: %s</b>" % esc(day_label(day))]
    if group_title:
        lines.append("Группа <b>%s</b>" % esc(group_title))
    lines.append("")
    if not lessons:
        lines.append("🎉 Пар нет — можно отдыхать!")
    else:
        blocks = unifirst.group_by_slot(lessons)
        lines.append("<b>%s</b>" % pair_word(len(blocks)).capitalize())
        lines.append("")
        for block in blocks:
            items = block["lessons"]
            time_text = block.get("start") or ""
            first = "⏰ %s" % time_text if time_text else "⏰"
            if block.get("para"):
                first += " (%s пара)" % block["para"]
            lines.append(first)
            if len(items) == 1:
                lesson = items[0]
                lines.append("   📘 %s%s" % (
                    esc(lesson.get("subject")),
                    (" (%s)" % esc(lesson.get("type"))) if lesson.get("type") else ""))
                if lesson.get("rooms"):
                    lines.append("   🚪 %s" % esc(", ".join(lesson["rooms"])))
                if lesson.get("teachers"):
                    lines.append("   👤 %s" % esc(", ".join(lesson["teachers"])))
            else:
                # Пара делится на подгруппы: пишем предмет один раз и варианты.
                lines.append("   📘 %s%s" % (
                    esc(items[0].get("subject")),
                    (" (%s)" % esc(items[0].get("type"))) if items[0].get("type") else ""))
                lines.append("   🔀 подгруппы — уточни свою:")
                for lesson in items:
                    who = esc(", ".join(lesson.get("teachers") or [])) or "преподаватель?"
                    rooms = esc(", ".join(lesson.get("rooms") or []))
                    lines.append("      • %s%s" % (who, (" · 🚪 " + rooms) if rooms else ""))
            lines.append("")
    if homework:
        lines.append("📝 <b>Не забудь сдать:</b>")
        for item in homework:
            lines.append("• %s — %s" % (esc(item.get("subject")),
                                        esc(item.get("task") or "без описания")))
    return "\n".join(lines).strip()


def before_lesson_reminder(minutes, lesson):
    """Напоминание перед парой. Принимает и одну пару, и блок подгрупп."""
    items = lesson.get("lessons") if isinstance(lesson, dict) and "lessons" in lesson else [lesson]
    block = lesson if isinstance(lesson, dict) and "lessons" in lesson else {}
    start = block.get("start") or (items[0].get("start") if items else "")
    head = "🔔 <b>Через %d %s%s</b>" % (
        minutes, plural(minutes, "минута", "минуты", "минут"),
        (" — %s" % esc(start)) if start else "")
    body = slot_block({"para": block.get("para"),
                       "start": start,
                       "end": block.get("end"),
                       "lessons": items}) if len(items) > 1 else lesson_block(items[0])
    return head + "\n" + body


def homework_deadline_reminder(items):
    lines = ["📝 <b>Сегодня сдать:</b>", ""]
    for item in items:
        lines.append("• <b>%s</b> — %s" % (esc(item.get("subject")),
                                           esc(item.get("task") or "без описания")))
        if item.get("due_time"):
            lines.append("   ⏰ к %s" % esc(item["due_time"]))
    return "\n".join(lines)


# -------------------------------------------------------------------- настройки

def settings_screen(user, subgroups=None):
    group = user.get("group_title") or "не выбрана"
    subgroup = user.get("subgroup") or ""
    if subgroup:
        group += " (%s)" % subgroup
    tz = int(user.get("tz_offset") or 3)
    return ("⚙️ <b>Настройки</b>\n\n"
            "🎓 Группа: <b>%s</b>\n"
            "🕒 Часовой пояс: <b>UTC%s%d</b> (Казань — UTC+3)\n"
            "🌙 Вечерний дайджест: <b>%s</b>%s\n"
            "🔔 Перед парой: <b>%s</b>\n"
            "📣 Изменения расписания: <b>%s</b>\n"
            "📚 Дедлайны ДЗ: <b>%s</b>" % (
                esc(group), "+" if tz >= 0 else "", tz,
                "вкл" if user.get("evening_enabled") else "выкл",
                (" в %s" % user.get("evening_time")) if user.get("evening_enabled") else "",
                ("за %d мин" % user["before_minutes"]) if user.get("before_minutes") else "выкл",
                "вкл" if user.get("change_alerts") else "выкл",
                "вкл" if user.get("hw_alerts") else "выкл"))


def help_text(user=None, is_admin=False):
    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "<b>Расписание</b>\n"
        "📅 Сегодня · 📅 Завтра · 🗓 Неделя — пары с временем, аудиторией и "
        "преподавателем. Листай недели кнопками ← →.\n"
        "🔄 Обновить — зайти на сайт расписания прямо сейчас и проверить, "
        "не поменялось ли.\n\n"
        "<b>ДЗ</b>\n"
        "📝 ДЗ → «➕ Добавить ДЗ» — пишешь <code>предмет | задание | срок</code>.\n"
        "Общие задания видны всей группе, личные заметки — только тебе.\n"
        "Кнопка «👍» на задании = «я тоже это записал».\n\n"
        "<b>Напоминания</b>\n"
        "⏰ Напоминания — вечерний дайджест «что завтра», предупреждение перед парой, "
        "сообщения об изменениях расписания и дедлайнах.\n\n"
        "<b>Команды</b>\n"
        "/start — выбрать группу заново\n"
        "/today, /tomorrow, /week — расписание\n"
        "/hw — домашние задания\n"
        "/refresh — проверить актуальность\n"
        "/settings — настройки\n"
        "/whoami — какая группа выбрана\n")
    if is_admin:
        text += ("\n👑 <b>Админ</b>\n"
                 "/admin — панель: обновление групп и кэша, копия базы\n"
                 "/stats — данные: активность, группы, домашка, другие боты\n"
                 "/users — кто пользуется ботом (и /users csv — таблица файлом)\n"
                 "/broadcast — рассылка: всем, активным за 7 дней, по группе или одному\n"
                 "/dm &lt;кому&gt; &lt;текст&gt; — личное сообщение студенту\n"
                 "/broadcasts — история рассылок\n"
                 "/grant <id>, /revoke <id> — выдать или снять права админа")
    return text


def whoami(user):
    user = user or {}
    lines = []
    if user.get("group_title"):
        group = "🎓 Твоя группа: <b>%s</b>" % esc(user["group_title"])
        if user.get("subgroup"):
            group += " (подгруппа %s)" % esc(user["subgroup"])
        lines.append(group)
    else:
        lines.append("Группа не выбрана. Нажми /start.")
    lines.append("🆔 Твой Telegram ID: <code>%s</code>" % esc(user.get("tg_id", "—")))
    if user.get("is_admin"):
        lines.append("👑 Ты админ бота")
    return "\n".join(lines)


# ----------------------------------------------------------------------- админ

def admin_screen(stats, groups_updated="", api_ok=True, backup_note=""):
    return ("👑 <b>Админ-панель</b>\n\n"
            "👥 Пользователей: <b>%(users)d</b>\n"
            "🎓 Групп в кэше: <b>%(groups)d</b> (обновлён: %(groups_updated)s)\n"
            "📚 Групп с подписчиками: <b>%(groups_in_use)d</b>\n"
            "🗓 Недель расписания в кэше: <b>%(timetable_weeks)d</b>\n"
            "📝 Открытых ДЗ: <b>%(homework)d</b>\n"
            "🔔 Изменений за всё время: <b>%(changes)d</b>\n"
            "💾 Резервные копии базы: <b>%(backup_note)s</b>\n\n"
            "API расписания: <b>%(api_ok)s</b>" % dict(
                stats, groups_updated=groups_updated or "никогда",
                backup_note=backup_note or "не настроены",
                api_ok="доступен ✅" if api_ok else "недоступен ❌"))


def admin_broadcast_done(sent, failed, target_title=""):
    where = " (%s)" % esc(target_title) if target_title else ""
    return "📣 Рассылка%s завершена: доставлено %d, ошибок %d" % (where, sent, failed)


def admin_stats_page(summary, bot_title="", provider_ok=True):
    """Подробная сводка по боту: активность, группы, домашка, другие боты."""
    summary = summary or {}
    lines = [
        "📊 <b>Данные по боту%s</b>" % ((" «%s»" % esc(bot_title)) if bot_title else ""),
        "",
        "👥 Всего пользователей: <b>%(total)d</b>" % summary,
        "🎓 Выбрали группу: <b>%(with_group)d</b>" % summary,
        "🌙 В тихом режиме: <b>%(quiet)d</b>" % summary,
        "",
        "<b>Активность</b>",
        "• сегодня заходили: <b>%(active_today)d</b>" % summary,
        "• за 7 дней: <b>%(active_week)d</b>" % summary,
        "• новых сегодня: <b>%(new_today)d</b>" % summary,
        "• новых за 7 дней: <b>%(new_week)d</b>" % summary,
        "• сообщений всего: <b>%(messages)d</b>" % summary,
        "",
        "<b>Домашка</b>",
        "• общих заданий группы: <b>%(homework_group)d</b>" % summary,
        "• личных заметок: <b>%(homework_personal)d</b>" % summary,
        "• открытых: <b>%(homework_open)d</b>" % summary,
    ]
    top = summary.get("top_groups") or []
    if top:
        lines.append("")
        lines.append("<b>Группы</b>")
        for item in top[:10]:
            lines.append("• %s — %d" % (esc(item.get("group", "")), item.get("users", 0)))
    others = summary.get("others") or []
    if others:
        lines.append("")
        lines.append("<b>Другие боты в этом сервисе</b>")
        for item in others:
            if item.get("ok"):
                lines.append("• %s — пользователей %d, заходили сегодня %d"
                             % (esc(item.get("title") or item.get("bot") or "бот"),
                                item.get("users", 0), item.get("active_today", 0)))
            else:
                lines.append("• %s — недоступен (%s)"
                             % (esc(item.get("title") or item.get("bot") or "бот"),
                                esc(str(item.get("error") or "")[:60])))
    if not provider_ok:
        lines.append("")
        lines.append("⚠️ Источник расписания сейчас недоступен — данные могли устареть.")
    return "\n".join(lines)


def users_page(rows, page, pages, total, bot_title="", query=""):
    """Список пользователей бота (кто, группа, когда заходил, сколько писал)."""
    import report as report_module

    header = "👥 <b>Пользователи%s</b> — всего %d\n" % (
        (" «%s»" % esc(bot_title)) if bot_title else "", total)
    if query:
        header += "Поиск: <b>%s</b>\n" % esc(query)
    if pages > 1:
        header += "Страница %d из %d\n" % (page + 1, pages)
    header += "\n"
    if not rows:
        return header + "Пока никого. Как только студенты начнут писать боту — появятся здесь."
    lines = []
    for index, row in enumerate(rows, start=1 + page * 10):
        name = esc(row.get("name") or "без имени")
        username = (" @" + esc(row["username"])) if row.get("username") else ""
        group = esc(row.get("group") or "группа не выбрана")
        if row.get("subgroup"):
            group += " (%s)" % esc(row["subgroup"])
        flags = []
        if row.get("is_admin"):
            flags.append("👑")
        if row.get("quiet"):
            flags.append("🌙")
        lines.append("%d. <b>%s</b>%s %s" % (index, name, username, "".join(flags)))
        lines.append("   🎓 %s · %s" % (group, report_module.format_last_seen(row.get("last_seen"))))
        lines.append("   💬 %d · 📝 %d" % (row.get("messages") or 0, row.get("homework") or 0))
    return header + "\n".join(lines)


def broadcast_targets(counts, groups):
    """Экран выбора, кому отправлять сообщение."""
    lines = [
        "📣 <b>Рассылка от админа</b>",
        "",
        "Кому отправить сообщение? Оно придёт в чат с ботом от его имени — "
        "студенты увидят текст и поймут, что это объявление.",
        "",
        "• всем: <b>%d</b> чел." % counts.get("all", 0),
        "• активным за 7 дней: <b>%d</b> чел." % counts.get("active", 0),
        "• 👤 одному студенту — по имени, @username или ID",
    ]
    if groups:
        lines.append("• по группе: выбери ниже")
    lines.append("")
    lines.append("<i>Дальше пришли текст сообщения — покажу предпросмотр и спрошу подтверждение.</i>")
    return "\n".join(lines)


def broadcast_preview(text, count, target_title=""):
    where = (" (%s)" % esc(target_title)) if target_title else ""
    return ("📣 <b>Предпросмотр рассылки</b>%s\n"
            "Получателей: <b>%d</b>\n\n"
            "—————\n%s\n—————\n\n"
            "Отправляем?" % (where, count, text))


def broadcasts_history(rows, bot_title=""):
    if not rows:
        return "🕓 Рассылок пока не было."
    lines = ["🕓 <b>История рассылок%s</b>\n" % (
        (" «%s»" % esc(bot_title)) if bot_title else "")]
    for row in rows:
        preview = (row.get("text") or "").replace("\n", " ")[:60]
        lines.append("• %s — %s: %d доставлено, %d ошибок\n   <i>%s</i>"
                     % (esc(str(row.get("created_at") or "")[:16]),
                        esc(row.get("target_title") or row.get("target") or "всем"),
                        row.get("delivered") or 0, row.get("failed") or 0, esc(preview)))
    return "\n".join(lines)


def error_generic(detail=""):
    text = ("😔 Что-то пошло не так. Попробуй ещё раз через минуту.")
    if detail:
        text += "\n\n<code>%s</code>" % esc(detail[:300])
    return text
