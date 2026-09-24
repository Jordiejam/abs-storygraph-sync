"""State-file durability, the status cache, and ABS request fan-out."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import app as A


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise A.req.HTTPError(self.status_code)


def _item(item_id):
    return {"id": item_id, "media": {"duration": 6000, "metadata": {"title": f"Book {item_id}"}}}


class HelperTests(unittest.TestCase):
    USER_ID = "user-1"

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="abs-sg-helpers-")
        A._config_store = A._UserJsonStore(f"{self.data_dir}/config")
        A._status_cache.clear()
        A.set_cfg(self.USER_ID, {"ABS_URL": "http://abs", "ABS_TOKEN": "token"})
        self.real_books = A.get_abs_books

    def tearDown(self):
        A.get_abs_books = self.real_books
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        path = f"{self.data_dir}/state.json"
        A._write_json_atomic(path, {"kept": True})
        with self.assertRaises(TypeError):
            A._write_json_atomic(path, {"bad": object()})
        with open(path) as f:
            self.assertEqual({"kept": True}, json.load(f))
        self.assertFalse([name for name in os.listdir(self.data_dir) if name.endswith(".tmp")])

    def test_status_cache_refetches_when_the_scope_changes(self):
        fetched = []
        A.get_abs_books = lambda user_id, scope: fetched.append(scope) or [{"scope": scope}]

        A.get_cached_books(self.USER_ID, "in_progress")
        A.get_cached_books(self.USER_ID, "in_progress")
        books, ok = A.get_cached_books(self.USER_ID, "library")

        self.assertEqual(["in_progress", "library"], fetched)
        self.assertEqual(([{"scope": "library"}], True), (books, ok))

    def test_in_progress_scope_reads_all_progress_in_one_request(self):
        urls = []
        responses = {
            "http://abs/api/me/items-in-progress": {"libraryItems": [_item("a"), _item("b")]},
            "http://abs/api/me": {"mediaProgress": [
                {"libraryItemId": "a", "progress": 0.5, "currentTime": 3000},
                {"libraryItemId": "b", "progress": 0.1, "currentTime": 600},
            ]},
        }

        def fake_get(url, **kwargs):
            urls.append(url)
            return _FakeResponse(responses[url])

        with mock.patch.object(A.req, "get", fake_get):
            books = A.get_abs_books(self.USER_ID, "in_progress")

        self.assertEqual([50.0, 10.0], [book["progress_percent"] for book in books])
        self.assertEqual(sorted(responses), sorted(urls))

    def test_a_missing_abs_item_is_none_rather_than_an_error(self):
        with mock.patch.object(A.req, "get", lambda url, **kwargs: _FakeResponse({}, 404)):
            self.assertIsNone(A.get_abs_book(self.USER_ID, "gone", {}))


if __name__ == "__main__":
    unittest.main()


def _book_page(label, progress=None):
    """Book-page markup shaped like StoryGraph's: the label carries a long run
    of utility classes, and the progress input puts its value before its name."""
    value = f' value="{progress}"' if progress is not None else ""
    return (
        '<button class="read-status-label text-cyan-700 text-sm font-medium w-full" '
        f'title="Book marked as {label}">{label}</button>'
        f'<input min="0" max="100"{value} class=" read-status-progress-number bg-transparent" '
        'type="number" name="read_status[progress_number]" id="read_status_progress_number" />'
    )


class StoryGraphPageTests(unittest.TestCase):
    def setUp(self):
        self.client = A.StoryGraphClient("session")
        self.posts = []
        self.client._post = lambda path, data: self.posts.append(path) or _FakeResponse({})

    def test_a_matching_status_is_not_posted_again(self):
        ok, already, html = self.client.ensure_status("b1", "currently-reading", html=_book_page("currently reading", 89))

        self.assertEqual((True, True), (ok, already))
        self.assertEqual([], self.posts)
        self.assertEqual(89.0, A._parse_current_progress(html))

    def test_currently_reading_does_not_count_as_read(self):
        ok, already, _ = self.client.ensure_status("b1", "read", html=_book_page("currently reading"))

        self.assertEqual((True, False), (ok, already))
        self.assertEqual(["/update-status.js?book_id=b1&status=read"], self.posts)

    def test_cleared_progress_reads_as_unknown(self):
        self.assertIsNone(A._parse_current_progress(_book_page("currently reading")))
