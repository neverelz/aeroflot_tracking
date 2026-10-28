"""Telegram-бот для оформления заявок на поиск рейсов Аэрофлота."""

from __future__ import annotations

import logging
import os
import re
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from airport_catalog import BY_CODE, Airport, find_airports
from calendar_ui import build_calendar
from itinerary import minutes_text
from task_store import MAX_RANGE_DAYS, TaskStore, dates_between


BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
TASKS_FILE = DATA_DIR / "tasks.json"

(
    ORIGIN,
    ORIGIN_SELECT,
    DESTINATION,
    DESTINATION_SELECT,
    DATE_MODE,
    SINGLE_DATE,
    RANGE_START,
    RANGE_END,
    MAX_STOPS,
    CONNECTION_MODE,
    CONNECTION_MIN,
    CONNECTION_MAX,
    THRESHOLD,
    CONFIRM,
    EDIT_MENU,
) = range(15)

DATE_FORMAT = "%d.%m.%Y"
logger = logging.getLogger(__name__)
store = TaskStore(TASKS_FILE)

BUTTON_NEW = "✈️ Создать заявку"
BUTTON_REQUESTS = "📋 Мои заявки"
BUTTON_AIRPORTS = "🛫 Аэропорты"
BUTTON_HELP = "ℹ️ Помощь"
BUTTON_CANCEL = "❌ Отменить"

# Кнопка отмены является обычным текстовым сообщением. Исключаем её из полей
# ввода, чтобы ConversationHandler передал сообщение в fallback `cancel`.
TEXT_INPUT = (
    filters.TEXT
    & ~filters.COMMAND
    & ~filters.Regex(f"^{re.escape(BUTTON_CANCEL)}$")
)

# Диалог намеренно чередует обычные сообщения и inline-кнопки и должен
# отслеживаться на уровне пользователя/чата. Предупреждение per_message для
# такого смешанного сценария не означает ошибку и только засоряет лог.
warnings.filterwarnings(
    "ignore",
    message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message\..*",
    category=PTBUserWarning,
)

MAIN_MENU = ReplyKeyboardMarkup(
    [[BUTTON_NEW, BUTTON_REQUESTS], [BUTTON_AIRPORTS, BUTTON_HELP]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите действие",
)
CANCEL_MENU = ReplyKeyboardMarkup(
    [[BUTTON_CANCEL]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Введите ответ или отмените заявку",
)


def resolve_airport(value: str) -> str | None:
    matches = find_airports(value, limit=1)
    return matches[0].code if matches else None


def parse_future_date(value: str, *, today: date | None = None) -> date:
    try:
        parsed = datetime.strptime(value.strip(), DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ, например 14.08.2027.") from exc

    current_date = today or date.today()
    if parsed < current_date:
        raise ValueError("Дата рейса не может быть в прошлом.")
    return parsed


def parse_threshold(value: str) -> int:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        raise ValueError("Введите сумму цифрами, например 15000.")
    threshold = int(digits)
    if not 1_000 <= threshold <= 1_000_000:
        raise ValueError("Укажите сумму от 1 000 до 1 000 000 ₽.")
    return threshold


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_dates(dates: list[date]) -> str:
    if len(dates) == 1:
        return dates[0].strftime(DATE_FORMAT)
    return f"{dates[0].strftime(DATE_FORMAT)} — {dates[-1].strftime(DATE_FORMAT)} ({len(dates)} дн.)"


def parse_connection_duration(value: str) -> int:
    """Разбирает минуты, Ч:ММ или запись вида «2 ч 30 мин»."""
    normalized = value.strip().lower().replace(",", ".")
    colon_match = re.fullmatch(r"(\d{1,2}):([0-5]\d)", normalized)
    if colon_match:
        minutes = int(colon_match.group(1)) * 60 + int(colon_match.group(2))
    else:
        text_match = re.fullmatch(
            r"(?:(\d+(?:\.\d+)?)\s*(?:ч|час(?:а|ов)?))?\s*"
            r"(?:(\d+)\s*(?:м|мин(?:ут[аы]?)?))?",
            normalized,
        )
        if text_match and any(text_match.groups()):
            minutes = round(float(text_match.group(1) or 0) * 60) + int(text_match.group(2) or 0)
        elif normalized.isdigit():
            minutes = int(normalized)
        else:
            raise ValueError("Введите время в минутах или как Ч:ММ, например 90 или 1:30.")
    if not 0 <= minutes <= 1440:
        raise ValueError("Стыковка должна быть не дольше 24 часов.")
    return minutes


def connections_text(item: dict) -> str:
    max_stops = int(item.get("max_stops", 0) or 0)
    if max_stops == 0:
        return "только прямые рейсы"
    minimum = item.get("min_connection_minutes")
    maximum = item.get("max_connection_minutes")
    if isinstance(minimum, int) and isinstance(maximum, int):
        return (
            f"до {max_stops} пересадок, стыковка "
            f"{minutes_text(minimum)}–{minutes_text(maximum)}"
        )
    return f"до {max_stops} пересадок"


def airport_keyboard(airports: list[Airport], field: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(airport.label, callback_data=f"airport:{field}:{airport.code}")]
        for airport in airports
    ]
    return InlineKeyboardMarkup(rows)


def date_limits() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=730)


def calendar_markup(mode: str, month: date, *, start: date | None = None) -> InlineKeyboardMarkup:
    minimum, maximum = date_limits()
    if mode == "end" and start:
        minimum = start
        maximum = min(maximum, start + timedelta(days=MAX_RANGE_DAYS - 1))
    return build_calendar(
        mode,
        month,
        min_date=minimum,
        max_date=maximum,
        selected_start=start,
    )


def request_summary(item: dict) -> str:
    threshold = item.get("threshold")
    threshold_text = format_money(threshold) if isinstance(threshold, int) else "не указан"
    return (
        f"✈️ {item['origin']} → {item['dest']}\n"
        f"📅 {_request_dates_text(item['dates'])}\n"
        f"🔄 {connections_text(item)}\n"
        f"🎯 До {threshold_text} ₽\n"
        f"Номер: #{item['request_id']}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Привет! Я помогу создать заявку на отслеживание цены рейса Аэрофлота.\n\n"
        "Выберите нужное действие в меню ниже.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/new — создать заявку\n"
        "/requests — посмотреть, изменить или удалить свои заявки\n"
        "/airports — примеры городов и IATA-кодов\n"
        "/menu — показать главное меню\n"
        "/cancel — отменить заполнение\n"
        "/help — эта справка",
        reply_markup=MAIN_MENU,
    )


async def airports_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Напишите название города — бот предложит только доступные в каталоге "
        "направлений Аэрофлота варианты и правильные IATA-коды. "
        "Произвольные коды не принимаются.",
        reply_markup=MAIN_MENU,
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    query = update.callback_query
    text = (
        "Откуда летите? Напишите название города, например Москва или Сочи. "
        "Я предложу доступные варианты."
    )
    if query:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(text, reply_markup=CANCEL_MENU)
    else:
        await update.message.reply_text(text, reply_markup=CANCEL_MENU)
    return ORIGIN


async def receive_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    airports = find_airports(update.message.text)
    if not airports:
        await update.message.reply_text(
            "Не нашёл такого направления в каталоге Аэрофлота. "
            "Проверьте написание или введите начало названия, например Москва или Сочи."
        )
        return ORIGIN
    await update.message.reply_text(
        "Выберите подходящий аэропорт:",
        reply_markup=airport_keyboard(airports, "origin"),
    )
    return ORIGIN_SELECT


async def receive_airport_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, field, code = query.data.split(":", 2)
    airport = BY_CODE.get(code)
    if not airport:
        await query.answer()
        await query.edit_message_text("Этот аэропорт больше недоступен. Начните заново: /new")
        return ConversationHandler.END

    if field == "origin":
        await query.answer()
        context.user_data["origin"] = code
        await query.edit_message_text(f"Выбрано: {airport.label}")
        await query.message.reply_text(
            f"Откуда: {airport.city} ({code}). Теперь начните вводить город назначения, "
            "например Сочи или Краснодар. Я предложу варианты."
        )
        return DESTINATION

    if code == context.user_data.get("origin"):
        await query.answer("Аэропорты вылета и прилёта должны отличаться.", show_alert=True)
        return DESTINATION_SELECT

    await query.answer()
    context.user_data["dest"] = code
    await query.edit_message_text(f"Выбрано: {airport.label}")
    if context.user_data.get("operation") == "edit_route":
        return await save_request_edit(update, context, origin=context.user_data["origin"], dest=code)

    await query.message.reply_text(
        f"Маршрут: {context.user_data['origin']} → {code}. Как выбрать даты?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Одна дата", callback_data="date_single"),
                InlineKeyboardButton("Диапазон дат", callback_data="date_range"),
            ]]
        ),
    )
    return DATE_MODE


async def receive_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    airports = find_airports(update.message.text)
    if not airports:
        await update.message.reply_text(
            "Не нашёл такого направления в каталоге Аэрофлота. "
            "Проверьте написание или введите начало названия города."
        )
        return DESTINATION
    await update.message.reply_text(
        "Выберите аэропорт назначения:",
        reply_markup=airport_keyboard(airports, "dest"),
    )
    return DESTINATION_SELECT


async def choose_date_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    today, _ = date_limits()
    if query.data == "date_single":
        await query.edit_message_text(
            "Выберите дату вылета:",
            reply_markup=calendar_markup("single", today),
        )
        return SINGLE_DATE
    await query.edit_message_text(
        f"Выберите первую дату диапазона (максимум {MAX_RANGE_DAYS} дней):",
        reply_markup=calendar_markup("start", today),
    )
    return RANGE_START


async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    if action == "noop":
        return None

    mode = parts[2]
    states = {"single": SINGLE_DATE, "start": RANGE_START, "end": RANGE_END}
    if action == "nav":
        shown_month = datetime.strptime(parts[3], "%Y-%m").date()
        await query.edit_message_reply_markup(
            reply_markup=calendar_markup(
                mode,
                shown_month,
                start=context.user_data.get("range_start"),
            )
        )
        return states[mode]

    selected = date.fromisoformat(parts[3])
    if mode == "single":
        context.user_data["dates"] = [selected]
        await query.edit_message_text(f"Дата: {selected.strftime(DATE_FORMAT)}")
        if context.user_data.get("operation") == "edit_dates":
            return await save_request_edit(update, context, dates=[selected])
        return await ask_max_stops(update)

    if mode == "start":
        context.user_data["range_start"] = selected
        await query.edit_message_text(
            f"Начало: {selected.strftime(DATE_FORMAT)}. Выберите последнюю дату:",
            reply_markup=calendar_markup("end", selected, start=selected),
        )
        return RANGE_END

    selected_dates = dates_between(context.user_data["range_start"], selected)
    context.user_data["dates"] = selected_dates
    await query.edit_message_text(f"Даты: {format_dates(selected_dates)}")
    if context.user_data.get("operation") == "edit_dates":
        return await save_request_edit(update, context, dates=selected_dates)
    return await ask_max_stops(update)


async def receive_single_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        flight_date = parse_future_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return SINGLE_DATE
    context.user_data["dates"] = [flight_date]
    if context.user_data.get("operation") == "edit_dates":
        return await save_request_edit(update, context, dates=[flight_date])
    return await ask_max_stops(update)


async def receive_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        start_date = parse_future_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return RANGE_START
    context.user_data["range_start"] = start_date
    await update.message.reply_text(
        f"Начало: {start_date.strftime(DATE_FORMAT)}. Теперь введите последнюю дату диапазона."
    )
    return RANGE_END


async def receive_range_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        end_date = parse_future_date(update.message.text)
        context.user_data["dates"] = dates_between(context.user_data["range_start"], end_date)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return RANGE_END
    if context.user_data.get("operation") == "edit_dates":
        return await save_request_edit(update, context, dates=context.user_data["dates"])
    return await ask_max_stops(update)


async def ask_max_stops(update: Update) -> int:
    await update.effective_message.reply_text(
        "Сколько пересадок максимально допустимо?\n"
        "Промежуточные города программа подберёт автоматически.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Только прямые", callback_data="stops:0"),
                InlineKeyboardButton("До 1", callback_data="stops:1"),
                InlineKeyboardButton("До 2", callback_data="stops:2"),
            ]]
        ),
    )
    return MAX_STOPS


async def choose_max_stops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    max_stops = int(query.data.split(":", 1)[1])
    context.user_data["max_stops"] = max_stops
    if max_stops == 0:
        context.user_data.update(
            connection_mode="direct",
            min_connection_minutes=None,
            max_connection_minutes=None,
        )
        await query.edit_message_text("Пересадки: только прямые рейсы.")
        return await finish_connection_settings(update, context)

    await query.edit_message_text(
        "Какие стыковки считать подходящими?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    "Удобные: 1:30–5:00",
                    callback_data="connection:comfortable",
                )],
                [InlineKeyboardButton(
                    "Любые варианты до 12 часов",
                    callback_data="connection:flexible",
                )],
                [InlineKeyboardButton(
                    "Настроить время",
                    callback_data="connection:custom",
                )],
            ]
        ),
    )
    return CONNECTION_MODE


async def choose_connection_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["connection_mode"] = mode
    if mode == "comfortable":
        context.user_data["min_connection_minutes"] = 90
        context.user_data["max_connection_minutes"] = 300
        await query.edit_message_text("Стыковки: удобные, от 1 ч 30 мин до 5 ч.")
        return await finish_connection_settings(update, context)
    if mode == "flexible":
        context.user_data["min_connection_minutes"] = 0
        context.user_data["max_connection_minutes"] = 720
        await query.edit_message_text("Стыковки: любые варианты Аэрофлота до 12 ч.")
        return await finish_connection_settings(update, context)

    await query.edit_message_text("Настройка времени стыковки.")
    await query.message.reply_text(
        "Введите минимальное время стыковки в минутах или как Ч:ММ, например 90 или 1:30.",
        reply_markup=CANCEL_MENU,
    )
    return CONNECTION_MIN


async def receive_connection_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        minimum = parse_connection_duration(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return CONNECTION_MIN
    context.user_data["min_connection_minutes"] = minimum
    await update.message.reply_text(
        f"Минимум: {minutes_text(minimum)}. Теперь введите максимальное время, например 5:00."
    )
    return CONNECTION_MAX


async def receive_connection_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        maximum = parse_connection_duration(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return CONNECTION_MAX
    minimum = context.user_data["min_connection_minutes"]
    if maximum < minimum:
        await update.message.reply_text("Максимальное время не может быть меньше минимального.")
        return CONNECTION_MAX
    context.user_data["max_connection_minutes"] = maximum
    await update.message.reply_text(
        f"Стыковки: от {minutes_text(minimum)} до {minutes_text(maximum)}."
    )
    return await finish_connection_settings(update, context)


async def finish_connection_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("operation") == "edit_connections":
        return await save_request_edit(
            update,
            context,
            max_stops=context.user_data["max_stops"],
            connection_mode=context.user_data["connection_mode"],
            min_connection_minutes=context.user_data.get("min_connection_minutes"),
            max_connection_minutes=context.user_data.get("max_connection_minutes"),
        )
    return await ask_threshold(update)


async def ask_threshold(update: Update) -> int:
    await update.effective_message.reply_text(
        "При какой цене прислать уведомление? Введите сумму в рублях, например 15000."
    )
    return THRESHOLD


async def receive_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["threshold"] = parse_threshold(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return THRESHOLD

    if context.user_data.get("operation") == "edit_threshold":
        return await save_request_edit(
            update,
            context,
            threshold=context.user_data["threshold"],
        )

    await update.message.reply_text(
        "Проверьте заявку:\n\n"
        f"✈️ {context.user_data['origin']} → {context.user_data['dest']}\n"
        f"📅 {format_dates(context.user_data['dates'])}\n"
        f"🔄 {connections_text(context.user_data)}\n"
        f"🎯 Цена до {format_money(context.user_data['threshold'])} ₽",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Сохранить", callback_data="confirm_save"),
                InlineKeyboardButton("❌ Отмена", callback_data="confirm_cancel"),
            ]]
        ),
    )
    return CONFIRM


async def confirm_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_cancel":
        context.user_data.clear()
        await query.edit_message_text("Создание заявки отменено. Чтобы начать заново: /new")
        await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    try:
        request_id = store.add_request(
            chat_id=update.effective_chat.id,
            origin=context.user_data["origin"],
            dest=context.user_data["dest"],
            dates=context.user_data["dates"],
            threshold=context.user_data["threshold"],
            max_stops=context.user_data["max_stops"],
            connection_mode=context.user_data["connection_mode"],
            min_connection_minutes=context.user_data.get("min_connection_minutes"),
            max_connection_minutes=context.user_data.get("max_connection_minutes"),
        )
    except (OSError, RuntimeError, ValueError):
        logger.exception("Не удалось сохранить заявку")
        await query.edit_message_text("Не удалось сохранить заявку. Попробуйте ещё раз позже.")
        await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    dates_text = format_dates(context.user_data["dates"])
    context.user_data.clear()
    await query.edit_message_text(
        f"✅ Заявка #{request_id} сохранена.\n"
        f"Буду проверять рейс на даты {dates_text} и сообщу, когда цена станет ниже порога."
    )
    await query.message.reply_text("Что хотите сделать дальше?", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    request_id = query.data.split(":", 1)[1]
    try:
        item = store.get_request(update.effective_chat.id, request_id)
    except RuntimeError:
        logger.exception("Не удалось загрузить заявку %s", request_id)
        await query.edit_message_text("Не удалось загрузить заявку. Попробуйте позже.")
        return ConversationHandler.END
    if not item:
        await query.edit_message_text("Заявка уже удалена или вам не принадлежит.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["editing_request_id"] = request_id
    await query.edit_message_text(
        f"Что изменить?\n\n{request_summary(item)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✈️ Маршрут", callback_data="editfield:route"),
                    InlineKeyboardButton("📅 Даты", callback_data="editfield:dates"),
                ],
                [
                    InlineKeyboardButton("🔄 Пересадки", callback_data="editfield:connections"),
                    InlineKeyboardButton("🎯 Порог цены", callback_data="editfield:threshold"),
                ],
                [InlineKeyboardButton("Закрыть", callback_data="edit_cancel")],
            ]
        ),
    )
    return EDIT_MENU


async def choose_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    if field == "route":
        context.user_data["operation"] = "edit_route"
        await query.edit_message_text("Изменение маршрута")
        await query.message.reply_text(
            "Напишите новый город вылета.",
            reply_markup=CANCEL_MENU,
        )
        return ORIGIN
    if field == "dates":
        context.user_data["operation"] = "edit_dates"
        await query.edit_message_text(
            "Как выбрать новые даты?",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("Одна дата", callback_data="date_single"),
                    InlineKeyboardButton("Диапазон дат", callback_data="date_range"),
                ]]
            ),
        )
        return DATE_MODE

    if field == "connections":
        context.user_data["operation"] = "edit_connections"
        await query.edit_message_text("Изменение настроек пересадок")
        return await ask_max_stops(update)

    context.user_data["operation"] = "edit_threshold"
    await query.edit_message_text("Изменение порога цены")
    await query.message.reply_text(
        "Введите новый порог в рублях, например 15000.",
        reply_markup=CANCEL_MENU,
    )
    return THRESHOLD


async def save_request_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    **changes,
) -> int:
    request_id = context.user_data.get("editing_request_id")
    try:
        updated = bool(request_id) and store.update_request(
            update.effective_chat.id,
            request_id,
            **changes,
        )
    except (OSError, RuntimeError, ValueError):
        logger.exception("Не удалось изменить заявку %s", request_id)
        updated = False

    context.user_data.clear()
    text = (
        f"✅ Заявка #{request_id} обновлена."
        if updated
        else "Не удалось обновить заявку: возможно, она уже удалена."
    )
    await update.effective_message.reply_text(text, reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Действие отменено.")
    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Заполнение отменено. Выберите следующее действие.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


def _request_dates_text(raw_dates: list[str]) -> str:
    parsed = []
    for value in raw_dates:
        try:
            parsed.append(datetime.strptime(value, DATE_FORMAT).date())
        except ValueError:
            pass
    parsed.sort()
    if not parsed:
        return ", ".join(raw_dates)
    return format_dates(parsed)


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        requests = store.requests_for_chat(update.effective_chat.id)
    except RuntimeError:
        logger.exception("Не удалось прочитать заявки")
        await update.message.reply_text(
            "Не удалось загрузить заявки. Попробуйте позже.",
            reply_markup=MAIN_MENU,
        )
        return

    if not requests:
        await update.message.reply_text(
            "У вас пока нет заявок. Нажмите «Создать заявку».",
            reply_markup=MAIN_MENU,
        )
        return

    await update.message.reply_text(f"Ваши заявки: {len(requests)}", reply_markup=MAIN_MENU)
    for item in requests:
        await update.message.reply_text(
            request_summary(item),
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=f"edit:{item['request_id']}",
                    ),
                    InlineKeyboardButton(
                        "🗑 Удалить",
                        callback_data=f"delete:{item['request_id']}",
                    ),
                ]]
            ),
        )


async def delete_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = query.data.split(":", 1)[1]
    try:
        deleted = store.delete_request(update.effective_chat.id, request_id)
    except RuntimeError:
        logger.exception("Не удалось удалить заявку")
        await query.edit_message_text("Не удалось удалить заявку. Попробуйте позже.")
        return
    if deleted:
        logger.info("Удалена заявка %s (%s дат)", request_id, deleted)
        await query.edit_message_text(
            f"Заявка #{request_id} удалена из очереди.\n"
            "Если одна проверка уже открыла страницу, она может завершиться; остальные даты будут пропущены."
        )
    else:
        await query.edit_message_text("Заявка уже удалена или вам не принадлежит.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке Telegram-события", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Не удалось обработать сообщение. Попробуйте отправить его ещё раз "
                "или нажмите «Отменить».",
                reply_markup=CANCEL_MENU,
            )
        except Exception:
            logger.exception("Не удалось сообщить пользователю об ошибке")


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_request),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_NEW)}$"), new_request),
            CallbackQueryHandler(new_request, pattern="^new_request$"),
            CallbackQueryHandler(start_edit, pattern=r"^edit:[a-zA-Z0-9-]+$"),
        ],
        states={
            ORIGIN: [MessageHandler(TEXT_INPUT, receive_origin)],
            ORIGIN_SELECT: [
                CallbackQueryHandler(receive_airport_selection, pattern=r"^airport:origin:[A-Z]{3}$")
            ],
            DESTINATION: [MessageHandler(TEXT_INPUT, receive_destination)],
            DESTINATION_SELECT: [
                CallbackQueryHandler(receive_airport_selection, pattern=r"^airport:dest:[A-Z]{3}$")
            ],
            DATE_MODE: [CallbackQueryHandler(choose_date_mode, pattern="^date_(single|range)$")],
            SINGLE_DATE: [
                CallbackQueryHandler(handle_calendar, pattern=r"^cal:"),
                MessageHandler(TEXT_INPUT, receive_single_date),
            ],
            RANGE_START: [
                CallbackQueryHandler(handle_calendar, pattern=r"^cal:"),
                MessageHandler(TEXT_INPUT, receive_range_start),
            ],
            RANGE_END: [
                CallbackQueryHandler(handle_calendar, pattern=r"^cal:"),
                MessageHandler(TEXT_INPUT, receive_range_end),
            ],
            MAX_STOPS: [CallbackQueryHandler(choose_max_stops, pattern=r"^stops:[012]$")],
            CONNECTION_MODE: [
                CallbackQueryHandler(
                    choose_connection_mode,
                    pattern=r"^connection:(comfortable|flexible|custom)$",
                )
            ],
            CONNECTION_MIN: [MessageHandler(TEXT_INPUT, receive_connection_min)],
            CONNECTION_MAX: [MessageHandler(TEXT_INPUT, receive_connection_max)],
            THRESHOLD: [MessageHandler(TEXT_INPUT, receive_threshold)],
            CONFIRM: [CallbackQueryHandler(confirm_request, pattern="^confirm_(save|cancel)$")],
            EDIT_MENU: [CallbackQueryHandler(choose_edit_field, pattern=r"^editfield:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("menu", start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_CANCEL)}$"), cancel),
            CallbackQueryHandler(cancel_callback, pattern=r"^(confirm_cancel|edit_cancel)$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("airports", airports_command))
    application.add_handler(CommandHandler("requests", list_requests))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BUTTON_REQUESTS)}$"), list_requests))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BUTTON_AIRPORTS)}$"), airports_command))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BUTTON_HELP)}$"), help_command))
    application.add_handler(CallbackQueryHandler(delete_request, pattern=r"^delete:[a-zA-Z0-9-]+$"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN в файле .env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx логирует Telegram API URL вместе с токеном на уровне INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logger.info("Telegram-бот запущен")
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
