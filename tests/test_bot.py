import json
import unittest
import uuid
from datetime import date
from pathlib import Path

from bot import (
    BUTTON_AIRPORTS,
    BUTTON_HELP,
    BUTTON_NEW,
    BUTTON_REQUESTS,
    MAIN_MENU,
    parse_future_date,
    parse_threshold,
    resolve_airport,
)
from task_store import TaskStore, contains_task, dates_between


class BotParsingTests(unittest.TestCase):
    def test_main_menu_contains_all_actions(self):
        labels = [[button.text for button in row] for row in MAIN_MENU.keyboard]
        self.assertEqual(
            labels,
            [[BUTTON_NEW, BUTTON_REQUESTS], [BUTTON_AIRPORTS, BUTTON_HELP]],
        )

    def test_resolve_city_and_iata(self):
        self.assertEqual(resolve_airport(" Москва "), "SVO")
        self.assertEqual(resolve_airport("led"), "LED")
        self.assertIsNone(resolve_airport("Неизвестный город"))

    def test_parse_future_date(self):
        today = date(2026, 10, 26)
        self.assertEqual(parse_future_date("27.10.2026", today=today), date(2026, 10, 27))
        with self.assertRaisesRegex(ValueError, "прошлом"):
            parse_future_date("25.10.2026", today=today)

    def test_parse_threshold(self):
        self.assertEqual(parse_threshold("15 000 ₽"), 15000)
        with self.assertRaises(ValueError):
            parse_threshold("дорого")

    def test_date_range_is_inclusive_and_limited(self):
        result = dates_between(date(2026, 11, 1), date(2026, 11, 3))
        self.assertEqual(len(result), 3)
        with self.assertRaisesRegex(ValueError, "31"):
            dates_between(date(2026, 11, 1), date(2026, 12, 2))


class TaskStoreTests(unittest.TestCase):
    def test_contains_only_current_task_date(self):
        first = {"request_id": "abc", "date": "01.11.2026"}
        second = {"request_id": "abc", "date": "02.11.2026"}
        self.assertTrue(contains_task([first], first))
        self.assertFalse(contains_task([first], second))

    def test_add_group_and_delete_request(self):
        directory = Path(__file__).parent / f".tmp-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            path = directory / "tasks.json"
            task_store = TaskStore(path)
            request_id = task_store.add_request(
                chat_id=123,
                origin="SVO",
                dest="AER",
                dates=[date(2026, 11, 1), date(2026, 11, 2)],
                threshold=15000,
            )

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(raw), 2)
            self.assertTrue(all(item["request_id"] == request_id for item in raw))
            self.assertEqual(task_store.requests_for_chat(123)[0]["dates"], ["01.11.2026", "02.11.2026"])
            self.assertEqual(task_store.requests_for_chat(999), [])

            self.assertEqual(task_store.delete_request(999, request_id), 0)
            self.assertEqual(task_store.delete_request(123, request_id), 2)
            self.assertEqual(task_store.load(), [])
        finally:
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

    def test_delete_legacy_task(self):
        directory = Path(__file__).parent / f".tmp-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            path = directory / "tasks.json"
            path.write_text(
                json.dumps(
                    [{
                        "chat_id": "123",
                        "origin": "SVO",
                        "dest": "LED",
                        "date": "01.11.2026",
                        "threshold": 10000,
                    }]
                ),
                encoding="utf-8",
            )
            task_store = TaskStore(path)
            self.assertEqual(task_store.requests_for_chat(123)[0]["request_id"], "legacy-0")
            self.assertEqual(task_store.delete_request(123, "legacy-0"), 1)
        finally:
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()


if __name__ == "__main__":
    unittest.main()
