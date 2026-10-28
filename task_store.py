"""Хранилище заявок на отслеживание рейсов.

Формат совместим с tracker.py: одна дата поиска хранится как одна задача.
Заявка на диапазон дат объединяется общим request_id.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


MAX_RANGE_DAYS = 31
DEFAULT_MAX_STOPS = 0
DEFAULT_CONNECTION_MODE = "direct"


def normalize_connection_settings(
    max_stops: int = DEFAULT_MAX_STOPS,
    connection_mode: str = DEFAULT_CONNECTION_MODE,
    min_connection_minutes: int | None = None,
    max_connection_minutes: int | None = None,
) -> dict:
    """Проверяет и нормализует настройки пересадок для хранения в заявке."""
    if max_stops not in {0, 1, 2}:
        raise ValueError("Допустимо не более двух пересадок.")
    if max_stops == 0:
        return {
            "max_stops": 0,
            "connection_mode": "direct",
            "min_connection_minutes": None,
            "max_connection_minutes": None,
            "avoid_overnight": True,
            "allow_airport_change": False,
        }
    if connection_mode not in {"comfortable", "flexible", "custom"}:
        raise ValueError("Неизвестный режим стыковки.")
    if min_connection_minutes is None or max_connection_minutes is None:
        raise ValueError("Укажите минимальное и максимальное время стыковки.")
    if not 0 <= min_connection_minutes <= max_connection_minutes <= 1440:
        raise ValueError("Время стыковки должно быть от 0 до 24 часов.")
    return {
        "max_stops": max_stops,
        "connection_mode": connection_mode,
        "min_connection_minutes": min_connection_minutes,
        "max_connection_minutes": max_connection_minutes,
        "avoid_overnight": True,
        "allow_airport_change": False,
    }


def dates_between(start: date, end: date) -> list[date]:
    """Возвращает включительный диапазон дат, ограниченный 31 днём."""
    if end < start:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    days_count = (end - start).days + 1
    if days_count > MAX_RANGE_DAYS:
        raise ValueError(f"Диапазон не может быть больше {MAX_RANGE_DAYS} дней.")

    return [start + timedelta(days=offset) for offset in range(days_count)]


def task_key(task: dict) -> tuple[str, ...]:
    """Возвращает устойчивый ключ одной даты заявки."""
    request_id = task.get("request_id")
    if request_id:
        return (
            "request",
            str(request_id),
            str(task.get("origin")),
            str(task.get("dest")),
            str(task.get("date")),
            str(task.get("threshold")),
            str(task.get("max_stops", DEFAULT_MAX_STOPS)),
            str(task.get("connection_mode", DEFAULT_CONNECTION_MODE)),
            str(task.get("min_connection_minutes")),
            str(task.get("max_connection_minutes")),
            str(task.get("avoid_overnight", True)),
            str(task.get("allow_airport_change", False)),
        )
    return (
        "legacy",
        str(task.get("chat_id")),
        str(task.get("origin")),
        str(task.get("dest")),
        str(task.get("date")),
        str(task.get("threshold")),
        str(task.get("max_stops", DEFAULT_MAX_STOPS)),
        str(task.get("connection_mode", DEFAULT_CONNECTION_MODE)),
        str(task.get("min_connection_minutes")),
        str(task.get("max_connection_minutes")),
        str(task.get("avoid_overnight", True)),
        str(task.get("allow_airport_change", False)),
    )


def contains_task(tasks: Iterable[dict], target: dict) -> bool:
    """Проверяет, осталась ли конкретная дата заявки в актуальной очереди."""
    target_key = task_key(target)
    return any(task_key(task) == target_key for task in tasks)


class TaskStore:
    """Потокобезопасное JSON-хранилище с атомарной записью файла."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        with self._lock:
            return self._read_unlocked()

    def add_request(
        self,
        *,
        chat_id: int | str,
        origin: str,
        dest: str,
        dates: Iterable[date],
        threshold: int,
        max_stops: int = DEFAULT_MAX_STOPS,
        connection_mode: str = DEFAULT_CONNECTION_MODE,
        min_connection_minutes: int | None = None,
        max_connection_minutes: int | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex[:8]
        date_list = list(dates)
        if not date_list:
            raise ValueError("В заявке должна быть хотя бы одна дата.")

        connection_settings = normalize_connection_settings(
            max_stops,
            connection_mode,
            min_connection_minutes,
            max_connection_minutes,
        )
        new_tasks = [
            {
                "request_id": request_id,
                "chat_id": str(chat_id),
                "origin": origin,
                "dest": dest,
                "date": flight_date.strftime("%d.%m.%Y"),
                "threshold": threshold,
                **connection_settings,
            }
            for flight_date in date_list
        ]

        with self._lock:
            tasks = self._read_unlocked()
            tasks.extend(new_tasks)
            self._write_unlocked(tasks)
        return request_id

    def requests_for_chat(self, chat_id: int | str) -> list[dict]:
        """Группирует отдельные даты в пользовательские заявки."""
        grouped: dict[str, dict] = {}
        chat_id = str(chat_id)

        for index, task in enumerate(self.load()):
            if str(task.get("chat_id")) != chat_id:
                continue

            request_id = str(task.get("request_id") or f"legacy-{index}")
            item = grouped.setdefault(
                request_id,
                {
                    "request_id": request_id,
                    "origin": task.get("origin", "?"),
                    "dest": task.get("dest", "?"),
                    "threshold": task.get("threshold"),
                    "max_stops": task.get("max_stops", DEFAULT_MAX_STOPS),
                    "connection_mode": task.get("connection_mode", DEFAULT_CONNECTION_MODE),
                    "min_connection_minutes": task.get("min_connection_minutes"),
                    "max_connection_minutes": task.get("max_connection_minutes"),
                    "avoid_overnight": task.get("avoid_overnight", True),
                    "allow_airport_change": task.get("allow_airport_change", False),
                    "dates": [],
                },
            )
            item["dates"].append(str(task.get("date", "?")))

        return list(grouped.values())

    def get_request(self, chat_id: int | str, request_id: str) -> dict | None:
        for request in self.requests_for_chat(chat_id):
            if request["request_id"] == request_id:
                return request
        return None

    def update_request(
        self,
        chat_id: int | str,
        request_id: str,
        *,
        origin: str | None = None,
        dest: str | None = None,
        dates: Iterable[date] | None = None,
        threshold: int | None = None,
        max_stops: int | None = None,
        connection_mode: str | None = None,
        min_connection_minutes: int | None = None,
        max_connection_minutes: int | None = None,
    ) -> bool:
        """Обновляет поля заявки, сохраняя её request_id."""
        chat_id = str(chat_id)
        with self._lock:
            tasks = self._read_unlocked()
            matching = [
                task
                for task in tasks
                if str(task.get("chat_id")) == chat_id
                and str(task.get("request_id")) == request_id
            ]
            if not matching:
                return False

            connection_settings = None
            if max_stops is not None:
                connection_settings = normalize_connection_settings(
                    max_stops,
                    connection_mode or DEFAULT_CONNECTION_MODE,
                    min_connection_minutes,
                    max_connection_minutes,
                )

            if dates is None:
                for task in matching:
                    if origin is not None:
                        task["origin"] = origin
                    if dest is not None:
                        task["dest"] = dest
                    if threshold is not None:
                        task["threshold"] = threshold
                    if connection_settings is not None:
                        task.update(connection_settings)
                self._write_unlocked(tasks)
                return True

            date_list = list(dates)
            if not date_list:
                raise ValueError("В заявке должна быть хотя бы одна дата.")

            template = matching[0]
            first_index = tasks.index(template)
            remaining = [task for task in tasks if task not in matching]
            replacement = [
                {
                    "request_id": request_id,
                    "chat_id": chat_id,
                    "origin": origin if origin is not None else template.get("origin"),
                    "dest": dest if dest is not None else template.get("dest"),
                    "date": flight_date.strftime("%d.%m.%Y"),
                    "threshold": threshold if threshold is not None else template.get("threshold"),
                    "max_stops": template.get("max_stops", DEFAULT_MAX_STOPS),
                    "connection_mode": template.get("connection_mode", DEFAULT_CONNECTION_MODE),
                    "min_connection_minutes": template.get("min_connection_minutes"),
                    "max_connection_minutes": template.get("max_connection_minutes"),
                    "avoid_overnight": template.get("avoid_overnight", True),
                    "allow_airport_change": template.get("allow_airport_change", False),
                }
                for flight_date in date_list
            ]
            if connection_settings is not None:
                for task in replacement:
                    task.update(connection_settings)
            remaining[first_index:first_index] = replacement
            self._write_unlocked(remaining)
            return True

    def delete_request(self, chat_id: int | str, request_id: str) -> int:
        """Удаляет только заявку указанного пользователя."""
        chat_id = str(chat_id)
        with self._lock:
            tasks = self._read_unlocked()
            remaining = []
            for index, task in enumerate(tasks):
                task_request_id = str(task.get("request_id") or f"legacy-{index}")
                matches = str(task.get("chat_id")) == chat_id and task_request_id == request_id
                if not matches:
                    remaining.append(task)
            deleted = len(tasks) - len(remaining)
            if deleted:
                self._write_unlocked(remaining)
            return deleted

    def _read_unlocked(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Не удалось прочитать {self.path.name}: {exc}") from exc
        if not isinstance(data, list):
            raise RuntimeError(f"{self.path.name} должен содержать JSON-массив.")
        return data

    def _write_unlocked(self, tasks: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
