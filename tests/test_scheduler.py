"""Focused tests for auto-sync cadence and lifecycle boundaries."""

import unittest
from datetime import date, datetime, timezone

import app as A
from conftest import isolate_state


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
        isolate_state(self)
        A.set_cfg(self.USER_ID, {
            "ABS_URL": "http://abs",
            "ABS_TOKEN": "token",
            "STORYGRAPH_SESSION": "session",
            "SYNC_MODE": "daily",
            "DAILY_SYNC_TIME": "00:00",
            "TIMEZONE": "Europe/London",
        })
        self.user = {"id": self.USER_ID, "username": "jordan"}
        # Auto-sync only ever looks at books with a confirmed edition.
        A._edition_store.get(self.USER_ID)["books"] = {
            "item-1": {"state": "confirmed", "edition": {"storygraph_book_id": "sg-A Book"}},
        }

        self.real_progress = A.get_abs_progress
        self.real_books = A.get_abs_books
        self.real_book = A.get_abs_book
        self.real_sync = A.do_sync
        self.real_daily_history = A._daily_history_sync
        A._daily_history_sync = lambda *args, **kwargs: True
        self.real_finish_history = A._finish_daily_history
        self.real_client = A.StoryGraphClient
        self.real_sessions = A.get_abs_listening_sessions

    def tearDown(self):
        A.get_abs_progress = self.real_progress
        A.get_abs_books = self.real_books
        A.get_abs_book = self.real_book
        A.do_sync = self.real_sync
        A._daily_history_sync = self.real_daily_history
        A._finish_daily_history = self.real_finish_history
        A.StoryGraphClient = self.real_client
        A.get_abs_listening_sessions = self.real_sessions

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

        def fake_sync(user_id, books, start_before_finish=None, **kwargs):
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
        A._finish_daily_history = lambda *args: True
        starts = []

        def fake_sync(user_id, books, start_before_finish=None, **kwargs):
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

        def fake_sync(user_id, books, start_before_finish=None, **kwargs):
            calls.append(books)
            return [{"status": "failed", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)
        self.assertEqual(2, len(calls))

    def test_scheduled_run_is_recorded_and_not_repeated(self):
        self.initialized_state(
            books={"item-1": {"handled_started_at": 100, "handled_finished_at": None}},
        )
        snapshot = {"item-1": progress()}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: [book()]
        calls = []

        def fake_sync(user_id, books, start_before_finish=None, **kwargs):
            calls.append((books, kwargs))
            return [{"status": "success", "title": books[0]["title"]}]

        A.do_sync = fake_sync
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0][1]["write_progress"])
        self.assertEqual("2026-09-21", A._scheduler_store.get(self.USER_ID)["last_daily_run"])

    def test_first_daily_history_range_is_yesterday_only(self):
        self.assertEqual(
            (date(2026, 9, 20), date(2026, 9, 20)),
            A._daily_history_range({}, "2026-09-21"),
        )

    def test_daily_history_range_catches_up_after_downtime(self):
        self.assertEqual(
            (date(2026, 9, 18), date(2026, 9, 20)),
            A._daily_history_range({"last_daily_run": "2026-09-18"}, "2026-09-21"),
        )

    def daily_finish(self):
        """A book finished since the last poll, on a day whose daily run is done."""
        self.initialized_state(
            last_daily_run="2026-09-21",
            books={"item-1": {"handled_started_at": 100, "handled_finished_at": None}},
        )
        A.get_abs_progress = lambda user_id: {"item-1": progress(finished=True, finished_at=200)}
        A.get_abs_books = lambda *args, **kwargs: self.fail("no daily run is due")
        A.get_abs_book = lambda user_id, item_id, item_progress: book(finished=True)
        order = []
        A.do_sync = lambda user_id, books, **kwargs: (
            order.append("sync") or [{"status": "success", "title": candidate["title"]} for candidate in books]
        )
        return order

    def test_a_daily_finish_writes_its_days_through_today_before_it_is_marked_read(self):
        order = self.daily_finish()
        A._finish_daily_history = lambda user_id, candidate, client, label, start, end: (
            order.append(("history", start, end)) or True
        )
        A._poll_user(self.user, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))

        self.assertEqual([("history", date(2026, 9, 20), date(2026, 9, 21)), "sync"], order)
        self.assertEqual(200, A._scheduler_store.get(self.USER_ID)["books"]["item-1"]["handled_finished_at"])

    def test_a_failed_finish_history_holds_the_finish_for_the_next_poll(self):
        order = self.daily_finish()
        A._finish_daily_history = lambda *args: order.append("history") and False
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)

        self.assertEqual(["history", "history"], order)
        self.assertIsNone(A._scheduler_store.get(self.USER_ID)["books"]["item-1"]["handled_finished_at"])

    def test_the_daily_run_leaves_finished_books_out_of_history(self):
        self.initialized_state(books={
            "item-1": {"handled_started_at": 100, "handled_finished_at": None},
            "item-2": {"handled_started_at": 100, "handled_finished_at": 200},
        })
        A.get_abs_progress = lambda user_id: {
            "item-1": progress(),
            "item-2": {**progress(finished=True, finished_at=200), "libraryItemId": "item-2"},
        }
        finished = {**book(finished=True), "abs_item_id": "item-2", "state_key": "item-2", "title": "Done"}
        A.get_abs_books = lambda *args, **kwargs: [book(), finished]
        A.do_sync = lambda user_id, books, **kwargs: [{"status": "success", "title": b["title"]} for b in books]
        seen = []
        A._daily_history_sync = lambda user_id, books, *args, **kwargs: (
            seen.extend(candidate["abs_item_id"] for candidate in books) or True
        )
        A._poll_user(self.user, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))

        self.assertEqual(["item-1"], seen)
        self.assertEqual("2026-09-21", A._scheduler_store.get(self.USER_ID)["last_daily_run"])

    def test_failed_daily_history_is_retried_without_advancing_the_day(self):
        self.initialized_state()
        snapshot = {"item-1": progress()}
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: [book()]
        A.get_abs_book = lambda user_id, item_id, item_progress: book()
        calls = []

        def fake_sync(user_id, books, start_before_finish=None, **kwargs):
            return [{"status": "success", "title": candidate["title"]} for candidate in books]

        A.do_sync = fake_sync
        A._daily_history_sync = lambda *args, **kwargs: calls.append(args) or False
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        A._poll_user(self.user, now)
        A._poll_user(self.user, now)

        state = A._scheduler_store.get(self.USER_ID)
        self.assertEqual(2, len(calls))
        self.assertNotIn("last_daily_run", state)

    def use_real_daily_run(self, books, *, status_ok=True):
        """Run the real do_sync and daily history against a fake StoryGraph,
        with a confirmed edition for every title except 'Obscure Podcast'."""
        A.do_sync = self.real_sync
        A._daily_history_sync = self.real_daily_history
        snapshot = {
            candidate["abs_item_id"]: {**progress(), "libraryItemId": candidate["abs_item_id"]}
            for candidate in books
        }
        self.initialized_state(
            last_daily_run="2026-09-21",
            books={item_id: {"handled_started_at": 100, "handled_finished_at": None} for item_id in snapshot},
        )
        A.get_abs_progress = lambda user_id: snapshot
        A.get_abs_books = lambda *args, **kwargs: [dict(candidate) for candidate in books]
        A.get_abs_listening_sessions = lambda user_id, item_id: [
            {"date": "2026-09-21", "currentTime": 600, "timeListening": 600},
        ]
        sg = {"searches": [], "writes": [], "journal": {}, "label": "currently reading"}
        A._edition_store.get(self.USER_ID)["books"] = {
            candidate["abs_item_id"]: {
                "state": "confirmed",
                "edition": {"storygraph_book_id": f"sg-{candidate['title']}"},
            }
            for candidate in books
            if candidate["title"] != "Obscure Podcast"
        }

        class FakeStoryGraph:
            def __init__(self, *args):
                pass

            def check_auth(self):
                return True

            def load_editions(self, query, language=None):
                sg["searches"].append(query)
                return []

            def get_book_page(self, book_id):
                return f'<button class="read-status-label">{sg["label"]}</button>'

            def ensure_status(self, book_id, status, html=None):
                return status_ok, True, ""

            def mark_read(self, book_id, started=None, finished=None, html=None):
                return self.ensure_status(book_id, "read")

            def start_reading(self, book_id, started=None, html=None):
                return self.ensure_status(book_id, "currently-reading")

            def get_logged_progress_dates(self, book_id):
                return {date for (logged_id, date) in sg["journal"] if logged_id == book_id}

            def add_dated_progress_entry(self, book_id, date, percent):
                sg["writes"].append((book_id, date))
                sg["journal"][(book_id, date)] = percent
                return True

        A.StoryGraphClient = FakeStoryGraph
        return sg

    def test_unmatched_book_does_not_block_the_daily_run(self):
        good = book()
        obscure = {**book(), "abs_item_id": "item-2", "state_key": "item-2", "title": "Obscure Podcast"}
        sg = self.use_real_daily_run([good, obscure])
        now = datetime(2026, 9, 22, 6, tzinfo=timezone.utc)

        A._poll_user(self.user, now)
        A._poll_user(self.user, now)

        self.assertEqual([("sg-A Book", "2026-09-21")], sg["writes"])
        self.assertEqual("2026-09-22", A._scheduler_store.get(self.USER_ID)["last_daily_run"])
        # Sync never searches StoryGraph itself; only a person's lookup does.
        self.assertEqual([], sg["searches"])

    def test_transient_status_failure_retries_the_day(self):
        sg = self.use_real_daily_run([book()], status_ok=False)
        A._poll_user(self.user, datetime(2026, 9, 22, 6, tzinfo=timezone.utc))

        self.assertEqual([], sg["writes"])
        self.assertEqual("2026-09-21", A._scheduler_store.get(self.USER_ID)["last_daily_run"])

    def test_finish_history_writes_the_last_days_unless_the_book_is_already_read(self):
        sg = self.use_real_daily_run([])
        A._edition_store.get(self.USER_ID)["books"] = {
            "item-1": {"state": "confirmed", "edition": {"storygraph_book_id": "sg-A Book"}},
        }
        args = (self.USER_ID, book(finished=True), A.StoryGraphClient(), "jordan", date(2026, 9, 20), date(2026, 9, 21))

        sg["label"] = "read"
        self.assertTrue(A._finish_daily_history(*args))
        self.assertEqual([], sg["writes"])

        sg["label"] = "currently reading"
        self.assertTrue(A._finish_daily_history(*args))
        self.assertEqual([("sg-A Book", "2026-09-21")], sg["writes"])

    def test_finish_history_waits_out_an_auto_confirm_hold(self):
        sg = self.use_real_daily_run([])
        sg["label"] = "currently reading"
        entry = {"state": "confirmed", "edition": {"storygraph_book_id": "sg-A Book"}, "auto_confirmed_at": A.time.time()}
        A._edition_store.get(self.USER_ID)["books"] = {"item-1": entry}
        args = (self.USER_ID, book(finished=True), A.StoryGraphClient(), "jordan", date(2026, 9, 20), date(2026, 9, 21))

        self.assertTrue(A._finish_daily_history(*args))
        self.assertEqual([], sg["writes"])

        entry["auto_confirmed_at"] -= A.POLL_INTERVAL
        self.assertTrue(A._finish_daily_history(*args))
        self.assertEqual([("sg-A Book", "2026-09-21")], sg["writes"])

    def test_daily_history_only_writes_to_a_confirmed_edition(self):
        sg = self.use_real_daily_run([])
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 10.0,
            "status": "currently-reading",
            "storygraph_book_id": "sg-unconfirmed",
        }
        args = (self.USER_ID, [book()], date(2026, 9, 21), date(2026, 9, 21), A.StoryGraphClient(), "jordan")
        self.assertTrue(A._daily_history_sync(*args))
        self.assertEqual([], sg["writes"])

        A._edition_store.get(self.USER_ID)["books"]["item-1"] = {
            "state": "confirmed", "edition": {"storygraph_book_id": "sg-confirmed"},
        }
        self.assertTrue(A._daily_history_sync(*args))
        self.assertEqual([("sg-confirmed", "2026-09-21")], sg["writes"])

    def candidates(self, scope="in_progress_finished", **item_progress):
        return A._frequent_sync_candidates(self.USER_ID, {"item-1": progress(**item_progress)}, scope)

    def test_frequent_threshold_uses_durable_sync_position(self):
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 10.0,
            "current_minutes": 10.0,
            "status": "currently-reading",
        }
        self.assertEqual([], self.candidates(current=14.9 * 60))
        self.assertEqual(["item-1"], self.candidates(current=15 * 60))

    def test_frequent_mode_does_not_recheck_an_already_synced_finish(self):
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 100.0,
            "current_minutes": 100.0,
            "status": "read",
        }
        self.assertEqual([], self.candidates(current=6000, finished=True))
        del A._sync_store.get(self.USER_ID)["item-1"]
        self.assertEqual(["item-1"], self.candidates(current=6000, finished=True))

    def test_frequent_candidates_skip_books_without_a_confirmed_edition(self):
        A._edition_store.get(self.USER_ID)["books"] = {}
        self.assertEqual([], self.candidates(current=6000))

    def test_the_in_progress_scope_leaves_out_what_abs_does(self):
        self.assertEqual([], self.candidates("in_progress", current=6000, finished=True))
        self.assertEqual(["item-1"], self.candidates("in_progress", current=6000))

    def test_a_quiet_frequent_poll_fetches_no_books_and_never_reaches_storygraph(self):
        A.set_cfg(self.USER_ID, {"SYNC_MODE": "frequent"})
        self.initialized_state(books={"item-1": {"handled_started_at": 100, "handled_finished_at": None}})
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 10.0, "current_minutes": 10.0, "status": "currently-reading", "storygraph_book_id": "sg-A Book",
        }
        A.get_abs_progress = lambda user_id: {"item-1": progress(current=600)}
        A.get_abs_books = lambda *args, **kwargs: self.fail("fetched the whole scope")
        A.get_abs_book = lambda *args: self.fail("fetched a book that hadn't moved")
        A.do_sync = lambda *args, **kwargs: self.fail("synced with nothing to write")
        A._poll_user(self.user)

    def test_a_frequent_poll_fetches_only_the_book_that_moved(self):
        A.set_cfg(self.USER_ID, {"SYNC_MODE": "frequent"})
        self.initialized_state(books={
            "item-1": {"handled_started_at": 100, "handled_finished_at": None},
            "item-2": {"handled_started_at": 100, "handled_finished_at": None},
        })
        A._edition_store.get(self.USER_ID)["books"]["item-2"] = {
            "state": "confirmed", "edition": {"storygraph_book_id": "sg-Other"},
        }
        A._sync_store.get(self.USER_ID).update({
            "item-1": {"pct": 10.0, "current_minutes": 10.0, "status": "currently-reading"},
            "item-2": {"pct": 10.0, "current_minutes": 10.0, "status": "currently-reading"},
        })
        A.get_abs_progress = lambda user_id: {
            "item-1": progress(current=1200),
            "item-2": {**progress(current=600), "libraryItemId": "item-2"},
        }
        A.get_abs_books = lambda *args, **kwargs: self.fail("fetched the whole scope")
        fetched = []
        A.get_abs_book = lambda user_id, item_id, item_progress: fetched.append(item_id) or book(current=20.0)
        A.do_sync = lambda user_id, books, **kwargs: [{"status": "success", "title": b["title"]} for b in books]
        A._poll_user(self.user)
        self.assertEqual(["item-1"], fetched)

    def test_an_unchanged_book_remembers_its_position(self):
        A._sync_store.get(self.USER_ID)["item-1"] = {
            "pct": 10.0, "current_minutes": 10.0, "status": "currently-reading", "storygraph_book_id": "sg-A Book",
        }

        class NoStoryGraph:
            def check_auth(self):
                raise AssertionError("checked the session with nothing to write")

        results = A.do_sync(self.USER_ID, [book(current=16.0)], label="jordan", client=NoStoryGraph())
        self.assertEqual("unchanged", results[0]["status"])
        self.assertEqual(16.0, A._sync_store.get(self.USER_ID)["item-1"]["current_minutes"])


if __name__ == "__main__":
    unittest.main()
