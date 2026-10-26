"""Inline-календарь Telegram для выбора даты и диапазона."""

from __future__ import annotations

import calendar
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


MONTHS_RU = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def build_calendar(
    mode: str,
    shown_month: date,
    *,
    min_date: date,
    max_date: date,
    selected_start: date | None = None,
) -> InlineKeyboardMarkup:
    """Создаёт календарь; mode используется в callback_data."""
    month_start = shown_month.replace(day=1)
    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"{MONTHS_RU[month_start.month]} {month_start.year}", callback_data="cal:noop")],
        [InlineKeyboardButton(day, callback_data="cal:noop") for day in WEEKDAYS_RU],
    ]

    for week in calendar.monthcalendar(month_start.year, month_start.month):
        row = []
        for day_number in week:
            if not day_number:
                row.append(InlineKeyboardButton(" ", callback_data="cal:noop"))
                continue

            day = date(month_start.year, month_start.month, day_number)
            selectable = min_date <= day <= max_date
            if not selectable:
                row.append(InlineKeyboardButton("·", callback_data="cal:noop"))
                continue

            label = f"🔹{day_number}" if day == selected_start else str(day_number)
            row.append(
                InlineKeyboardButton(label, callback_data=f"cal:day:{mode}:{day.isoformat()}")
            )
        keyboard.append(row)

    previous_month = shift_month(month_start, -1)
    next_month = shift_month(month_start, 1)
    previous_callback = (
        f"cal:nav:{mode}:{previous_month.strftime('%Y-%m')}"
        if shift_month(month_start, 0) > min_date.replace(day=1)
        else "cal:noop"
    )
    next_callback = (
        f"cal:nav:{mode}:{next_month.strftime('%Y-%m')}"
        if next_month <= max_date.replace(day=1)
        else "cal:noop"
    )
    keyboard.append(
        [
            InlineKeyboardButton("‹", callback_data=previous_callback),
            InlineKeyboardButton("Отмена", callback_data="confirm_cancel"),
            InlineKeyboardButton("›", callback_data=next_callback),
        ]
    )
    return InlineKeyboardMarkup(keyboard)
