import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import tracker
from check_schedule import empty_state


class TrackerSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_mode_runs_once_and_returns(self):
        with (
            patch.object(tracker, "_load_check_state", return_value=empty_state()),
            patch.object(tracker, "run_due_tasks", new_callable=AsyncMock) as run_due,
        ):
            await tracker.scheduler_loop(is_test=True)

        run_due.assert_awaited_once()
        self.assertTrue(run_due.await_args.kwargs["is_test"])

    async def test_normal_mode_stays_in_scheduler_and_is_not_test_run(self):
        with (
            patch.object(tracker, "_load_check_state", return_value=empty_state()),
            patch.object(
                tracker,
                "run_due_tasks",
                new_callable=AsyncMock,
                return_value=0,
            ) as run_due,
            patch.object(
                tracker.asyncio,
                "sleep",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await tracker.scheduler_loop(is_test=False)

        run_due.assert_awaited_once()
        self.assertNotIn("is_test", run_due.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
