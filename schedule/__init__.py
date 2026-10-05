# -*- coding: utf-8 -*-
"""Пакет работы с расписанием Поволжского ГУФКСиТ.

Модули:
    unifirst — клиент публичного API api.unifirst.ru и нормализация данных.

Пример:
    from schedule.unifirst import Unifirst, iso_year_week, normalize
    api = Unifirst()
    groups = api.groups()
    year, week = iso_year_week(date.today())
    lessons = normalize(api.timetable("26281", year, week))
"""

__all__ = ["unifirst"]
