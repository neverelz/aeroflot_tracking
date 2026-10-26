"""Небольшой каталог аэропортов для подсказок в Telegram-боте."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Airport:
    code: str
    city: str
    name: str
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.city} — {self.name} ({self.code})"


AIRPORTS = (
    Airport("MOW", "Москва", "все аэропорты", ("moscow",)),
    Airport("SVO", "Москва", "Шереметьево", ("sheremetyevo", "шереметьево")),
    Airport("LED", "Санкт-Петербург", "Пулково", ("петербург", "спб", "saint petersburg", "pulkovo")),
    Airport("AER", "Сочи", "Сочи", ("sochi", "адлер", "adler")),
    Airport("KRR", "Краснодар", "Пашковский", ("krasnodar", "пашковский")),
    Airport("KZN", "Казань", "Казань", ("kazan",)),
    Airport("SVX", "Екатеринбург", "Кольцово", ("yekaterinburg", "ekaterinburg", "кольцово")),
    Airport("OVB", "Новосибирск", "Толмачёво", ("novosibirsk", "толмачево")),
    Airport("KGD", "Калининград", "Храброво", ("kaliningrad", "храброво")),
    Airport("MRV", "Минеральные Воды", "Минеральные Воды", ("минводы", "mineralnye vody")),
    Airport("MCX", "Махачкала", "Уйташ", ("makhachkala",)),
    Airport("UFA", "Уфа", "Уфа", ("ufa",)),
    Airport("KUF", "Самара", "Курумоч", ("samara", "курумоч")),
    Airport("VVO", "Владивосток", "Кневичи", ("vladivostok",)),
    Airport("KHV", "Хабаровск", "Новый", ("khabarovsk",)),
    Airport("IKT", "Иркутск", "Иркутск", ("irkutsk",)),
    Airport("OMS", "Омск", "Омск", ("omsk",)),
    Airport("PEE", "Пермь", "Большое Савино", ("perm",)),
    Airport("MMK", "Мурманск", "Мурманск", ("murmansk",)),
    Airport("ARH", "Архангельск", "Талаги", ("arkhangelsk",)),
    Airport("VOG", "Волгоград", "Гумрак", ("volgograd",)),
    Airport("GOJ", "Нижний Новгород", "Стригино", ("нижний", "nizhny novgorod")),
    Airport("CEK", "Челябинск", "Баландино", ("chelyabinsk",)),
    Airport("TJM", "Тюмень", "Рощино", ("tyumen",)),
    Airport("SGC", "Сургут", "Сургут", ("surgut",)),
    Airport("KJA", "Красноярск", "Красноярск", ("krasnoyarsk",)),
    Airport("YKS", "Якутск", "Якутск", ("yakutsk",)),
    Airport("UUS", "Южно-Сахалинск", "Хомутово", ("сахалин", "yuzhno-sakhalinsk")),
    Airport("PKC", "Петропавловск-Камчатский", "Елизово", ("камчатка", "petropavlovsk")),
    Airport("GDX", "Магадан", "Сокол", ("magadan",)),
    Airport("BAX", "Барнаул", "Барнаул", ("barnaul",)),
    Airport("BQS", "Благовещенск", "Игнатьево", ("blagoveshchensk",)),
    Airport("NOZ", "Новокузнецк", "Спиченково", ("novokuznetsk",)),
    Airport("TOF", "Томск", "Богашёво", ("tomsk",)),
    Airport("REN", "Оренбург", "Оренбург", ("orenburg",)),
    Airport("ASF", "Астрахань", "Астрахань", ("astrakhan",)),
    Airport("SCO", "Актау", "Актау", ("aktau",)),
    Airport("ALA", "Алматы", "Алматы", ("алма-ата", "almaty")),
    Airport("NQZ", "Астана", "Нурсултан Назарбаев", ("astana",)),
    Airport("TAS", "Ташкент", "Ташкент", ("tashkent",)),
    Airport("FRU", "Бишкек", "Манас", ("bishkek",)),
    Airport("EVN", "Ереван", "Звартноц", ("yerevan",)),
    Airport("GYD", "Баку", "Гейдар Алиев", ("baku",)),
    Airport("TBS", "Тбилиси", "Тбилиси", ("tbilisi",)),
    Airport("MSQ", "Минск", "Национальный", ("minsk",)),
    Airport("IST", "Стамбул", "Стамбул", ("istanbul",)),
    Airport("AYT", "Анталья", "Анталья", ("анталия", "antalya")),
    Airport("DXB", "Дубай", "Дубай", ("dubai",)),
    Airport("AUH", "Абу-Даби", "Заид", ("abu dhabi",)),
    Airport("CAI", "Каир", "Каир", ("cairo",)),
    Airport("BKK", "Бангкок", "Суварнабхуми", ("bangkok",)),
    Airport("HKT", "Пхукет", "Пхукет", ("phuket",)),
    Airport("DEL", "Дели", "Индира Ганди", ("delhi",)),
    Airport("PEK", "Пекин", "Шоуду", ("beijing",)),
    Airport("PVG", "Шанхай", "Пудун", ("shanghai",)),
)

BY_CODE = {airport.code: airport for airport in AIRPORTS}


def normalize(value: str) -> str:
    value = value.strip().lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()


def _search_terms(airport: Airport) -> tuple[str, ...]:
    return tuple(normalize(value) for value in (airport.city, airport.name, *airport.aliases))


def find_airports(query: str, limit: int = 8) -> list[Airport]:
    """Ищет аэропорты по IATA-коду, городу, названию и псевдонимам."""
    normalized = normalize(query)
    if not normalized:
        return []

    code_match = BY_CODE.get(normalized.upper())
    if code_match:
        return [code_match]

    exact = [airport for airport in AIRPORTS if normalized in _search_terms(airport)]
    if exact:
        return exact[:limit]

    partial = [
        airport
        for airport in AIRPORTS
        if any(normalized in term or term.startswith(normalized) for term in _search_terms(airport))
    ]
    return partial[:limit]
