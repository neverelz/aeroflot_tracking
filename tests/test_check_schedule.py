import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_schedule import (
    CheckStateStore,
    empty_state,
    gap_seconds_remaining,
    next_due_task,
    prune_state,
    request_schedule_key,
    task_is_due,
    task_schedule_key,
)


def make_task(request_id: str, flight_date: str, chat_id: str = "1") -> dict:
    return {
        "request_id": request_id,
        "chat_id": chat_id,
        "origin": "MOW",
        "dest": "AER",
        "date": flight_date,
        "threshold": 15000,
        "max_stops": 0,
    }


class CheckScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
        self.interval = timedelta(hours=3)

    def test_new_task_is_due_immediately_then_waits_interval(self):
        task = make_task("one", "01.11.2026")
        state = empty_state()
        self.assertTrue(task_is_due(task, state, self.now, self.interval))

        state["checked_at"][task_schedule_key(task)] = self.now.isoformat()
        self.assertFalse(task_is_due(task, state, self.now + timedelta(hours=2), self.interval))
        self.assertTrue(task_is_due(task, state, self.now + timedelta(hours=3), self.interval))

    def test_global_gap_is_at_least_ten_seconds(self):
        state = empty_state()
        state["last_started_at"] = self.now.isoformat()
        self.assertEqual(gap_seconds_remaining(state, self.now + timedelta(seconds=3), 10), 7)
        self.assertEqual(gap_seconds_remaining(state, self.now + timedelta(seconds=10), 10), 0)

    def test_requests_are_selected_round_robin(self):
        first_a = make_task("a", "01.11.2026", "1")
        second_a = make_task("a", "02.11.2026", "1")
        first_b = make_task("b", "01.11.2026", "2")
        state = empty_state()

        self.assertEqual(
            next_due_task([first_a, second_a, first_b], state, self.now, self.interval),
            first_a,
        )
        state["request_started_at"][request_schedule_key(first_a)] = self.now.isoformat()
        self.assertEqual(
            next_due_task([first_a, second_a, first_b], state, self.now, self.interval),
            first_b,
        )

    def test_prune_and_atomic_state_roundtrip(self):
        active = make_task("active", "01.11.2026")
        removed = make_task("removed", "02.11.2026")
        state = empty_state()
        state["checked_at"][task_schedule_key(active)] = self.now.isoformat()
        state["checked_at"][task_schedule_key(removed)] = self.now.isoformat()
        state["request_started_at"][request_schedule_key(removed)] = self.now.isoformat()
        self.assertTrue(prune_state(state, [active]))
        self.assertNotIn(task_schedule_key(removed), state["checked_at"])

        directory = Path(__file__).parent / f".tmp-schedule-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            store = CheckStateStore(directory / "check_state.json")
            store.save(state)
            self.assertEqual(store.load(), state)
        finally:
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()


if __name__ == "__main__":
    unittest.main()
