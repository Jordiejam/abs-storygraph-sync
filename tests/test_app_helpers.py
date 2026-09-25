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


_UNLABELLED_PAGE = '<input type="number" name="read_status[progress_number]" />'


def _read_list(*read_ids):
    """The add-a-read page, listing each existing read with its edit link."""
    return "".join(
        f'<a href="/read_instances/{read_id}/edit?book_id=b1">Edit</a>'
        f'<a href="/remove-reread?book_id=b1&amp;read_instance_id={read_id}">Remove read</a>'
        for read_id in read_ids
    )


def _read_form(read_id, start=("", "", ""), finish=("", "", "")):
    """A read's edit form, shaped like StoryGraph's: a PATCH override and six
    date selects, each with a blank option and the chosen one selected."""
    def select(name, value):
        return (
            f'<select name="read_instance[{name}]"><option value="">-</option>'
            + (f'<option selected="selected" value="{value}">{value}</option>' if value else "")
            + "</select>"
        )
    fields = zip(("start_day", "start_month", "start_year", "day", "month", "year"), (*start, *finish))
    return (
        f'<form action="/read_instances/{read_id}" method="post">'
        '<input type="hidden" name="_method" value="patch" />'
        '<input type="hidden" name="authenticity_token" value="tok" />'
        + "".join(select(name, value) for name, value in fields)
        + f'<input type="hidden" name="read_instance_id" value="{read_id}" />'
        '<input type="submit" name="commit" value="Update" /></form>'
    )


class StoryGraphPageTests(unittest.TestCase):
    def setUp(self):
        self.client = A.StoryGraphClient("session")
        self.posts = []
        self.post_status = 200
        self.client._post = lambda path, data: self.posts.append(path) or _FakeResponse({}, self.post_status)
        # What StoryGraph serves after a status POST, and on the add-a-read page.
        self.pages = {"/books/b1": _book_page("read"), "/read_instances/new?book_id=b1": ""}
        self.client._get = lambda path: _Resp(self.pages[path])

    def test_a_matching_status_is_not_posted_again(self):
        ok, already, html = self.client.ensure_status("b1", "currently-reading", html=_book_page("currently reading", 89))

        self.assertEqual((True, True), (ok, already))
        self.assertEqual([], self.posts)
        self.assertEqual(89.0, A._parse_current_progress(html))

    def test_currently_reading_does_not_count_as_read(self):
        ok, already, html = self.client.ensure_status("b1", "read", html=_book_page("currently reading"))

        self.assertEqual((True, False), (ok, already))
        self.assertEqual(["/update-status.js?book_id=b1&status=read"], self.posts)
        self.assertEqual(self.pages["/books/b1"], html)

    def test_a_book_off_the_shelf_has_its_status_posted(self):
        ok, already, _ = self.client.ensure_status("b1", "read", html=_UNLABELLED_PAGE)

        self.assertEqual((True, False), (ok, already))
        self.assertEqual(["/update-status.js?book_id=b1&status=read"], self.posts)

    def test_a_status_the_page_does_not_show_after_posting_is_a_failure(self):
        self.pages["/books/b1"] = _book_page("to read")

        ok, _, _ = self.client.ensure_status("b1", "currently-reading", html=_book_page("to read"))

        self.assertFalse(ok)

    def test_a_book_still_off_the_shelf_after_posting_is_a_failure(self):
        # A read on file doesn't make it one: StoryGraph can hold a read of a
        # book that isn't on your shelf.
        self.pages["/books/b1"] = _UNLABELLED_PAGE
        self.pages["/read_instances/new?book_id=b1"] = _read_list("41")

        for status in ("read", "currently-reading"):
            ok, _, _ = self.client.ensure_status("b1", status, html=_book_page("to read"))
            self.assertFalse(ok, status)

    def test_a_rejected_post_is_a_failure_without_rereading_the_page(self):
        self.post_status = 422
        self.pages = {}

        self.assertEqual((False, False, None), self.client.ensure_status("b1", "read", html=_book_page("to read")))

    def test_being_signed_out_when_confirming_raises(self):
        self.client._get = lambda path: _Resp("", url="https://app.thestorygraph.com/users/sign_in")

        with self.assertRaises(A.StoryGraphAuthError):
            self.client.ensure_status("b1", "read", html=_book_page("to read"))

    def test_cleared_progress_reads_as_unknown(self):
        self.assertIsNone(A._parse_current_progress(_book_page("currently reading")))


class StoryGraphReadDateTests(unittest.TestCase):
    EDIT = "/read_instances/42/edit?book_id=b1"

    def setUp(self):
        self.client = A.StoryGraphClient("session")
        self.pages = {"/read_instances/new?book_id=b1": _read_list("42"), self.EDIT: _read_form("42")}
        self.client._get = lambda path: _Resp(self.pages[path])
        self.patches = []

        def post(url, data=None, **kwargs):
            self.patches.append((url, dict(data)))
            # Echo the submitted dates back, as StoryGraph's edit form does.
            dates = [data[f"read_instance[{name}]"] for name in ("start_day", "start_month", "start_year", "day", "month", "year")]
            self.pages[self.EDIT] = _read_form("42", tuple(dates[:3]), tuple(dates[3:]))
            return _Resp("", 200)
        self.client._session.post = post

    def test_lists_each_read_once_in_page_order(self):
        self.pages["/read_instances/new?book_id=b1"] = _read_list("7", "42") + _read_list("7")

        self.assertEqual(["7", "42"], self.client.read_ids("b1"))

    def test_dates_a_read_through_its_edit_form(self):
        ok = self.client.set_read_dates("b1", "42", A.date_cls(2026, 9, 5), A.date_cls(2026, 9, 15))

        self.assertTrue(ok)
        url, data = self.patches[0]
        self.assertEqual("https://app.thestorygraph.com/read_instances/42", url)
        self.assertEqual("patch", data["_method"])
        self.assertEqual(
            ("5", "9", "2026", "15", "9", "2026"),
            tuple(data[f"read_instance[{name}]"] for name in ("start_day", "start_month", "start_year", "day", "month", "year")),
        )
        self.assertNotIn("commit", data)

    def test_keeps_a_start_it_is_not_given_unless_it_is_after_the_finish(self):
        self.pages[self.EDIT] = _read_form("42", ("20", "9", "2026"), ("25", "9", "2026"))

        self.client.set_read_dates("b1", "42", None, A.date_cls(2026, 9, 22))
        self.assertEqual("20", self.patches[-1][1]["read_instance[start_day]"])

        self.client.set_read_dates("b1", "42", None, A.date_cls(2026, 9, 15))
        self.assertEqual("15", self.patches[-1][1]["read_instance[start_day]"])

    def test_a_date_the_form_does_not_show_afterwards_is_a_failure(self):
        self.client._session.post = lambda url, data=None, **kwargs: _Resp("", 200)

        self.assertFalse(self.client.set_read_dates("b1", "42", A.date_cls(2026, 9, 5), A.date_cls(2026, 9, 15)))

    def test_marking_read_dates_only_the_read_it_adds(self):
        self.pages["/books/b1"] = _book_page("currently reading")
        self.pages["/read_instances/new?book_id=b1"] = _read_list("7")
        self.pages["/read_instances/7/edit?book_id=b1"] = _read_form("7", ("1", "8", "2026"), ("10", "8", "2026"))
        dated = []
        self.client.set_read_dates = lambda book_id, read_id, started, finished: dated.append(read_id) or True

        def post_status(path, data):
            self.pages["/books/b1"] = _book_page("read")
            self.pages["/read_instances/new?book_id=b1"] = _read_list("7", "42")
            return _FakeResponse({})
        self.client._post = post_status

        ok, already, _ = self.client.mark_read("b1", A.date_cls(2026, 9, 5), A.date_cls(2026, 9, 15))

        self.assertEqual((True, False), (ok, already))
        self.assertEqual(["42"], dated)

    def test_a_reread_never_starts_before_the_last_read_finished(self):
        # ABS keeps a relistened book's first startedAt.
        self.pages["/books/b1"] = _book_page("rereading")
        self.pages["/read_instances/new?book_id=b1"] = _read_list("7")
        self.pages["/read_instances/7/edit?book_id=b1"] = _read_form("7", ("1", "9", "2026"), ("10", "9", "2026"))
        dated = []
        self.client.set_read_dates = lambda book_id, read_id, started, finished: dated.append((started, finished)) or True

        def post_status(path, data):
            self.pages["/books/b1"] = _book_page("read")
            self.pages["/read_instances/new?book_id=b1"] = _read_list("42", "7")
            return _FakeResponse({})
        self.client._post = post_status

        self.client.mark_read("b1", A.date_cls(2026, 8, 1), A.date_cls(2026, 9, 20))
        self.assertEqual([(A.date_cls(2026, 9, 10), A.date_cls(2026, 9, 20))], dated)

    def test_a_book_already_read_is_neither_posted_nor_redated(self):
        self.pages["/books/b1"] = _book_page("read")
        self.client._post = lambda path, data: self.fail("posted a status")
        self.client.set_read_dates = lambda *args: self.fail("redated a read")

        self.assertEqual((True, True), self.client.mark_read("b1", A.date_cls(2026, 9, 5), A.date_cls(2026, 9, 15))[:2])


def _journal(*entries):
    """A journal page: (entry id, text) pairs, each its own entry block."""
    return "".join(
        f'<div class="entry"><p>No date <a href="/journal_entries/{entry_id}/edit">Edit</a></p>'
        f"<span>{text}</span></div>"
        for entry_id, text in entries
    )


START_A = "aaaaaaaa-0000-0000-0000-00000000000a"
START_B = "bbbbbbbb-0000-0000-0000-00000000000b"


class StoryGraphStartDateTests(unittest.TestCase):
    EDIT = f"/journal_entries/{START_B}/edit"

    def setUp(self):
        self.client = A.StoryGraphClient("session")
        # A reread: an old read's start is already in the journal.
        self.pages = {
            "/books/b1": _book_page("read"),
            "/journal?book_id=b1&page=1": _journal((START_A, "Started reading"), ("c" * 8 + "-0000-0000-0000-00000000000c", "50%")),
        }
        self.client._get = lambda path: _Resp(self.pages.get(path, ""))
        self.dated = []
        self.client.set_journal_entry_date = lambda entry_id, value: self.dated.append((entry_id, value)) or True

        def post_status(path, data):
            self.pages["/books/b1"] = _book_page("rereading")
            self.pages["/journal?book_id=b1&page=1"] += _journal((START_B, "Started reading"))
            return _FakeResponse({})
        self.client._post = post_status

    def test_starting_dates_only_the_start_it_adds(self):
        ok, already, _ = self.client.start_reading("b1", A.date_cls(2026, 9, 1))

        self.assertEqual((True, False), (ok, already))
        self.assertEqual([(START_B, A.date_cls(2026, 9, 1))], self.dated)

    def test_a_reread_start_is_moved_up_to_the_last_finish(self):
        self.pages["/read_instances/new?book_id=b1"] = _read_list("7")
        self.pages["/read_instances/7/edit?book_id=b1"] = _read_form("7", ("1", "9", "2026"), ("10", "9", "2026"))

        self.client.start_reading("b1", A.date_cls(2026, 8, 1))
        self.assertEqual([(START_B, A.date_cls(2026, 9, 10))], self.dated)

    def test_a_book_already_being_read_is_neither_posted_nor_redated(self):
        self.pages["/books/b1"] = _book_page("currently reading")
        self.client._post = lambda path, data: self.fail("posted a status")

        self.assertEqual((True, True), self.client.start_reading("b1", A.date_cls(2026, 9, 1))[:2])
        self.assertEqual([], self.dated)

    def test_pages_through_the_journal_until_a_page_adds_nothing(self):
        self.pages["/journal?book_id=b1&page=2"] = _journal((START_B, "Started reading"))
        self.pages["/journal?book_id=b1&page=3"] = _journal((START_B, "Started reading"))

        self.assertEqual({START_A, START_B}, self.client.started_entry_ids("b1"))

    def test_moves_a_journal_entry_through_its_edit_form(self):
        client = A.StoryGraphClient("session")
        form = (
            f'<form action="/journal_entries/{START_B}" method="post">'
            '<input type="hidden" name="_method" value="put" />'
            '<input type="hidden" name="authenticity_token" value="tok" />'
            '<select name="journal_entry[day]"><option selected="selected" value="25">25</option></select>'
            '<select name="journal_entry[month]"><option selected="selected" value="9">9</option></select>'
            '<select name="journal_entry[year]"><option selected="selected" value="2026">2026</option></select>'
            '<input type="number" name="journal_entry[percent_reached]" value="0" />'
            '<input type="hidden" name="journal_entry[note]" />'
            "</form>"
        )
        pages = {self.EDIT: form}
        client._get = lambda path: _Resp(pages[path])
        posts = []

        def post(url, data=None, **kwargs):
            posts.append((url, dict(data)))
            pages[self.EDIT] = form.replace('value="25">25', 'value="1">1')
            return _Resp("", 302)
        client._session.post = post

        self.assertTrue(client.set_journal_entry_date(START_B, A.date_cls(2026, 9, 1)))
        url, data = posts[0]
        self.assertEqual(f"https://app.thestorygraph.com/journal_entries/{START_B}", url)
        self.assertEqual(("put", "1", "9", "2026", "0"), (
            data["_method"], data["journal_entry[day]"], data["journal_entry[month]"],
            data["journal_entry[year]"], data["journal_entry[percent_reached]"],
        ))
