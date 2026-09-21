"""Focused tests for auto-sync cadence and lifecycle boundaries."""

import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import app as A


def progress(*, current=600, finished=False, started_at=100, finished_at=None):
    return {
        "libraryItemId": "item-1",
        "currentTime": current,
        "progress": 1.0 if finished else 0.1,
        "isFinished": finished,
        "startedAt": started_at,
        "finishedAt": finished_at,
    }


def book(*, current=10.0, finished=False):
    return {
        "abs_item_id": "item-1",
        "state_key": "item-1",
        "title": "A Book",
        "author": "An Author",
        "identifiers": [],
        "progress_percent": 100.0 if finished else 10.0,
        "current_minutes": current,
        "duration_minutes": 100.0,
        "is_finished": finished,
    }


class SchedulerTests(unittest.TestCase):
    USER_ID = "user-1"

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="abs-sg-scheduler-")
        A._config_store = A._UserJsonStore(f"{self.data_dir}/config")
        A._sync_store = A._UserJsonStore(f"{self.data_dir}/sync")
        A._scheduler_store = A._UserJsonStore(f"{self.data_dir}/scheduler")
        A._status_cache.clear()
        A.set_cfg(self.USER_ID, {
            "ABS_URL": "http://abs",
            "ABS_TOKEN": "token",
            "STORYGRAPH_SESSION": "session",
            "SYNC_MODE": "daily",
            "DAILY_SYNC_TIME": "00:00",
            "TIMEZONE": "Europe/London",
        })
        self.user = {"id": self.USER_ID, "username": "jordan"}

        self.real_progress = A.get_abs_progress
        self.real_books = A.get_abs_books
        self.real_book = A.get_abs_book
        self.real_sync = A.do_sync

    def tearDown(self):
        A.get_abs_progress = self.real_progress
        A.get_abs_books = self.real_books
        A.get_abs_book = self.real_book
        A.do_sync = self.real_sync
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def initialized_state(self, **updates):
        state = A._scheduler_store.get(self.USER_ID)
        state.update({"initialized": True, "books": {}, **updates})
        A._scheduler_store.save(self.USER_ID)
        return state

    def test_first_snapshot_is_a_quiet_baseline(self):
        state = {}
        changes = A._lifecycle_changes({"item-1": progress(finished=True, finished_at=200)}, state)
        self.assertEqual({}, changes)
        self.assertEqual(100, state["books"]["item-1"]["handled_started_at"])
        self.assertEqual(200, state["books"]["item-1"]["handled_finished_at"])

    def test_new_start_and_finish_are_each_detected(self):
        state = {"initialized": True, "books": {}}
        self.assertEqual(
            {"item-1": {"start": True, "finish": False}},
            A._lifecycle_changes({"item-1": progress()}, state),
        )
        A._mark_handled_lifecycle(state, progress(), "item-1")
        self.assertEqual(
            {"item-1": {"start": False, "finish": True}},
            A._lifecycle_changes(
                {"item-1": progress(finished=True, finished_at=200)}, state
            ),
        )

    def test_midnight_daily_run_is_due_once_per_local_date(self):
        self.initialized_state()
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        due, local_date = A._daily_schedule(self.USER_ID, now)
        self.assertTrue(due)
        self.assertEqual("2026-09-21", local_date)

        A._scheduler_store.get(self.USER_ID)["last_daily_run"] = local_date
        due, _ = A._daily_schedule(self.USER_ID, now)
        self.assertFalse(due)

    def test_daily_mode_waits_for_ordinary_progress_but_syncs_a_start(self):
        self.initialized_state(last_daily_run="2026-09-21")
        snapshot = {"item-1": progress()}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: self.fail("ordinary books should wait")
        A.get_abs_book = lambda user_id, item_id, item_progress: book()
        calls = []

        def fake_sync(user_id, books, start_before_finish=None):
            calls.append((books, start_before_finish))
            return [{"status": "success", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        A._poll_user(self.user, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))
        self.assertEqual(1, len(calls))
        self.assertEqual(100, A._scheduler_store.get(self.USER_ID)["books"]["item-1"]["handled_started_at"])

    def test_short_book_records_start_before_finish(self):
        self.initialized_state(last_daily_run="2026-09-21")
        snapshot = {"item-1": progress(finished=True, finished_at=200)}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: []
        A.get_abs_book = lambda user_id, item_id, item_progress: book(finished=True)
        starts = []

        def fake_sync(user_id, books, start_before_finish=None):
            starts.append(start_before_finish)
            return [{"status": "success", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        A._poll_user(self.user, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))
        self.assertEqual([{"item-1"}], starts)

    def test_failed_lifecycle_event_is_retried(self):
        self.initialized_state(last_daily_run="2026-09-21")
        snapshot = {"item-1": progress()}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: []
        A.get_abs_book = lambda user_id, item_id, item_progress: book()
        calls = []

        def fake_sync(user_id, books, start_before_finish=None):
            calls.append(books)
            return [{"status": "failed", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)
        self.assertEqual(2, len(calls))

    def test_scheduled_run_is_recorded_and_not_repeated(self):
        self.initialized_state()
        snapshot = {"item-1": progress()}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: [book()]
        calls = []

        def fake_sync(user_id, books, start_before_finish=None):
            calls.append(books)
            return [{"status": "success", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)
        self.assertEqual(1, len(calls))
        self.assertEqual("2026-09-21", A._scheduler_store.get(self.USER_ID)["last_daily_run"])

    def test_frequent_threshold_uses_durable_sync_position(self):
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 10.0,
            "current_minutes": 10.0,
            "status": "currently-reading",
        }
        self.assertEqual([], A._frequent_sync_candidates(self.USER_ID, [book(current=14.9)]))
        self.assertEqual(1, len(A._frequent_sync_candidates(self.USER_ID, [book(current=15.0)])))

    def test_frequent_mode_does_not_recheck_an_already_synced_finish(self):
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 100.0,
            "current_minutes": 100.0,
            "status": "read",
        }
        self.assertEqual([], A._frequent_sync_candidates(self.USER_ID, [book(current=100.0, finished=True)]))


if __name__ == "__main__":
    unittest.main()
