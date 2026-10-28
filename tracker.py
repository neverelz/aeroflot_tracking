"""
Многопользовательский трекер цен на авиабилеты Аэрофлота.
Берет задания из tasks.json.

Запуск:
  python tracker.py          — постоянная работа (по расписанию)
  python tracker.py --test   — один прогон всех заданий без ожидания
"""

import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telegram import Bot
from telegram.error import TelegramError

from itinerary import (
    FlightOffer,
    cheapest_matching_offer,
    minutes_text,
    parse_offer_text,
)
from task_store import contains_task

# ── Настройки ──────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "3"))
HEADLESS           = os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}
CHROMIUM_EXECUTABLE_PATH = os.getenv("CHROMIUM_EXECUTABLE_PATH", "").strip()

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CSV_FILE = DATA_DIR / "prices.csv"
TASKS_FILE = DATA_DIR / "tasks.json"
LOG_FILE = DATA_DIR / "tracker.log"

# ── Логирование ────────────────────────────────────────────────────────────────
# Docker-лог в stdout обязателен, а файловый лог — дополнительный. Примонтированный
# каталог может принадлежать другому UID или быть защищён SELinux, и это не должно
# останавливать трекер до запуска.
log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
file_log_error: OSError | None = None
try:
    log_handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
except OSError as exc:
    file_log_error = exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers,
)
log = logging.getLogger(__name__)
if file_log_error is not None:
    log.warning(
        "Нет доступа к файловому логу %s (%s). Продолжаем с логами Docker.",
        LOG_FILE,
        file_log_error,
    )
# httpx включает токен Telegram в URL на уровне INFO — не пишем такие строки в лог.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ── Работа с заданиями (JSON) ──────────────────────────────────────────────────
def load_tasks() -> list[dict]:
    """Загружает список задач для проверки из JSON файла."""
    if not TASKS_FILE.exists():
        log.warning("Файл %s пока не создан — ожидаем первую заявку из бота.", TASKS_FILE)
        return []

    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            tasks = json.load(f)
            return tasks
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Ошибка чтения %s: %s. Повторим при следующей проверке.", TASKS_FILE, exc)
        return []


def generate_aeroflot_url(origin: str, dest: str, date_str: str) -> str:
    """Преобразует дату DD.MM.YYYY в формат YYYYMMDD и собирает ссылку."""
    date_obj = datetime.strptime(date_str, "%d.%m.%Y")
    url_date = date_obj.strftime("%Y%m%d")
    return (
        "https://www.aeroflot.ru/ru-ru/sb/search"
        f"?adults=1&infants=0&children=0&childrenfrgn=0&childrenaward=0"
        f"&cabin=economy&routes={origin}.{url_date}.{dest}"
    )


# ── CSV-хранилище ──────────────────────────────────────────────────────────────
def init_csv() -> None:
    """Создаёт CSV с заголовком, если файл не существует."""
    if not CSV_FILE.exists():
        try:
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                # Добавили chat_id и маршрут, чтобы не путать данные разных пользователей
                writer.writerow(["timestamp", "chat_id", "route", "date", "price_rub", "url", "note"])
            log.info("Создан файл %s", CSV_FILE)
        except OSError as exc:
            log.warning("Не удалось создать %s: %s. Работаем без истории CSV.", CSV_FILE, exc)


def save_to_csv(task: dict, search_url: str, price: int | None, note: str = "") -> None:
    """Сохраняет запись в CSV."""
    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                task.get("chat_id"),
                f"{task.get('origin')}->{task.get('dest')}",
                task.get("date"),
                price if price is not None else "N/A",
                search_url,
                note,
            ])
    except OSError as exc:
        log.warning("Не удалось записать историю в %s: %s", CSV_FILE, exc)


# ── Telegram ───────────────────────────────────────────────────────────────────
async def send_telegram(chat_id: str, text: str) -> None:
    """Отправляет сообщение конкретному пользователю."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Пропуск отправки: не указан токен или chat_id")
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
        )
        log.info(f"Уведомление отправлено пользователю {chat_id}")
    except TelegramError as e:
        log.error("Ошибка Telegram для %s: %s", chat_id, e)


# ── Парсинг страницы ───────────────────────────────────────────────────────────
async def _extract_min_price(page) -> int | None:
    """Извлекает минимальную цену из КАРТОЧЕК рейсов (надежный метод)."""
    price_list = await page.evaluate(r"""
        () => {
            const results = [];
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT,
                {
                    acceptNode(node) {
                        const p = node.parentElement;
                        if (!p) return NodeFilter.FILTER_REJECT;
                        const rect = p.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return NodeFilter.FILTER_REJECT;
                        const style = window.getComputedStyle(p);
                        if (style.display === 'none' || style.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
                        return NodeFilter.FILTER_ACCEPT;
                    }
                },
                false
            );

            let node;
            while ((node = walker.nextNode())) {
                const text = node.textContent.trim();
                const matches = text.match(/(\d[\d\s]{2,6}\d)\s*[₽Р]/g);

                if (matches) {
                    let isRealTicket = false;
                    let ancestor = node.parentElement;

                    while (ancestor && ancestor.tagName !== 'BODY') {
                        const rect = ancestor.getBoundingClientRect();
                        if (rect.height > 800) break; 

                        const ancestorText = ancestor.textContent || "";
                        const hasTime = /\b(?:[01]?\d|2[0-3])\s*:\s*[0-5]\d\b/.test(ancestorText);
                        const hasDuration = /в пути/i.test(ancestorText); 

                        if (hasTime || hasDuration) {
                            isRealTicket = true;
                            break;
                        }
                        ancestor = ancestor.parentElement;
                    }

                    if (isRealTicket) {
                        for (const m of matches) {
                            const num = parseInt(m.replace(/\D/g, ''), 10);
                            if (num >= 5000 && num <= 500000) results.push(num);
                        }
                    }
                }
            }
            return results;
        }
    """)

    if price_list and len(price_list) > 0:
        valid = [p for p in price_list if isinstance(p, (int, float)) and 5000 <= p <= 500000]
        if valid:
            return min(valid)
    return None


async def _extract_offers(page) -> list[FlightOffer]:
    """Извлекает отдельные карточки маршрутов и преобразует их в варианты."""
    card_texts = await page.evaluate(r"""
        () => {
            const results = new Set();
            const pricePattern = /\d[\d\s\u00a0]{2,8}\d\s*(?:₽|Р\b)/i;
            const timePattern = /\b(?:[01]?\d|2[0-3]):[0-5]\d\b/g;
            const routePattern = /без пересад|\d+\s+пересад|прямой рейс|стыковк|в пути/i;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const ownText = (node.textContent || '').replace(/\s+/g, ' ').trim();
                if (!pricePattern.test(ownText)) continue;

                let element = node.parentElement;
                let selected = null;
                for (let depth = 0; element && element !== document.body && depth < 10; depth++) {
                    const rect = element.getBoundingClientRect();
                    const text = (element.innerText || element.textContent || '')
                        .replace(/\s+/g, ' ')
                        .trim();
                    const times = text.match(timePattern) || [];
                    if (
                        rect.width > 0 && rect.height > 0 &&
                        text.length >= 20 && text.length <= 5000 &&
                        times.length >= 2 && routePattern.test(text)
                    ) {
                        selected = text;
                        break;
                    }
                    element = element.parentElement;
                }
                if (selected) results.add(selected);
            }
            return Array.from(results);
        }
    """)

    offers: list[FlightOffer] = []
    seen = set()
    for text in card_texts:
        offer = parse_offer_text(text)
        if offer is None:
            continue
        key = (
            offer.price,
            offer.stops,
            offer.layovers,
            offer.route,
            offer.total_duration_minutes,
        )
        if key not in seen:
            seen.add(key)
            offers.append(offer)
    return offers


def _offer_log_text(offer: FlightOffer) -> str:
    parts = [f"пересадок: {offer.stops}"]
    if offer.layovers:
        parts.append("стыковки: " + ", ".join(minutes_text(value) for value in offer.layovers))
    if offer.total_duration_minutes is not None:
        parts.append(f"в пути: {minutes_text(offer.total_duration_minutes)}")
    return "; ".join(parts)


def _offer_message_lines(offer: FlightOffer) -> list[str]:
    if offer.stops == 0:
        lines = ["🔄 Пересадки: <b>прямой рейс</b>"]
    else:
        lines = [f"🔄 Пересадки: <b>{offer.stops}</b>"]
        if len(offer.route) > 2:
            lines.append(f"🧭 Через: <b>{' → '.join(offer.route[1:-1])}</b>")
        if offer.layovers:
            lines.append(
                "⏱ Стыковки: <b>"
                + ", ".join(minutes_text(value) for value in offer.layovers)
                + "</b>"
            )
    if offer.total_duration_minutes is not None:
        lines.append(f"🕒 В пути: <b>{minutes_text(offer.total_duration_minutes)}</b>")
    return lines


async def process_task(context, task: dict, is_test: bool = False) -> None:
    """Обрабатывает одно задание: открывает вкладку, парсит, отправляет уведомление."""
    origin = task['origin']
    dest = task['dest']
    date = task['date']
    threshold = task['threshold']
    chat_id = task['chat_id']

    search_url = generate_aeroflot_url(origin, dest, date)

    log.info("-" * 40)
    log.info(f"Проверка: {origin} → {dest} на {date} (Порог: {threshold})")

    page = await context.new_page()
    price = None

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)

        # Закрываем куки
        try:
            cookie_btn = await page.wait_for_selector(
                "button:has-text('Хорошо'), button:has-text('Принять'), [class*='cookie'] button",
                timeout=8_000,
            )
            if cookie_btn:
                await cookie_btn.click()
        except PlaywrightTimeoutError:
            pass

        # Ждём загрузки
        try:
            await page.wait_for_selector("[class*='price'], [class*='Price']", timeout=30_000)
        except PlaywrightTimeoutError:
            log.warning("Карточки рейсов не появились, но попробуем спарсить...")

        offers = await _extract_offers(page)
        selected_offer = cheapest_matching_offer(task, offers)
        price = selected_offer.price if selected_offer else None

        if price is None:
            if offers:
                note = "Нет маршрутов, подходящих по пересадкам"
                log.info("%s. Найдено вариантов до фильтрации: %s", note, len(offers))
            else:
                note = "Не удалось разобрать карточки маршрутов"
                log.warning("%s.", note)
            save_to_csv(task, search_url, None, note)
            if is_test:
                await send_telegram(chat_id, f"❌ {note}: {origin} → {dest} | {date}")
        else:
            details = _offer_log_text(selected_offer)
            log.info("Найден подходящий маршрут: %s ₽ (%s)", price, details)
            save_to_csv(task, search_url, price, details)

            # Отправка уведомления
            if price < threshold or is_test:
                emoji = "🔥" if price < threshold else "ℹ️"
                reason = "ЦЕНА УПАЛА!" if price < threshold else "Тестовый запуск"
                offer_lines = "\n".join(_offer_message_lines(selected_offer))
                price_text = f"{price:,}".replace(",", " ")
                threshold_text = f"{threshold:,}".replace(",", " ")

                msg = (
                    f"{emoji} <b>{reason}</b>\n\n"
                    f"✈️ <b>{origin} → {dest}</b>\n"
                    f"📅 Дата: {date}\n"
                    f"{offer_lines}\n"
                    f"💰 Цена: <b>{price_text} ₽</b>\n"
                    f"🎯 Ваш порог: {threshold_text} ₽\n"
                    f"🔗 <a href=\"{search_url}\">Купить билет</a>"
                )
                await send_telegram(chat_id, msg)
            else:
                log.info(f"Цена {price} ₽ выше порога {threshold} ₽. Молчим.")

    except Exception as e:
        log.error(f"Ошибка при обработке {origin}-{dest}: {e}")
    finally:
        await page.close()


# ── Главный цикл ───────────────────────────────────────────────────────────────
async def run_all_tasks(is_test: bool = False) -> None:
    """Запускает браузер один раз и проходит по всем задачам из tasks.json."""
    tasks = load_tasks()
    if not tasks:
        log.warning("Список задач пуст!")
        return

    log.info(f"=== Запуск цикла проверок. Задач в очереди: {len(tasks)} ===")

    async with async_playwright() as p:
        launch_options = {
            "headless": HEADLESS,
            "slow_mo": 300,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if CHROMIUM_EXECUTABLE_PATH:
            launch_options["executable_path"] = CHROMIUM_EXECUTABLE_PATH

        browser = await p.chromium.launch(**launch_options)

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        for i, task in enumerate(tasks):
            if not contains_task(load_tasks(), task):
                log.info(
                    "Заявка %s на %s удалена — пропускаем.",
                    task.get("request_id", "без номера"),
                    task.get("date", "неизвестную дату"),
                )
                continue

            await process_task(context, task, is_test)

            # Делаем паузу между проверками разных билетов,
            # чтобы Аэрофлот не забанил нас за частые запросы (кроме последней задачи)
            if i < len(tasks) - 1:
                log.info("Пауза 10 секунд перед следующим запросом...")
                await asyncio.sleep(10)

        await context.close()
        await browser.close()

    log.info("=== Цикл проверок завершен ===")


def run_check(is_test: bool = False) -> None:
    """Синхронная обёртка для schedule."""
    asyncio.run(run_all_tasks(is_test=is_test))


def main() -> None:
    is_test = "--test" in sys.argv
    init_csv()

    if is_test:
        log.info("🧪 ТЕСТОВЫЙ РЕЖИМ")
        run_check(is_test=True)
        return

    log.info(f"🚀 Трекер запущен. Проверка каждые {CHECK_INTERVAL} ч.")
    run_check()

    schedule.every(CHECK_INTERVAL).hours.do(run_check)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
