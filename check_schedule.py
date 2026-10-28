"""Устойчивое расписание проверок отдельных задач."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_store import task_key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def task_schedule_key(task: dict) -> str:
    """Стабильный ключ меняется при редактировании значимых полей задачи."""
    return json.dumps(task_key(task), ensure_ascii=False, separators=(",", ":"))


def request_schedule_key(task: dict) -> str:
    return f"{task.get('chat_id')}:{task.get('request_id', 'legacy')}"


def empty_state() -> dict:
    return {
        "version": 2,
        "last_started_at": None,
        "last_finished_at": None,
        "checked_at": {},
        "request_started_at": {},
    }


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def task_is_due(task: dict, state: dict, now: datetime, interval: timedelta) -> bool:
    checked = state.get("checked_at", {})
    last_checked = parse_timestamp(checked.get(task_schedule_key(task)))
    return last_checked is None or now >= last_checked + interval


def gap_seconds_remaining(state: dict, now: datetime, minimum_gap: float) -> float:
    timestamps = [
        parsed
        for parsed in (
            parse_timestamp(state.get("last_started_at")),
            parse_timestamp(state.get("last_finished_at")),
        )
        if parsed is not None
    ]
    if not timestamps:
        return 0.0
    return max(0.0, minimum_gap - (now - max(timestamps)).total_seconds())


def next_due_task(
    tasks: list[dict],
    state: dict,
    now: datetime,
    interval: timedelta,
) -> dict | None:
    """Выбирает по одной дате от каждой заявки по кругу."""
    due = [task for task in tasks if task_is_due(task, state, now, interval)]
    if not due:
        return None
    request_history = state.get("request_started_at", {})
    never = datetime.min.replace(tzinfo=timezone.utc)
    return min(
        enumerate(due),
        key=lambda item: (
            parse_timestamp(request_history.get(request_schedule_key(item[1]))) or never,
            item[0],
        ),
    )[1]


def prune_state(state: dict, tasks: list[dict]) -> bool:
    """Удаляет историю заявок, которых уже нет или которые были изменены."""
    active = {task_schedule_key(task) for task in tasks}
    checked = state.setdefault("checked_at", {})
    stale = [key for key in checked if key not in active]
    for key in stale:
        checked.pop(key, None)
    active_requests = {request_schedule_key(task) for task in tasks}
    request_history = state.setdefault("request_started_at", {})
    stale_requests = [key for key in request_history if key not in active_requests]
    for key in stale_requests:
        request_history.pop(key, None)
    return bool(stale or stale_requests)


class CheckStateStore:
    """Атомарное JSON-хранилище состояния планировщика."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Не удалось прочитать {self.path.name}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("checked_at", {}), dict):
            raise RuntimeError(f"Некорректный формат {self.path.name}")
        data.setdefault("version", 2)
        data.setdefault("last_started_at", None)
        data.setdefault("last_finished_at", None)
        data.setdefault("checked_at", {})
        data.setdefault("request_started_at", {})
        return data

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
