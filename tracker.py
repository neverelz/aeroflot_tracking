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

from task_store import contains_task

# ── Настройки ──────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "3"))
HEADLESS           = os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CSV_FILE = DATA_DIR / "prices.csv"
TASKS_FILE = DATA_DIR / "tasks.json"
LOG_FILE = DATA_DIR / "tracker.log"

# ── Логирование ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
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
    except Exception as e:
        log.error(f"Ошибка чтения tasks.json: {e}")
        sys.exit(1)


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
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Добавили chat_id и маршрут, чтобы не путать данные разных пользователей
            writer.writerow(["timestamp", "chat_id", "route", "date", "price_rub", "url", "note"])
        log.info("Создан файл %s", CSV_FILE)


def save_to_csv(task: dict, search_url: str, price: int | None, note: str = "") -> None:
    """Сохраняет запись в CSV."""
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

        price = await _extract_min_price(page)

        if price is None:
            log.warning("Цена не найдена.")
            save_to_csv(task, search_url, None, "Ошибка парсинга")
            if is_test:
                await send_telegram(chat_id, f"❌ Ошибка получения цены {origin} → {dest} | {date}")
        else:
            log.info(f"Найдена цена: {price} ₽")
            save_to_csv(task, search_url, price)

            # Отправка уведомления
            if price < threshold or is_test:
                emoji = "🔥" if price < threshold else "ℹ️"
                reason = "ЦЕНА УПАЛА!" if price < threshold else "Тестовый запуск"

                msg = (
                    f"{emoji} <b>{reason}</b>\n\n"
                    f"✈️ <b>{origin} → {dest}</b>\n"
                    f"📅 Дата: {date}\n"
                    f"💰 Цена: <b>{price:,} ₽</b>\n".replace(",", " ") +
                    f"🎯 Ваш порог: {threshold:,} ₽\n".replace(",", " ") +
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
        browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=300,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

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
