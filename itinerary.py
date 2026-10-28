"""Разбор и фильтрация вариантов перелёта с пересадками."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


RUB_PRICE_RE = re.compile(r"(\d[\d\s\u00a0]{2,8}\d)\s*(?:₽|Р\b)", re.IGNORECASE)
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
STOPS_RE = re.compile(r"(?<![:\d])\b([012])\s+пересад", re.IGNORECASE)
DURATION_RE = re.compile(
    r"(?:в пути|общее время|продолжительность)[^\d]{0,30}(?:(\d+)\s*(?:дн|день|дня|дней))?"
    r"\s*(?:(\d+)\s*ч)?\s*(?:(\d+)\s*мин)?",
    re.IGNORECASE,
)
LAYOVER_RE = re.compile(
    r"(?:пересадк|стыковк)[а-яё]*(?![^\d]{0,60}(?:в пути|общее время|продолжительность))"
    r"[^\d]{0,60}(?:(\d+)\s*(?:дн|день|дня|дней))?"
    r"\s*(?:(\d+)\s*ч)?\s*(?:(\d+)\s*мин)?",
    re.IGNORECASE,
)
IATA_RE = re.compile(r"\b[A-Z]{3}\b")


@dataclass(frozen=True)
class FlightOffer:
    price: int
    stops: int
    layovers: tuple[int, ...] = ()
    route: tuple[str, ...] = ()
    total_duration_minutes: int | None = None
    overnight: bool = False
    airport_change: bool = False


def _duration_minutes(match: re.Match[str]) -> int | None:
    days, hours, minutes = match.groups()
    if not any((days, hours, minutes)):
        return None
    return int(days or 0) * 1440 + int(hours or 0) * 60 + int(minutes or 0)


def parse_offer_text(text: str) -> FlightOffer | None:
    """Преобразует текст одной карточки результата в структурированный вариант."""
    compact = " ".join(text.replace("\u00a0", " ").split())
    prices = [int(re.sub(r"\D", "", value)) for value in RUB_PRICE_RE.findall(compact)]
    prices = [price for price in prices if 1_000 <= price <= 1_000_000]
    if not prices:
        return None

    lower = compact.lower()
    if "без пересад" in lower or "прямой рейс" in lower:
        stops = 0
    else:
        stop_match = STOPS_RE.search(compact)
        if stop_match:
            stops = int(stop_match.group(1))
        else:
            times_count = len(TIME_RE.findall(compact))
            if times_count < 2:
                return None
            stops = max(0, min(2, times_count // 2 - 1))

    layovers = tuple(
        value
        for match in LAYOVER_RE.finditer(compact)
        if (value := _duration_minutes(match)) is not None
    )
    duration_match = DURATION_RE.search(compact)
    total_duration = _duration_minutes(duration_match) if duration_match else None

    raw_route = [code for code in IATA_RE.findall(compact) if code not in {"RUB", "RUR"}]
    route: list[str] = []
    for code in raw_route:
        if code not in {"RUB", "RUR"} and (not route or route[-1] != code):
            route.append(code)

    clock_values = []
    for value in TIME_RE.findall(compact):
        hours, minutes = value.split(":")
        clock_values.append(int(hours) * 60 + int(minutes))
    overnight = bool(re.search(r"ночн|на следующий день", lower))
    expected_times = 2 * (stops + 1)
    if stops and len(clock_values) >= expected_times:
        segment_times = clock_values[:expected_times]
        overnight = overnight or any(
            segment_times[index * 2 + 2] < segment_times[index * 2 + 1]
            for index in range(stops)
        )

    airport_change = bool(re.search(r"смена аэропорта|другой аэропорт", lower))
    if stops and len(raw_route) == expected_times:
        airport_change = airport_change or any(
            raw_route[index * 2 + 1] != raw_route[index * 2 + 2]
            for index in range(stops)
        )

    return FlightOffer(
        price=min(prices),
        stops=stops,
        layovers=layovers,
        route=tuple(route),
        total_duration_minutes=total_duration,
        overnight=overnight,
        airport_change=airport_change,
    )


def offer_matches(task: dict, offer: FlightOffer) -> bool:
    """Проверяет вариант по настройкам пересадок заявки."""
    max_stops = int(task.get("max_stops", 0) or 0)
    if offer.stops > max_stops:
        return False
    if offer.stops == 0:
        return True
    if offer.airport_change and not bool(task.get("allow_airport_change", False)):
        return False
    if offer.overnight and bool(task.get("avoid_overnight", True)):
        return False

    minimum = int(task.get("min_connection_minutes", 0) or 0)
    maximum = int(task.get("max_connection_minutes", 720) or 720)
    # Для фильтра времени нужны данные по каждой стыковке. Не считаем
    # неполную карточку подходящей, иначе можно прислать неудобный маршрут.
    if len(offer.layovers) < offer.stops:
        return False
    return all(minimum <= value <= maximum for value in offer.layovers)


def cheapest_matching_offer(task: dict, offers: Iterable[FlightOffer]) -> FlightOffer | None:
    matching = [offer for offer in offers if offer_matches(task, offer)]
    return min(matching, key=lambda offer: offer.price) if matching else None


def minutes_text(value: int) -> str:
    hours, minutes = divmod(value, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"

