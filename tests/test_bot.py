import json
import unittest
import uuid
from datetime import date
from pathlib import Path

from airport_catalog import find_airports
from bot import (
    BUTTON_AIRPORTS,
    BUTTON_HELP,
    BUTTON_NEW,
    BUTTON_REQUESTS,
    MAIN_MENU,
    airport_keyboard,
    parse_future_date,
    parse_threshold,
    resolve_airport,
)
from calendar_ui import build_calendar
from task_store import TaskStore, contains_task, dates_between


class BotParsingTests(unittest.TestCase):
    def test_main_menu_contains_all_actions(self):
        labels = [[button.text for button in row] for row in MAIN_MENU.keyboard]
        self.assertEqual(
            labels,
            [[BUTTON_NEW, BUTTON_REQUESTS], [BUTTON_AIRPORTS, BUTTON_HELP]],
        )

    def test_resolve_city_and_iata(self):
        self.assertEqual(resolve_airport(" Москва "), "MOW")
        self.assertEqual(resolve_airport("led"), "LED")
        self.assertIsNone(resolve_airport("Неизвестный город"))

    def test_airport_suggestions_are_limited_to_catalog(self):
        self.assertEqual(
            [airport.code for airport in find_airports("Москва")],
            ["MOW", "SVO"],
        )
        self.assertEqual(find_airports("KRD"), [])

    def test_city_prefix_returns_city_suggestions(self):
        self.assertEqual(
            [airport.code for airport in find_airports("Моск")],
            ["MOW", "SVO"],
        )
        self.assertEqual(
            [airport.code for airport in find_airports("Санкт")],
            ["LED"],
        )

    def test_airport_keyboard_contains_rows_of_buttons(self):
        markup = airport_keyboard(find_airports("Москва"), "origin")
        self.assertEqual(len(markup.inline_keyboard), 2)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "airport:origin:MOW")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "airport:origin:SVO")

    def test_calendar_disables_dates_outside_period(self):
        markup = build_calendar(
            "single",
            date(2026, 10, 1),
            min_date=date(2026, 10, 26),
            max_date=date(2026, 11, 30),
        )
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("cal:day:single:2026-10-26", callbacks)
        self.assertNotIn("cal:day:single:2026-10-25", callbacks)

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
        first = {
            "request_id": "abc",
            "origin": "MOW",
            "dest": "AER",
            "date": "01.11.2026",
            "threshold": 15000,
        }
        second = {**first, "date": "02.11.2026"}
        changed_route = {**first, "dest": "SCO"}
        self.assertTrue(contains_task([first], first))
        self.assertFalse(contains_task([first], second))
        self.assertFalse(contains_task([first], changed_route))

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

    def test_update_route_dates_and_threshold(self):
        directory = Path(__file__).parent / f".tmp-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            task_store = TaskStore(directory / "tasks.json")
            request_id = task_store.add_request(
                chat_id=123,
                origin="MOW",
                dest="AER",
                dates=[date(2026, 11, 1)],
                threshold=15000,
            )
            self.assertTrue(
                task_store.update_request(
                    123,
                    request_id,
                    dest="SCO",
                    dates=[date(2026, 12, 1), date(2026, 12, 2)],
                    threshold=20000,
                )
            )
            request = task_store.get_request(123, request_id)
            self.assertEqual(request["dest"], "SCO")
            self.assertEqual(request["threshold"], 20000)
            self.assertEqual(request["dates"], ["01.12.2026", "02.12.2026"])
        finally:
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()


if __name__ == "__main__":
    unittest.main()
