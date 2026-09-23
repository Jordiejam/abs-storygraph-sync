"""Route-level tests for the History Import flow.

The pure reconstruction and parsing logic is covered in test_history.py and
test_journal.py. What matters here is the orchestration the routes do around it:
what gets skipped, what gets written, what survives a partial failure, and what
the write endpoint refuses to trust.

StoryGraph is replaced with a fake that records every call and keeps an
in-memory journal, so these exercise the real Flask routes and the real state
files without touching the network.
"""

import json
import os
import shutil
import tempfile
import unittest

import app as A
from matcher import EditionCandidate


AUDIO = EditionCandidate(
    "aaaaaaaa-0000-0000-0000-000000000001", "The Book (Audio)", "Audiobook",
    600.0, "B01", "English", "Pub",
)
PRINT = EditionCandidate(
    "cccccccc-0000-0000-0000-000000000003", "The Book", "Paperback",
    None, "978", "English", "Pub",
)
OTHER_ID = "bbbbbbbb-0000-0000-0000-000000000002"

SESSIONS = [
    {"date": "2026-01-05", "currentTime": 3600, "timeListening": 3600},
    {"date": "2026-01-06", "currentTime": 7200, "timeListening": 3600},
    {"date": "2026-01-07", "currentTime": 10800, "timeListening": 3600},
]


class FakeStoryGraph:
    """An in-memory StoryGraph. `journal` maps date -> percent, with None
    standing for a status-only entry such as "Started reading"."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeStoryGraph.instances.append(self)

    # knobs the tests set on the class
    journal = {}
    editions = [AUDIO, PRINT]
    auth_ok = True
    status_ok = True
    write_result = True          # True/False, or a dict of date -> bool
    drop_writes = False          # accept the POST but never actually record it

    @classmethod
    def reset(cls):
        cls.journal, cls.editions = {}, [AUDIO, PRINT]
        cls.auth_ok = cls.status_ok = cls.write_result = True
        cls.drop_writes = False
        cls.instances = []

    def check_auth(self):
        return FakeStoryGraph.auth_ok

    def get_book_page(self, book_id):
        self.calls.append(("book_page", book_id))
        return "<html><head><title>Picked Edition | The StoryGraph</title></head><body>ok</body></html>"

    def load_editions(self, title, author):
        self.calls.append(("load_editions", title))
        return list(FakeStoryGraph.editions)

    def match_audio_edition(self, candidates, title, duration_minutes=0, identifiers=None):
        return A.choose_audio_edition(
            candidates, target_duration_minutes=duration_minutes, identifiers=identifiers or []
        )

    def get_logged_progress_dates(self, book_id):
        self.calls.append(("journal", book_id))
        return {d for d, pct in FakeStoryGraph.journal.items() if pct is not None}

    def ensure_status(self, book_id, status):
        self.calls.append(("ensure_status", book_id))
        # The real call creates a status-only journal entry dated today; a
        # regression here would let it block that day's own import.
        FakeStoryGraph.journal.setdefault("2026-01-05", None)
        return FakeStoryGraph.status_ok, False, None

    def add_dated_progress_entry(self, book_id, date, percent):
        self.calls.append(("write", date, percent))
        outcome = FakeStoryGraph.write_result
        ok = outcome.get(date, True) if isinstance(outcome, dict) else outcome
        if ok and not FakeStoryGraph.drop_writes:
            FakeStoryGraph.journal[date] = percent
        return ok


class _ImportRouteCase(unittest.TestCase):
    """Shared fixture: a throwaway data dir, a logged-in user and a fake
    StoryGraph. Holds no tests of its own, so subclasses don't re-run them."""

    ITEM = "item-abcdefgh"

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="abs-sg-case-")
        for attr, path in (("CONFIG_DIR", "config"), ("SYNC_STATE_DIR", "sync_state"),
                           ("IMPORT_STATE_DIR", "import_state"),
                           ("SCHEDULER_STATE_DIR", "scheduler_state")):
            setattr(A, attr, f"{self.data_dir}/{path}")
        A.USERS_FILE = f"{self.data_dir}/users.json"
        A._config_store = A._UserJsonStore(A.CONFIG_DIR)
        A._sync_store = A._UserJsonStore(A.SYNC_STATE_DIR)
        A._import_store = A._UserJsonStore(A.IMPORT_STATE_DIR)
        A._scheduler_store = A._UserJsonStore(A.SCHEDULER_STATE_DIR)
        A._status_cache.clear()

        self._real_client = A.StoryGraphClient
        self._real_resolve = A._resolve_abs_book
        self._real_sessions = A.get_abs_listening_sessions
        FakeStoryGraph.reset()
        A.StoryGraphClient = FakeStoryGraph
        A._resolve_abs_book = lambda uid, iid: dict(self.book)
        A.get_abs_listening_sessions = lambda uid, iid: list(self.sessions)

        self.book = {
            "abs_item_id": self.ITEM, "title": "The Book", "author": "An Author",
            "identifiers": ["B01"], "progress_percent": 50.0, "current_minutes": 300.0,
            "duration_minutes": 600.0, "is_finished": False, "state_key": self.ITEM,
        }
        self.sessions = SESSIONS

        A.READ_ONLY = False
        A.app.config["TESTING"] = True
        self.user = A.create_user(username="jordan", password="pw", is_admin=True)
        A.set_cfg(self.user["id"], {
            "ABS_URL": "http://abs", "ABS_TOKEN": "tok", "STORYGRAPH_SESSION": "sess",
        })
        self.client = A.app.test_client()
        self.client.post("/login", data={"username": "jordan", "password": "pw"})

    def tearDown(self):
        A.StoryGraphClient = self._real_client
        A._resolve_abs_book = self._real_resolve
        A.get_abs_listening_sessions = self._real_sessions
        A.READ_ONLY = False
        shutil.rmtree(self.data_dir, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def preview(self, item_id=None):
        return self.client.get(f"/api/history-import-preview/{item_id or self.ITEM}").get_json()

    def do_import(self, keys):
        return self.client.post(f"/api/history-import/{self.ITEM}", json={"days": keys})

    def all_keys(self):
        return [day["key"] for day in self.preview()["days"]]

    def stored(self):
        with open(f"{A.IMPORT_STATE_DIR}/{self.user['id']}.json") as f:
            return json.load(f)[self.ITEM]

    def reasons(self, payload):
        return {r["date"]: r.get("reason") or r["status"] for r in payload["results"]}


class ImportPreviewTests(_ImportRouteCase):
    def test_preview_matches_and_persists_the_edition_with_one_editions_fetch(self):
        data = self.preview()
        self.assertEqual(AUDIO.book_id, data["matched_edition"]["storygraph_book_id"])
        self.assertEqual(600.0, data["matched_edition"]["duration_minutes"])
        loads = [c for c in FakeStoryGraph.instances[0].calls if c[0] == "load_editions"]
        self.assertEqual(1, len(loads))
        self.assertEqual("auto", self.stored()["edition"]["source"])

    def test_preview_offers_audio_candidates_when_nothing_matches_confidently(self):
        FakeStoryGraph.editions = [
            EditionCandidate(OTHER_ID, "Part 1 of 2", "Audiobook", 300.0, "X1", "en", "P"),
            PRINT,
        ]
        data = self.preview()
        self.assertIsNone(data["matched_edition"])
        self.assertEqual([OTHER_ID], [c["storygraph_book_id"] for c in data["candidates"]])

    def test_preview_does_not_treat_a_status_only_entry_as_already_logged(self):
        FakeStoryGraph.journal = {"2026-01-06": None}
        days = self.preview()["days"]
        self.assertEqual([False, False, False], [d["already_logged_on_storygraph"] for d in days])

    def test_preview_marks_a_day_already_logged_when_it_has_a_percentage(self):
        FakeStoryGraph.journal = {"2026-01-06": 20.0}
        by_date = {d["date"]: d for d in self.preview()["days"]}
        self.assertTrue(by_date["2026-01-06"]["already_logged_on_storygraph"])
        self.assertFalse(by_date["2026-01-07"]["already_logged_on_storygraph"])


class ImportWriteTests(_ImportRouteCase):
    def test_status_only_sync_does_not_create_an_undated_progress_entry(self):
        user_id = self.user["id"]
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 10.0,
            "status": "currently-reading",
            "storygraph_book_id": AUDIO.book_id,
        }
        client = FakeStoryGraph()

        result = A.do_sync(
            user_id,
            [dict(self.book)],
            write_progress=False,
            client=client,
        )

        self.assertEqual("success", result[0]["status"])
        self.assertEqual([], [call for call in client.calls if call[0] == "write"])

    def test_daily_sync_reuses_import_state_and_only_reconciles_its_date_range(self):
        user_id = self.user["id"]
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 30.0,
            "status": "currently-reading",
            "storygraph_book_id": AUDIO.book_id,
        }
        client = FakeStoryGraph()

        ok = A._daily_history_sync(
            user_id,
            [dict(self.book)],
            A.date_cls.fromisoformat("2026-01-06"),
            A.date_cls.fromisoformat("2026-01-06"),
            client,
            "jordan",
        )

        self.assertTrue(ok)
        self.assertEqual(
            [("write", "2026-01-06", 20.0)],
            [call for call in client.calls if call[0] == "write"],
        )
        imported_keys = self.stored()["imported_days"]
        self.assertEqual(["2026-01-06@120.0"], list(imported_keys))

        body = self.do_import(self.all_keys()).get_json()
        self.assertEqual("already_imported", self.reasons(body)["2026-01-06"])
        self.assertEqual(2, body["imported"])

    def test_daily_retry_skips_the_journal_once_every_day_is_imported(self):
        user_id = self.user["id"]
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 30.0,
            "status": "currently-reading",
            "storygraph_book_id": AUDIO.book_id,
        }
        day = A.date_cls.fromisoformat("2026-01-06")
        A._daily_history_sync(user_id, [dict(self.book)], day, day, FakeStoryGraph(), "jordan")

        retry = FakeStoryGraph()
        self.assertTrue(A._daily_history_sync(user_id, [dict(self.book)], day, day, retry, "jordan"))
        self.assertEqual([], retry.calls)

    def test_imports_every_confirmed_day_including_the_ensure_status_day(self):
        keys = self.all_keys()
        body = self.do_import(keys).get_json()
        self.assertEqual(3, body["imported"])
        self.assertEqual({"success"}, {r["status"] for r in body["results"]})
        self.assertEqual(3, len(self.stored()["imported_days"]))

    def test_a_rerun_skips_everything_it_already_imported(self):
        keys = self.all_keys()
        self.do_import(keys)
        body = self.do_import(keys).get_json()
        self.assertEqual(0, body["imported"])
        self.assertEqual({"already_imported"}, set(self.reasons(body).values()))

    def test_skips_a_day_storygraph_already_has_a_percentage_for(self):
        FakeStoryGraph.journal = {"2026-01-06": 20.0}
        body = self.do_import(self.all_keys()).get_json()
        self.assertEqual("already_logged_on_storygraph", self.reasons(body)["2026-01-06"])
        self.assertEqual(2, body["imported"])

    def test_a_partial_failure_still_records_the_days_that_worked(self):
        FakeStoryGraph.write_result = {"2026-01-06": False}
        body = self.do_import(self.all_keys()).get_json()
        self.assertEqual(2, body["imported"])
        self.assertEqual("storygraph_rejected", self.reasons(body)["2026-01-06"])
        recorded = self.stored()["imported_days"]
        self.assertEqual(2, len(recorded))
        self.assertNotIn("2026-01-06", "".join(recorded))

    def test_an_http_success_that_never_reached_the_journal_is_not_recorded(self):
        # A 200/302 from the full-page form doesn't prove StoryGraph saved
        # anything, so nothing counts until it shows up in the journal.
        FakeStoryGraph.drop_writes = True
        body = self.do_import(self.all_keys()).get_json()
        self.assertEqual(0, body["imported"])
        self.assertEqual({"storygraph_did_not_save"}, set(self.reasons(body).values()))
        self.assertEqual({}, self.stored().get("imported_days", {}))

    def test_refuses_to_write_before_an_edition_is_chosen(self):
        FakeStoryGraph.editions = [PRINT]
        self.preview()
        r = self.do_import(["2026-01-07@180.0"])
        self.assertEqual(400, r.status_code)
        self.assertIn("No StoryGraph edition", r.get_json()["error"])

    def test_reports_a_failure_when_the_status_cannot_be_set(self):
        self.all_keys()
        FakeStoryGraph.status_ok = False
        r = self.do_import(["2026-01-07@180.0"])
        self.assertEqual(502, r.status_code)

    def test_rejects_an_invalid_storygraph_session(self):
        self.all_keys()
        FakeStoryGraph.auth_ok = False
        self.assertEqual(401, self.do_import(["2026-01-07@180.0"]).status_code)


class ImportInputTrustTests(_ImportRouteCase):
    def test_ignores_a_checkpoint_key_absent_from_a_fresh_rebuild(self):
        self.all_keys()
        body = self.do_import(["2026-01-07@999.9"]).get_json()
        self.assertEqual(0, body["imported"])
        self.assertEqual("stale_preview", body["results"][0]["reason"])
        self.assertEqual([], [c for c in FakeStoryGraph.instances[-1].calls if c[0] == "write"])

    def test_never_writes_a_date_or_percentage_the_browser_supplied(self):
        keys = self.all_keys()
        # The old payload shape, plus values ABS never reported.
        r = self.client.post(f"/api/history-import/{self.ITEM}", json={"days": [
            {"date": "1999-01-01", "progress_percent": 5000, "end_position_minutes": 7},
        ]})
        self.assertEqual(400, r.status_code)
        # And the values actually written come from the rebuild, not the request.
        self.do_import(keys)
        written = {d: pct for _, d, pct in
                   [c for c in FakeStoryGraph.instances[-1].calls if c[0] == "write"]}
        self.assertEqual({"2026-01-05": 10.0, "2026-01-06": 20.0, "2026-01-07": 30.0}, written)

    def test_malformed_payloads_are_rejected_rather_than_crashing(self):
        self.all_keys()
        for payload in ({"days": ["justastring"]}, {"days": [None]}, {"days": []},
                        {"days": [{"date": "not-a-date"}]}, {}, {"days": "2026-01-07@180.0"},
                        {"days": {"a": 1}}, {"days": 7}):
            r = self.client.post(f"/api/history-import/{self.ITEM}", json=payload)
            self.assertIn(r.status_code, (200, 400), f"{payload} -> {r.status_code}")
            if r.status_code == 200:
                self.assertEqual(0, r.get_json()["imported"], payload)

    def test_rejects_a_malformed_item_id_and_missing_configuration(self):
        self.assertEqual(400, self.client.get("/api/history-import-preview/!!bad!!").status_code)
        A._config_store.get(self.user["id"])["STORYGRAPH_SESSION"] = ""
        r = self.client.get(f"/api/history-import-preview/{self.ITEM}")
        self.assertEqual(400, r.status_code)
        self.assertIn("STORYGRAPH_SESSION", r.get_json()["error"])

    def test_read_only_mode_blocks_the_write_but_not_the_preview(self):
        self.all_keys()
        A.READ_ONLY = True
        self.assertEqual(403, self.do_import(["2026-01-07@180.0"]).status_code)
        self.assertEqual(200, self.client.get(f"/api/history-import-preview/{self.ITEM}").status_code)
        # Pinning an edition writes only local state, so it stays available.
        self.assertEqual(200, self.client.post(
            f"/api/history-import-edition/{self.ITEM}",
            json={"storygraph_book_id": OTHER_ID}).status_code)


class EditionOverrideTests(_ImportRouteCase):
    """A manual pin has to beat both auto-matching and whatever regular sync
    last settled on — otherwise it can't correct a bad match."""

    def test_a_manual_pin_replaces_an_auto_matched_edition(self):
        self.preview()
        self.assertEqual(AUDIO.book_id, self.stored()["edition"]["storygraph_book_id"])
        r = self.client.post(f"/api/history-import-edition/{self.ITEM}", json={
            "storygraph_book_id": f"https://app.thestorygraph.com/books/{OTHER_ID}?x=1",
        })
        self.assertEqual(200, r.status_code)
        edition = self.stored()["edition"]
        self.assertEqual(OTHER_ID, edition["storygraph_book_id"])
        self.assertEqual("manual", edition["source"])
        self.assertEqual("Picked Edition", edition["title"])

    def test_rejects_something_that_is_not_a_book_id(self):
        r = self.client.post(f"/api/history-import-edition/{self.ITEM}",
                             json={"storygraph_book_id": "the-book-i-meant"})
        self.assertEqual(400, r.status_code)

    def test_sync_retargets_a_book_it_had_already_synced_elsewhere(self):
        user_id = self.user["id"]
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 50.0, "status": "currently-reading", "storygraph_book_id": AUDIO.book_id,
        }
        self.client.post(f"/api/history-import-edition/{self.ITEM}", json={"storygraph_book_id": OTHER_ID})

        posted = []
        fake = FakeStoryGraph()
        fake.ensure_status = lambda book_id, status: (posted.append(("status", book_id)), (True, False, None))[1]
        fake.update_progress = lambda book_id, pct, html=None: posted.append(("progress", book_id)) or True
        fake._parse_current_progress = lambda html: None
        A.StoryGraphClient = lambda *a, **k: fake

        results = A.do_sync(user_id, [dict(self.book)])
        self.assertEqual(["success"], [r["status"] for r in results])
        self.assertEqual({OTHER_ID}, {book_id for _, book_id in posted})
        self.assertEqual(OTHER_ID, A._sync_store.get(user_id)[self.ITEM]["storygraph_book_id"])

    def test_an_auto_match_does_not_retarget_a_book_sync_already_handles(self):
        user_id = self.user["id"]
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 10.0, "status": "currently-reading", "storygraph_book_id": OTHER_ID,
        }
        self.preview()  # saves an auto-matched edition pointing at AUDIO
        self.assertEqual("auto", self.stored()["edition"]["source"])

        posted = []
        fake = FakeStoryGraph()
        fake.ensure_status = lambda book_id, status: (posted.append(book_id), (True, False, None))[1]
        fake.update_progress = lambda book_id, pct, html=None: posted.append(book_id) or True
        fake._parse_current_progress = lambda html: None
        A.StoryGraphClient = lambda *a, **k: fake

        A.do_sync(user_id, [dict(self.book)])
        self.assertEqual({OTHER_ID}, set(posted), "an auto match must not hijack a synced book")

    def test_short_reread_writes_start_before_the_unchanged_finish(self):
        user_id = self.user["id"]
        finished_book = dict(self.book, progress_percent=100.0, current_minutes=600.0, is_finished=True)
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 100.0, "status": "read", "storygraph_book_id": AUDIO.book_id,
        }
        statuses = []
        fake = FakeStoryGraph()
        fake.ensure_status = lambda book_id, status: (statuses.append(status), (True, False, None))[1]
        fake.update_progress = lambda book_id, pct, html=None: True
        fake._parse_current_progress = lambda html: None
        A.StoryGraphClient = lambda *a, **k: fake

        results = A.do_sync(user_id, [finished_book], start_before_finish={self.ITEM})
        self.assertEqual(["success"], [result["status"] for result in results])
        self.assertEqual(["currently-reading", "read"], statuses)


class SyncSettingsTests(_ImportRouteCase):
    def test_sync_settings_default_to_frequent_and_midnight(self):
        settings = self.client.get("/api/settings").get_json()
        self.assertEqual("frequent", settings["SYNC_MODE"])
        self.assertEqual("00:00", settings["DAILY_SYNC_TIME"])

    def test_daily_settings_are_validated_and_saved(self):
        self.assertEqual(400, self.client.post("/api/settings", json={"SYNC_MODE": "sometimes"}).status_code)
        self.assertEqual(400, self.client.post("/api/settings", json={"DAILY_SYNC_TIME": "midnight"}).status_code)
        self.assertEqual(400, self.client.post("/api/settings", json={"TIMEZONE": "Middle/Earth"}).status_code)

        response = self.client.post("/api/settings", json={
            "SYNC_MODE": "daily",
            "DAILY_SYNC_TIME": "00:00",
            "TIMEZONE": "Europe/London",
        })
        self.assertEqual(200, response.status_code)
        settings = self.client.get("/api/settings").get_json()
        self.assertEqual("daily", settings["SYNC_MODE"])
        self.assertEqual("00:00", settings["DAILY_SYNC_TIME"])
        self.assertEqual("Europe/London", settings["TIMEZONE"])


if __name__ == "__main__":
    unittest.main()
