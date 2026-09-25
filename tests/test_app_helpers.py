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

    def test_a_single_item_fetch_asks_abs_for_its_runtime(self):
        # ABS leaves media.duration out of an item unless it's expanded.
        def fake_get(url, params=None, **kwargs):
            media = {"metadata": {"title": "Book"}}
            if (params or {}).get("expanded"):
                media["duration"] = 58252.2
            return _FakeResponse({"id": "item-1", "media": media})

        with mock.patch.object(A.req, "get", fake_get):
            book = A.get_abs_book(self.USER_ID, "item-1", {})
        self.assertEqual(970.9, book["duration_minutes"])

    def test_narrators_publisher_and_language_are_kept_for_edition_matching(self):
        book = A._item_to_book({"id": "item-1", "media": {"duration": 60, "metadata": {
            "title": "Book", "narratorName": "Ray Porter, Someone Else",
            "publisher": "Audible Studios", "language": "English",
        }}}, {})
        self.assertEqual(["Ray Porter", "Someone Else"], book["narrators"])
        self.assertEqual(("Audible Studios", "English"), (book["publisher"], book["language"]))

    def test_reads_the_storygraph_tag_off_an_abs_book(self):
        book_id = "A810EEFF-D332-4B69-A85B-9FB99D2C4936"
        book = A._item_to_book({"id": "item-1", "media": {
            "duration": 60, "metadata": {"title": "Book"}, "tags": ["Fantasy", f"StoryGraph: {book_id}"],
        }}, {})
        self.assertEqual(book_id.lower(), book["storygraph_tag"])
        self.assertIsNone(A._item_to_book(_item("item-2"), {})["storygraph_tag"])

    def test_writing_a_tag_replaces_the_old_one_and_keeps_the_rest(self):
        old_id, new_id = "a" * 36, "b" * 36
        item = {"media": {"tags": ["Fantasy", f"storygraph:{old_id}"]}}
        patches = []
        with mock.patch.object(A.req, "get", lambda url, **kwargs: _FakeResponse(item)), \
                mock.patch.object(A.req, "patch", lambda url, **kwargs: patches.append((url, kwargs["json"])) or _FakeResponse({})):
            self.assertTrue(A.write_storygraph_tag(self.USER_ID, "item-1", new_id))
            item["media"]["tags"] = ["Fantasy", f"storygraph:{new_id}"]
            self.assertFalse(A.write_storygraph_tag(self.USER_ID, "item-1", new_id))
        self.assertEqual(
            [("http://abs/api/items/item-1/media", {"tags": ["Fantasy", f"storygraph:{new_id}"]})], patches,
        )

    def test_a_missing_abs_item_is_none_rather_than_an_error(self):
        with mock.patch.object(A.req, "get", lambda url, **kwargs: _FakeResponse({}, 404)):
            self.assertIsNone(A.get_abs_book(self.USER_ID, "gone", {}))



def _filter_page(book_id, identifier, next_page=None):
    """A /filter-editions response holding one audio edition card."""
    card = (
        f'<div><a href=\\"\\/books\\/{book_id}\\">Book<\\/a><p>10h \\u2022 audio<\\/p>'
        f'<div>ISBN/UID: {identifier}<\\/div><div>Format: Audio<\\/div><\\/div>'
    )
    more = (
        f"$('#next_link').replaceWith('<a id=\\\"next_link\\\" href=\\\"/filter-editions?page={next_page}\\\">more<\\/a>');"
        if next_page else ""
    )
    return f"$('.panes').append(\"{card}\");{more}"


class _Resp:
    def __init__(self, text, status_code=200, url="https://app.thestorygraph.com/x"):
        self.text, self.status_code, self.url, self.cookies = text, status_code, url, {}

    def raise_for_status(self):
        if self.status_code != 200:
            raise A.req.HTTPError(self.status_code)


class StoryGraphEditionPagingTests(unittest.TestCase):
    WORK = "11111111-0000-0000-0000-000000000000"

    def client(self, filter_pages, fail=False):
        client = A.StoryGraphClient("session")
        search = f'<a class="book-title-link" href="/books/{self.WORK}">Book</a>'
        plain = (
            f'<div><a href="/books/{"22222222-0000-0000-0000-000000000000"}">Book</a>'
            "<p>300 pages • hardcover</p><div>ISBN/UID: 978</div><div>Format: Hardcover</div></div>"
        )
        client._get = lambda path: _Resp(search if path.startswith("/browse") else plain)
        self.filter_calls = []

        def filter_get(url, params=None, **kwargs):
            self.filter_calls.append(params)
            if fail:
                return _Resp("", 500)
            return _Resp(filter_pages[params["page"] - 1])
        client._session.get = filter_get
        return client

    def test_reads_audio_filter_pages_until_there_are_no_more(self):
        client = self.client([
            _filter_page("a" * 8 + "-0000-0000-0000-000000000000", "A1", next_page=2),
            _filter_page("b" * 8 + "-0000-0000-0000-000000000000", "B1"),
        ])
        editions = client.load_editions("Book Author", "english")
        self.assertEqual(["978", "A1", "B1"], [edition.identifier for edition in editions])
        self.assertEqual([1, 2], [params["page"] for params in self.filter_calls])
        self.assertEqual({"english"}, {params["languages[]"] for params in self.filter_calls})

    def test_stops_at_the_page_cap_and_skips_the_language_filter_when_unknown(self):
        pages = [
            _filter_page(f"{n:08d}-0000-0000-0000-000000000000", f"P{n}", next_page=n + 1)
            for n in range(1, 10)
        ]
        self.client(pages).load_editions("Book Author")
        self.assertEqual(A.MAX_AUDIO_EDITION_PAGES, len(self.filter_calls))
        self.assertNotIn("languages[]", self.filter_calls[0])

    def test_falls_back_to_the_plain_list_when_the_filter_fails(self):
        editions = self.client([], fail=True).load_editions("Book Author")
        self.assertEqual(["978"], [edition.identifier for edition in editions])


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
