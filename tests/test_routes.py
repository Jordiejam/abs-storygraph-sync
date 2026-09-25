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
from unittest import mock

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
    search_signed_out = False    # StoryGraph bounces the search to its sign-in page
    shelf_label = ""             # the book page's status label; "" is off your shelf

    @classmethod
    def reset(cls):
        cls.journal, cls.editions = {}, [AUDIO, PRINT]
        cls.auth_ok = cls.status_ok = cls.write_result = True
        cls.drop_writes = cls.search_signed_out = False
        cls.shelf_label = ""
        cls.instances = []

    def check_auth(self):
        return FakeStoryGraph.auth_ok

    def get_book_page(self, book_id):
        self.calls.append(("book_page", book_id))
        label = f'<button class="read-status-label">{FakeStoryGraph.shelf_label}</button>' if FakeStoryGraph.shelf_label else ""
        return f"<html><head><title>Picked Edition | The StoryGraph</title></head><body>ok{label}</body></html>"

    def load_editions(self, query, language=None):
        self.calls.append(("load_editions", query))
        self.languages = getattr(self, "languages", []) + [language]
        if FakeStoryGraph.search_signed_out:
            raise A.StoryGraphAuthError("StoryGraph session invalid — update it in Settings")
        return list(FakeStoryGraph.editions)

    def get_logged_progress_dates(self, book_id):
        self.calls.append(("journal", book_id))
        return {d for d, pct in FakeStoryGraph.journal.items() if pct is not None}

    def start_reading(self, book_id, started=None):
        self.calls.append(("start_reading", book_id, started))
        return self.ensure_status(book_id, "currently-reading")

    def mark_read(self, book_id, started=None, finished=None):
        self.calls.append(("mark_read", book_id, started, finished))
        return self.ensure_status(book_id, "read")

    def ensure_status(self, book_id, status):
        self.calls.append(("ensure_status", book_id, status))
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
                           ("SCHEDULER_STATE_DIR", "scheduler_state"),
                           ("EDITIONS_DIR", "editions")):
            setattr(A, attr, f"{self.data_dir}/{path}")
        A.USERS_FILE = f"{self.data_dir}/users.json"
        A._config_store = A._UserJsonStore(A.CONFIG_DIR)
        A._sync_store = A._UserJsonStore(A.SYNC_STATE_DIR)
        A._import_store = A._UserJsonStore(A.IMPORT_STATE_DIR)
        A._scheduler_store = A._UserJsonStore(A.SCHEDULER_STATE_DIR)
        A._edition_store = A._UserJsonStore(A.EDITIONS_DIR)
        A._status_cache.clear()

        self._real_client = A.StoryGraphClient
        self._real_resolve = A._resolve_abs_book
        self._real_sessions = A.get_abs_listening_sessions
        self._real_tag = A.write_storygraph_tag
        FakeStoryGraph.reset()
        A.StoryGraphClient = FakeStoryGraph
        A._resolve_abs_book = lambda uid, iid: dict(self.book)
        A.get_abs_listening_sessions = lambda uid, iid: list(self.sessions)
        self.tags_written = []
        A.write_storygraph_tag = lambda uid, iid, book_id: self.tags_written.append((iid, book_id)) or True

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
        A.write_storygraph_tag = self._real_tag
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

    def stored_edition(self):
        with open(f"{A.EDITIONS_DIR}/{self.user['id']}.json") as f:
            return json.load(f)["books"][self.ITEM]

    def confirm(self, book_id=AUDIO.book_id):
        return self.client.post(f"/api/editions/{self.ITEM}/confirm", json={"storygraph_book_id": book_id})

    def sync_with_recording_fake(self):
        """Run do_sync against a fake that records every StoryGraph write."""
        posted = []
        fake = FakeStoryGraph()
        fake.ensure_status = lambda book_id, status: (posted.append(("status", book_id)), (True, False, None))[1]
        fake.update_progress = lambda book_id, pct, html=None: posted.append(("progress", book_id)) or True
        A.StoryGraphClient = lambda *a, **k: fake
        results = A.do_sync(self.user["id"], [dict(self.book)], label="jordan")
        return results, posted, fake

    def reasons(self, payload):
        return {r["date"]: r.get("reason") or r["status"] for r in payload["results"]}


class ImportPreviewTests(_ImportRouteCase):
    def test_preview_matches_and_persists_the_edition_with_one_editions_fetch(self):
        data = self.preview()
        self.assertEqual(AUDIO.book_id, data["matched_edition"]["storygraph_book_id"])
        self.assertEqual(600.0, data["matched_edition"]["duration_minutes"])
        loads = [c for c in FakeStoryGraph.instances[0].calls if c[0] == "load_editions"]
        self.assertEqual(1, len(loads))
        self.assertEqual("suggested", data["edition_state"])
        self.assertEqual("suggested", self.stored_edition()["state"])

    def test_preview_offers_audio_candidates_when_nothing_matches_confidently(self):
        FakeStoryGraph.editions = [
            EditionCandidate(OTHER_ID, "Part 1 of 2", "Audiobook", 300.0, "X1", "en", "P"),
            PRINT,
        ]
        data = self.preview()
        self.assertEqual("unmatched", data["edition_state"])
        self.assertIsNone(data["matched_edition"])
        self.assertEqual([OTHER_ID], [c["storygraph_book_id"] for c in data["candidates"]])

    def test_preview_without_a_storygraph_session_is_history_only(self):
        A._config_store.get(self.user["id"]).pop("STORYGRAPH_SESSION")
        data = self.preview()
        self.assertFalse(data["storygraph_ready"])
        self.assertIsNone(data["matched_edition"])
        self.assertEqual(3, len(data["days"]))
        self.assertEqual([], FakeStoryGraph.instances, "no StoryGraph session means no StoryGraph calls")

    def test_preview_does_not_treat_a_status_only_entry_as_already_logged(self):
        FakeStoryGraph.journal = {"2026-01-06": None}
        self.preview()
        self.confirm()
        days = self.preview()["days"]
        self.assertEqual([False, False, False], [d["already_logged_on_storygraph"] for d in days])

    def test_preview_marks_a_day_already_logged_when_it_has_a_percentage(self):
        FakeStoryGraph.journal = {"2026-01-06": 20.0}
        self.preview()
        self.confirm()
        by_date = {d["date"]: d for d in self.preview()["days"]}
        self.assertTrue(by_date["2026-01-06"]["already_logged_on_storygraph"])
        self.assertFalse(by_date["2026-01-07"]["already_logged_on_storygraph"])


class ImportWriteTests(_ImportRouteCase):
    def setUp(self):
        super().setUp()
        self.preview()
        self.confirm()
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
            label="jordan",
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

    def storygraph_calls(self):
        return [
            c for c in FakeStoryGraph.instances[-1].calls
            if c[0] in {"start_reading", "ensure_status", "write", "mark_read"}
        ]

    def test_a_finished_book_is_marked_read_after_its_days_with_the_read_spanning_them(self):
        self.book["is_finished"] = True
        body = self.do_import(self.all_keys()).get_json()

        # Entries only land under the read in progress: any on a read book
        # would start a second read.
        self.assertEqual([
            ("start_reading", AUDIO.book_id, A.date_cls(2026, 1, 5)),
            ("ensure_status", AUDIO.book_id, "currently-reading"),
            ("write", "2026-01-05", 10.0), ("write", "2026-01-06", 20.0), ("write", "2026-01-07", 30.0),
            ("mark_read", AUDIO.book_id, A.date_cls(2026, 1, 5), A.date_cls(2026, 1, 7)),
            ("ensure_status", AUDIO.book_id, "read"),
        ], self.storygraph_calls())
        self.assertEqual("marked_read", body["finish"])

    def test_abs_start_and_finish_dates_widen_the_read(self):
        day = 24 * 60 * 60 * 1000
        jan_5 = 1767571200000  # 2026-01-05T00:00:00Z
        self.book.update(is_finished=True, started_at=jan_5 - 2 * day, finished_at=jan_5 + 5 * day)
        self.do_import(self.all_keys())

        mark = next(c for c in self.storygraph_calls() if c[0] == "mark_read")
        self.assertEqual((A.date_cls(2026, 1, 3), A.date_cls(2026, 1, 10)), mark[2:])

    def test_only_the_imported_days_date_the_read(self):
        # A relistened book's history also holds its first listen.
        self.book["is_finished"] = True
        self.do_import(self.all_keys()[1:])

        calls = {c[0]: c for c in self.storygraph_calls()}
        self.assertEqual(A.date_cls(2026, 1, 6), calls["start_reading"][2])
        self.assertEqual((A.date_cls(2026, 1, 6), A.date_cls(2026, 1, 7)), calls["mark_read"][2:])

    def test_a_failed_day_leaves_a_finished_book_currently_reading_for_the_retry(self):
        self.book["is_finished"] = True
        FakeStoryGraph.write_result = {"2026-01-06": False}
        body = self.do_import(self.all_keys()).get_json()

        self.assertNotIn("mark_read", [c[0] for c in self.storygraph_calls()])
        self.assertEqual("left_currently_reading", body["finish"])

    def test_an_unfinished_book_is_not_marked_read(self):
        body = self.do_import(self.all_keys()).get_json()

        # The start is dated to the first listening day, not today.
        self.assertEqual(
            [
                ("start_reading", AUDIO.book_id, A.date_cls(2026, 1, 5)),
                ("ensure_status", AUDIO.book_id, "currently-reading"),
            ],
            [c for c in self.storygraph_calls() if c[0] != "write"],
        )
        self.assertIsNone(body["finish"])

    def test_a_book_already_read_on_storygraph_asks_before_importing_as_a_reread(self):
        FakeStoryGraph.shelf_label = "read"
        keys = self.all_keys()

        response = self.do_import(keys)
        self.assertEqual(409, response.status_code)
        self.assertTrue(response.get_json()["already_read"])
        self.assertEqual([], self.storygraph_calls())

        body = self.client.post(f"/api/history-import/{self.ITEM}", json={"days": keys, "allow_reread": True}).get_json()
        self.assertEqual(3, body["imported"])

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
    def setUp(self):
        super().setUp()
        self.preview()
        self.confirm()
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
        r = self.client.post(f"/api/history-import/{self.ITEM}", json={"days": ["2026-01-05@60.0"]})
        self.assertEqual(400, r.status_code)
        self.assertIn("STORYGRAPH_SESSION", r.get_json()["error"])
        A._config_store.get(self.user["id"])["ABS_TOKEN"] = ""
        r = self.client.get(f"/api/history-import-preview/{self.ITEM}")
        self.assertEqual(400, r.status_code)
        self.assertIn("ABS_TOKEN", r.get_json()["error"])

    def test_read_only_mode_blocks_the_write_but_not_the_preview(self):
        self.all_keys()
        A.READ_ONLY = True
        self.assertEqual(403, self.do_import(["2026-01-07@180.0"]).status_code)
        self.assertEqual(200, self.client.get(f"/api/history-import-preview/{self.ITEM}").status_code)
        # Looking up and confirming an edition write only local state, so they
        # stay available.
        self.assertEqual(200, self.client.post(f"/api/editions/{self.ITEM}/lookup").status_code)
        self.assertEqual(200, self.confirm(OTHER_ID).status_code)


class EditionConfirmationTests(_ImportRouteCase):
    """Only an edition a person confirmed is ever written to, and confirming a
    different one has to beat whatever regular sync last settled on."""

    def test_confirming_a_pasted_url_replaces_a_suggestion(self):
        self.preview()
        self.assertEqual(AUDIO.book_id, self.stored_edition()["edition"]["storygraph_book_id"])
        r = self.confirm(f"https://app.thestorygraph.com/books/{OTHER_ID}?x=1")
        self.assertEqual(200, r.status_code)
        entry = self.stored_edition()
        self.assertEqual("confirmed", entry["state"])
        self.assertEqual(OTHER_ID, entry["edition"]["storygraph_book_id"])
        self.assertEqual("Picked Edition", entry["edition"]["title"])

    def test_confirming_a_candidate_keeps_its_details_without_fetching_its_page(self):
        self.preview()
        self.confirm(AUDIO.book_id)
        edition = self.stored_edition()["edition"]
        self.assertEqual(600.0, edition["duration_minutes"])
        self.assertEqual([], [c for f in FakeStoryGraph.instances for c in f.calls if c[0] == "book_page"])

    def test_confirming_tags_the_abs_book_with_the_edition(self):
        r = self.confirm(OTHER_ID).get_json()
        self.assertEqual([(self.ITEM, OTHER_ID)], self.tags_written)
        self.assertIsNone(r["tag_error"])

    def test_a_failed_tag_still_confirms_and_says_why(self):
        def refuse(uid, iid, book_id):
            resp = A.req.Response()
            resp.status_code = 403
            raise A.req.HTTPError(response=resp)
        A.write_storygraph_tag = refuse
        r = self.confirm(OTHER_ID)
        self.assertEqual(200, r.status_code)
        self.assertIn("isn't allowed to update books", r.get_json()["tag_error"])
        self.assertEqual("confirmed", self.stored_edition()["state"])

    def test_read_only_mode_never_tags_abs(self):
        A.READ_ONLY = True
        self.assertEqual(200, self.confirm(OTHER_ID).status_code)
        self.assertEqual([], self.tags_written)

    def test_a_signed_out_session_never_confirms_a_pasted_edition(self):
        def signed_out(self, book_id):
            raise A.StoryGraphAuthError("StoryGraph session invalid — update it in Settings")
        with mock.patch.object(FakeStoryGraph, "get_book_page", signed_out):
            r = self.confirm(OTHER_ID)
        self.assertEqual(401, r.status_code)
        self.assertNotIn(self.ITEM, A._editions(self.user["id"]))

    def test_rejects_something_that_is_not_a_book_id(self):
        self.assertEqual(400, self.confirm("the-book-i-meant").status_code)

    def test_import_refuses_a_suggested_edition_until_it_is_confirmed(self):
        self.assertEqual("suggested", self.preview()["edition_state"])
        r = self.do_import(["2026-01-07@180.0"])
        self.assertEqual(400, r.status_code)
        self.assertIn("Confirm a StoryGraph edition", r.get_json()["error"])
        self.assertEqual([], [c for f in FakeStoryGraph.instances for c in f.calls if c[0] == "write"])

    def test_sync_never_writes_to_an_unconfirmed_suggestion_or_searches(self):
        A._sync_store.get(self.user["id"])[self.ITEM] = {
            "pct": 10.0, "status": "currently-reading", "storygraph_book_id": OTHER_ID,
        }
        self.preview()  # suggests AUDIO
        results, posted, fake = self.sync_with_recording_fake()
        self.assertEqual(["needs_edition"], [r["status"] for r in results])
        self.assertEqual([], posted)
        self.assertEqual([], fake.calls, "sync must not search StoryGraph for an edition itself")

    def test_sync_retargets_a_book_to_a_newly_confirmed_edition(self):
        A._sync_store.get(self.user["id"])[self.ITEM] = {
            "pct": 50.0, "status": "currently-reading", "storygraph_book_id": AUDIO.book_id,
        }
        self.confirm(OTHER_ID)
        results, posted, _ = self.sync_with_recording_fake()
        self.assertEqual(["success"], [r["status"] for r in results])
        self.assertEqual({OTHER_ID}, {book_id for _, book_id in posted})
        self.assertEqual(OTHER_ID, A._sync_store.get(self.user["id"])[self.ITEM]["storygraph_book_id"])

    def test_short_reread_writes_start_before_the_unchanged_finish(self):
        user_id = self.user["id"]
        self.confirm()
        finished_book = dict(self.book, progress_percent=100.0, current_minutes=600.0, is_finished=True)
        A._sync_store.get(user_id)[self.ITEM] = {
            "pct": 100.0, "status": "read", "storygraph_book_id": AUDIO.book_id,
        }
        statuses = []
        fake = FakeStoryGraph()
        fake.ensure_status = lambda book_id, status: (statuses.append(status), (True, False, None))[1]
        fake.update_progress = lambda book_id, pct, html=None: True
        A.StoryGraphClient = lambda *a, **k: fake

        results = A.do_sync(user_id, [finished_book], label="jordan", start_before_finish={self.ITEM})
        self.assertEqual(["success"], [result["status"] for result in results])
        self.assertEqual(["currently-reading", "read"], statuses)


    def test_a_finish_dates_its_read_from_abs_in_the_users_timezone(self):
        user_id = self.user["id"]
        self.confirm()
        A.set_cfg(user_id, {"TIMEZONE": "Pacific/Auckland"})
        # 2026-01-05 13:00 and 2026-01-09 12:00 UTC are the next day in Auckland.
        finished_book = dict(
            self.book, progress_percent=100.0, current_minutes=600.0, is_finished=True,
            started_at=1767618000000, finished_at=1767960000000,
        )
        marked = []
        fake = FakeStoryGraph()
        fake.mark_read = lambda book_id, started=None, finished=None: marked.append((started, finished)) or (True, False, None)
        A.StoryGraphClient = lambda *a, **k: fake

        A.do_sync(user_id, [finished_book], label="jordan")
        self.assertEqual([(A.date_cls(2026, 1, 6), A.date_cls(2026, 1, 10))], marked)


class EditionsPageTests(_ImportRouteCase):
    def setUp(self):
        super().setUp()
        self._real_books = A.get_abs_books
        A.get_abs_books = lambda user_id, scope, **kwargs: [dict(self.book)]

    def tearDown(self):
        A.get_abs_books = self._real_books
        super().tearDown()

    def lookup(self, **body):
        return self.client.post(f"/api/editions/{self.ITEM}/lookup", json=body)

    def test_the_page_renders_and_the_list_starts_unchecked_without_touching_storygraph(self):
        self.assertEqual(200, self.client.get("/editions").status_code)
        data = self.client.get("/api/editions").get_json()
        self.assertEqual(["unchecked"], [b["state"] for b in data["books"]])
        self.assertEqual([], FakeStoryGraph.instances)

    def test_a_lookup_records_a_suggestion_and_the_list_shows_it(self):
        row = self.lookup().get_json()
        self.assertEqual("suggested", row["state"])
        self.assertEqual(AUDIO.book_id, row["edition"]["storygraph_book_id"])
        listed = self.client.get("/api/editions").get_json()["books"][0]
        self.assertEqual("suggested", listed["state"])

    def test_a_lookup_filters_by_the_books_language_when_abs_knows_it(self):
        self.book["language"] = "eng"
        self.lookup()
        self.assertEqual(["english"], FakeStoryGraph.instances[-1].languages)

    def test_a_lookup_can_use_a_persons_own_search_words(self):
        self.lookup(query="The Book dramatised")
        self.assertEqual([("load_editions", "The Book dramatised")], FakeStoryGraph.instances[-1].calls)

    def test_a_lookup_never_unconfirms_a_confirmed_edition(self):
        self.confirm(OTHER_ID)
        FakeStoryGraph.editions = [PRINT]
        row = self.lookup().get_json()
        self.assertEqual("confirmed", row["state"])
        self.assertEqual(OTHER_ID, row["edition"]["storygraph_book_id"])
        self.assertEqual([PRINT.book_id], [c["storygraph_book_id"] for c in row["candidates"]])

    def test_a_lookup_suggests_the_edition_abs_is_tagged_with(self):
        self.book["storygraph_tag"] = PRINT.book_id
        row = self.lookup().get_json()
        self.assertEqual((PRINT.book_id, "tagged"), (row["edition"]["storygraph_book_id"], row["reason"]["code"]))
        self.assertEqual(PRINT.book_id, row["storygraph_tag"])
        self.assertNotIn("tagged_unlisted", row["reason"])

    def test_a_tagged_edition_the_search_missed_is_still_suggested_by_its_page_title(self):
        self.book["storygraph_tag"] = OTHER_ID
        row = self.lookup().get_json()
        self.assertEqual("suggested", row["state"])
        self.assertEqual((OTHER_ID, "Picked Edition"), (row["edition"]["storygraph_book_id"], row["edition"]["title"]))
        self.assertTrue(row["reason"]["tagged_unlisted"])
        self.assertIn(AUDIO.book_id, [c["storygraph_book_id"] for c in row["candidates"]])

    def sync_tags(self, books):
        A.get_abs_books = lambda user_id, scope, **kwargs: books
        return self.client.post("/api/editions/sync-tags", json={}).get_json()

    def test_tag_sync_confirms_tagged_books_and_tags_confirmed_ones(self):
        self.lookup()  # suggests AUDIO, so its details are on file
        tagged = dict(self.book, storygraph_tag=AUDIO.book_id)
        bare = dict(self.book, abs_item_id="item-bare-tag", title="Bare", storygraph_tag=OTHER_ID)
        untagged = dict(self.book, abs_item_id="item-untagged", title="Untagged")
        A._editions(self.user["id"])["item-untagged"] = {
            "state": "confirmed", "edition": {"storygraph_book_id": PRINT.book_id}, "candidates": [],
        }
        result = self.sync_tags([tagged, bare, untagged])
        self.assertEqual((2, 1, 0, []), (result["confirmed"], result["tagged"], result["untagged"], result["conflicts"]))
        self.assertEqual([("item-untagged", PRINT.book_id)], self.tags_written)
        entry = self.stored_edition()
        self.assertEqual(("confirmed", 600.0), (entry["state"], entry["edition"]["duration_minutes"]))
        self.assertEqual(OTHER_ID, A._confirmed_edition_id(self.user["id"], "item-bare-tag"))
        self.assertEqual([], FakeStoryGraph.instances[1:], "tag sync must not touch StoryGraph")

    def test_tag_sync_leaves_a_different_confirmed_edition_alone_and_reports_it(self):
        self.confirm(OTHER_ID)
        self.tags_written.clear()
        result = self.sync_tags([dict(self.book, storygraph_tag=AUDIO.book_id)])
        self.assertEqual(["The Book"], result["conflicts"])
        self.assertEqual(OTHER_ID, self.stored_edition()["edition"]["storygraph_book_id"])
        self.assertEqual([], self.tags_written)

    def test_tag_sync_stops_writing_once_abs_refuses(self):
        attempts = []
        def refuse(uid, iid, book_id):
            attempts.append(iid)
            resp = A.req.Response()
            resp.status_code = 403
            raise A.req.HTTPError(response=resp)
        A.write_storygraph_tag = refuse
        for item_id in ("item-one-aaaa", "item-two-aaaa"):
            A._editions(self.user["id"])[item_id] = {
                "state": "confirmed", "edition": {"storygraph_book_id": OTHER_ID}, "candidates": [],
            }
        books = [dict(self.book, abs_item_id=i) for i in ("item-one-aaaa", "item-two-aaaa")]
        result = self.sync_tags(books)
        self.assertEqual((0, 2), (result["tagged"], result["untagged"]))
        self.assertIn("isn't allowed", result["tag_error"])
        self.assertEqual(1, len(attempts))

    def test_tag_sync_writes_no_tags_in_read_only_mode(self):
        A._editions(self.user["id"])[self.ITEM] = {
            "state": "confirmed", "edition": {"storygraph_book_id": OTHER_ID}, "candidates": [],
        }
        A.READ_ONLY = True
        result = self.sync_tags([dict(self.book)])
        self.assertEqual((0, 1, True), (result["tagged"], result["untagged"], result["read_only"]))
        self.assertEqual([], self.tags_written)

    def test_a_lookup_records_why_it_did_or_did_not_match(self):
        self.assertEqual("identifier", self.lookup().get_json()["reason"]["code"])
        FakeStoryGraph.editions = [
            EditionCandidate(OTHER_ID, "Part 1 of 2", "Audiobook", 300.0, "X1", "en", "P"),
        ]
        reason = self.lookup(query="The Book part").get_json()["reason"]
        self.assertEqual("runtime_mismatch", reason["code"])
        self.assertEqual(-300.0, reason["closest_delta_minutes"])
        self.assertEqual("The Book part", reason["query"])
        self.assertEqual(600.0, reason["abs_runtime_minutes"])

    def test_with_no_audio_match_the_edition_you_have_read_becomes_the_suggestion(self):
        yours = EditionCandidate(OTHER_ID, "The Book", "Hardcover", None, "978", "English", "Pub", read_by_you=True)
        FakeStoryGraph.editions = [
            EditionCandidate(AUDIO.book_id, "Part 1 of 2", "Audiobook", 300.0, "X1", "en", "P"),
            yours,
        ]
        row = self.lookup().get_json()
        self.assertEqual("suggested", row["state"])
        self.assertEqual(OTHER_ID, row["edition"]["storygraph_book_id"])
        self.assertTrue(row["reason"]["read_edition"]["fallback"])
        # Offered alongside the audio editions even though it's a hardcover.
        self.assertEqual([AUDIO.book_id, OTHER_ID], [c["storygraph_book_id"] for c in row["candidates"]])

    def test_with_no_audio_edition_every_edition_is_offered(self):
        FakeStoryGraph.editions = [PRINT]
        row = self.lookup().get_json()
        self.assertEqual("unmatched", row["state"])
        self.assertEqual([PRINT.book_id], [c["storygraph_book_id"] for c in row["candidates"]])

    def test_a_signed_out_session_is_reported_rather_than_recorded_as_no_match(self):
        FakeStoryGraph.search_signed_out = True
        r = self.lookup()
        self.assertEqual(401, r.status_code)
        self.assertEqual("unchecked", self.client.get("/api/editions").get_json()["books"][0]["state"])

    def test_the_list_flags_a_book_sync_already_wrote_to_a_different_edition(self):
        A._sync_store.get(self.user["id"])[self.ITEM] = {
            "pct": 10.0, "status": "currently-reading", "storygraph_book_id": OTHER_ID,
        }
        A._edition_store.get(self.user["id"])["books"] = {}
        self.lookup()
        listed = self.client.get("/api/editions").get_json()["books"][0]
        self.assertEqual(OTHER_ID, listed["synced_book_id"])
        self.assertEqual(AUDIO.book_id, listed["edition"]["storygraph_book_id"])


class EditionMigrationTests(_ImportRouteCase):
    def test_older_editions_migrate_and_only_manual_picks_count_as_confirmed(self):
        user_id = self.user["id"]
        A._import_store.get(user_id).update({
            "item-manual1": {"edition": {"storygraph_book_id": OTHER_ID, "title": "Picked", "source": "manual"},
                             "imported_days": {"2026-01-05@60.0": {}}},
            "item-auto0001": {"edition": {"storygraph_book_id": AUDIO.book_id, "title": "Auto", "source": "auto"}},
        })
        A._sync_store.get(user_id).update({
            "item-synced01": {"pct": 5.0, "status": "currently-reading", "storygraph_book_id": AUDIO.book_id},
            "An Old Title": {"pct": 5.0, "status": "currently-reading", "storygraph_book_id": AUDIO.book_id},
        })

        A._edition_store.get(user_id).clear()
        A._migrate_legacy_state(user_id)
        A._migrate_legacy_state(user_id)  # idempotent
        editions = A._editions(user_id)
        self.assertNotIn("An Old Title", A._sync_store.get(user_id))
        self.assertEqual({
            "item-manual1": "confirmed", "item-auto0001": "suggested", "item-synced01": "suggested",
        }, {item_id: entry["state"] for item_id, entry in editions.items()})
        self.assertNotIn("source", editions["item-manual1"]["edition"])
        self.assertNotIn("edition", A._import_store.get(user_id)["item-manual1"])
        self.assertIn("imported_days", A._import_store.get(user_id)["item-manual1"])
        self.assertEqual(OTHER_ID, A._confirmed_edition_id(user_id, "item-manual1"))
        self.assertIsNone(A._confirmed_edition_id(user_id, "item-synced01"))


class AdminPageTests(_ImportRouteCase):
    def test_non_admins_get_no_admin_markup_and_no_admin_api(self):
        self.assertIn("log-box", self.client.get("/").get_data(as_text=True))

        A.create_user(username="plain", password="pw")
        self.client.get("/logout")
        self.client.post("/login", data={"username": "plain", "password": "pw"})
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("users-list", html)
        self.assertNotIn("log-box", html)
        self.assertEqual(403, self.client.get("/api/logs").status_code)
        self.assertEqual(403, self.client.get("/api/users").status_code)


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
