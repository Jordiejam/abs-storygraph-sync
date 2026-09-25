"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, jsonify, request, render_template, session, redirect, url_for, g
from urllib.parse import urlparse
from functools import wraps
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import date as date_cls, datetime, timezone, time as datetime_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import requests as req
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from matcher import (
    AudiobookDetails, EditionCandidate, edition_checks, match_audio_edition, merge_editions,
    normalise_language, parse_filtered_editions, parse_storygraph_editions,
)
from history import build_history_preview, day_key
from journal import parse_journal_page, progress_dates

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
USERS_FILE = f"{DATA_DIR}/users.json"
CONFIG_DIR = f"{DATA_DIR}/config"
SYNC_STATE_DIR = f"{DATA_DIR}/sync_state"
IMPORT_STATE_DIR = f"{DATA_DIR}/import_state"
SCHEDULER_STATE_DIR = f"{DATA_DIR}/scheduler_state"
EDITIONS_DIR = f"{DATA_DIR}/editions"

STORYGRAPH_BASE = "https://app.thestorygraph.com"
# Pages of StoryGraph's audio-only edition filter read per lookup. Each is ten
# editions and about 1.5 MB; the most-shelved releases come first, and later
# pages of a popular book are mostly user-added duplicates.
MAX_AUDIO_EDITION_PAGES = 3
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 600))
SYNC_THRESHOLD = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() in {"1", "true", "yes", "on"}

OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

# Only used as a fallback when the reverse proxy's forwarded headers (picked up
# automatically via ProxyFix below) aren't enough to get the right scheme/host —
# e.g. an unusual proxy chain. Set to the externally-visible base URL, no
# trailing slash: PUBLIC_URL=https://abs-sync.example.com
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")

SYNC_SCOPES = ("in_progress", "in_progress_finished", "library")
SYNC_MODES = ("frequent", "daily")
DEFAULT_SYNC_SCOPE = "in_progress"
DEFAULT_SYNC_MODE = "frequent"
DEFAULT_DAILY_SYNC_TIME = "00:00"
DEFAULT_TIMEZONE = "UTC"


def _write_json_atomic(path: str, data):
    """Write to a temp file and swap it in, so a crash mid-write can't leave a
    truncated file that the loaders would silently read as empty."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def _get_secret_key() -> bytes:
    key_file = f"{DATA_DIR}/secret_key"
    try:
        with open(key_file, "rb") as f:
            key = f.read()
            if len(key) >= 32:
                return key
    except FileNotFoundError:
        pass
    key = os.urandom(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(key)
    return key

# ── Logging ───────────────────────────────────────────────────────────────────

class LogBuffer(logging.Handler):
    def __init__(self, maxlen=300):
        super().__init__()
        self._records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self._records.append({
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            })

    def get(self):
        with self._lock:
            return list(self._records)


_log_buffer = LogBuffer()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger().addHandler(_log_buffer)
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # suppress per-request access logs

# ── User store ───────────────────────────────────────────────────────────────

_users_lock = threading.Lock()


def _load_users() -> list[dict]:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_users(users: list[dict]):
    _write_json_atomic(USERS_FILE, users)


def any_users_exist() -> bool:
    with _users_lock:
        return bool(_load_users())


def list_users() -> list[dict]:
    with _users_lock:
        return _load_users()


def get_user(user_id: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u["id"] == user_id), None)


def get_user_by_username(username: str) -> dict | None:
    username = (username or "").lower()
    with _users_lock:
        return next((u for u in _load_users() if (u.get("username") or "").lower() == username), None)


def get_user_by_oidc_sub(sub: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u.get("oidc_sub") == sub), None)


def create_user(*, username=None, password=None, oidc_sub=None, display_name=None, is_admin=False) -> dict:
    with _users_lock:
        users = _load_users()
        user = {
            "id": uuid.uuid4().hex,
            "username": username,
            "password_hash": generate_password_hash(password) if password else None,
            "oidc_sub": oidc_sub,
            "display_name": display_name or username or "User",
            "is_admin": is_admin,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        users.append(user)
        _save_users(users)
        return user


def update_user(user_id: str, **fields):
    with _users_lock:
        users = _load_users()
        for u in users:
            if u["id"] == user_id:
                u.update(fields)
        _save_users(users)


def delete_user(user_id: str):
    with _users_lock:
        users = [u for u in _load_users() if u["id"] != user_id]
        _save_users(users)


def _user_label(user: dict) -> str:
    return user.get("username") or user.get("display_name") or user["id"]

# ── Per-user JSON stores ─────────────────────────────────────────────────────
# Config, sync state and History Import state all want the same thing: one
# {user_id}.json under a directory, read once and cached in memory, rewritten
# whole on save.


class _UserJsonStore:
    def __init__(self, directory: str):
        self._dir = directory
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}

    def _path(self, user_id: str) -> str:
        return f"{self._dir}/{user_id}.json"

    def get(self, user_id: str) -> dict:
        """This user's live, mutable dict — edit it in place, then save()."""
        with self._lock:
            if user_id not in self._cache:
                try:
                    with open(self._path(user_id)) as f:
                        self._cache[user_id] = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    self._cache[user_id] = {}
            return self._cache[user_id]

    def save(self, user_id: str):
        with self._lock:
            _write_json_atomic(self._path(user_id), self._cache.get(user_id, {}))


_config_store = _UserJsonStore(CONFIG_DIR)

# The last {progress %, StoryGraph status} successfully pushed per book.
_sync_store = _UserJsonStore(SYNC_STATE_DIR)

# Per book (keyed by abs_item_id): {"imported_days": {day_key -> {percent,
# imported_at}}}. Saved immediately after each verified dated write, so a rerun
# can never double-import a day and a failure partway through still leaves
# correct partial state on disk.
_import_store = _UserJsonStore(IMPORT_STATE_DIR)

# {"books": {abs_item_id -> edition entry}}: which StoryGraph edition each ABS
# book belongs to. See _editions().
_edition_store = _UserJsonStore(EDITIONS_DIR)

# Lifecycle transitions handled by auto-sync and the date of the most recent
# daily run. Kept separate from _sync_store: that store only describes writes
# that successfully reached StoryGraph.
_scheduler_store = _UserJsonStore(SCHEDULER_STATE_DIR)

_user_locks: dict[str, threading.RLock] = {}
_user_locks_guard = threading.Lock()


def _user_lock(user_id: str) -> threading.RLock:
    """The stores hand out live dicts, so the poller and a request thread must
    not edit (or save) the same user's state at once. Held around anything
    that mutates it."""
    with _user_locks_guard:
        return _user_locks.setdefault(user_id, threading.RLock())


def cfg(user_id: str, key: str, default: str = "") -> str:
    return _config_store.get(user_id).get(key) or default


ABS_KEYS = ("ABS_URL", "ABS_TOKEN")
SYNC_KEYS = (*ABS_KEYS, "STORYGRAPH_SESSION")


def _missing_cfg(user_id: str, keys) -> list[str]:
    return [k for k in keys if not cfg(user_id, k)]


def set_cfg(user_id: str, updates: dict):
    _config_store.get(user_id).update({k: v for k, v in updates.items() if v})
    _config_store.save(user_id)


# ── Editions ─────────────────────────────────────────────────────────────────
# An edition entry is {"state", "edition", "candidates", "checked_at"}:
#   suggested  a lookup found a confident match (or sync used one before
#              confirmation existed) — nothing is written to it yet
#   unmatched  a lookup found nothing confident; "candidates" holds the options
#   confirmed  a person chose or approved "edition" — the only state sync and
#              History Import will ever write to
# A book with no entry has never been looked up. Lookups only ever happen when
# a person asks for one; nothing here searches StoryGraph on its own.


def _editions(user_id: str) -> dict[str, dict]:
    """This user's live {abs_item_id: entry} map. The first call migrates the
    editions that used to live elsewhere: History Import's saved edition (a
    manual pick counts as confirmed, an auto-match only as a suggestion) and
    the ids regular sync was already writing to, which nobody ever confirmed."""
    store = _edition_store.get(user_id)
    if "books" in store:
        return store["books"]
    with _user_lock(user_id):
        if "books" in store:
            return store["books"]
        books = {}
        for item_id, synced in _sync_store.get(user_id).items():
            book_id = (synced or {}).get("storygraph_book_id")
            # Legacy title-keyed state can't be tied to an ABS item.
            if book_id and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", item_id):
                books[item_id] = {
                    "state": "suggested",
                    "edition": {"storygraph_book_id": book_id, "title": None, "format": None,
                                "duration_minutes": None, "identifier": None},
                    "candidates": [],
                }
        import_state = _import_store.get(user_id)
        for item_id, item_state in import_state.items():
            edition = (item_state or {}).pop("edition", None)
            if edition:
                source = edition.pop("source", "auto")
                books[item_id] = {
                    "state": "confirmed" if source == "manual" else "suggested",
                    "edition": edition,
                    "candidates": [],
                }
        store["books"] = books
        _edition_store.save(user_id)
        _import_store.save(user_id)
        return books


def _edition_entry(user_id: str, item_id: str | None) -> dict:
    return (_editions(user_id).get(item_id) or {}) if item_id else {}


def _confirmed_edition_id(user_id: str, item_id: str | None) -> str | None:
    """The StoryGraph book id a person confirmed for this ABS item, if any."""
    entry = _edition_entry(user_id, item_id)
    if entry.get("state") != "confirmed":
        return None
    return (entry.get("edition") or {}).get("storygraph_book_id")

# ── ABS ───────────────────────────────────────────────────────────────────────

def _abs_get(user_id: str, path: str, params: dict | None = None, *, required: bool = True):
    """GET from this user's ABS. `required=False` returns None on a non-200
    instead of raising, for lookups where a missing record is normal."""
    resp = req.get(
        f"{cfg(user_id, 'ABS_URL')}{path}",
        headers={"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"},
        params=params,
        timeout=10,
    )
    if not required and resp.status_code != 200:
        return None
    resp.raise_for_status()
    return resp


# ABS has no custom fields, so a book's confirmed StoryGraph edition is kept on
# it as a tag. See _storygraph_tag_id and write_storygraph_tag.
STORYGRAPH_TAG_PREFIX = "storygraph:"
_STORYGRAPH_TAG_RE = re.compile(rf"{STORYGRAPH_TAG_PREFIX}\s*([0-9a-fA-F-]{{36}})", re.IGNORECASE)


def _storygraph_tag_id(tags) -> str | None:
    """The StoryGraph book id from the first storygraph:<id> tag, if any."""
    for tag in tags or []:
        m = _STORYGRAPH_TAG_RE.fullmatch(str(tag).strip())
        if m:
            return m.group(1).lower()
    return None


def write_storygraph_tag(user_id: str, item_id: str, storygraph_book_id: str) -> bool:
    """Tag the ABS book with its StoryGraph edition, replacing any older
    storygraph: tag and keeping every other tag. Returns False when the tag
    was already there. Needs an ABS user allowed to update books."""
    tags = (_abs_get(user_id, f"/api/items/{item_id}").json().get("media") or {}).get("tags") or []
    wanted = f"{STORYGRAPH_TAG_PREFIX}{storygraph_book_id}"
    if wanted in tags:
        return False
    kept = [tag for tag in tags if not _storygraph_tag_id([tag])]
    resp = req.patch(
        f"{cfg(user_id, 'ABS_URL')}/api/items/{item_id}/media",
        headers={"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"},
        json={"tags": [*kept, wanted]},
        timeout=10,
    )
    resp.raise_for_status()
    return True


def _item_to_book(item: dict, progress: dict) -> dict | None:
    media = item.get("media", {})
    metadata = media.get("metadata", {})
    title = metadata.get("title", "").strip()
    if not title:
        return None
    identifiers = [
        str(metadata[key]).strip()
        for key in ("isbn", "asin")
        if metadata.get(key) and str(metadata[key]).strip()
    ]
    book = {
        "abs_item_id": item.get("id", ""),
        "title": title,
        "author": metadata.get("authorName", ""),
        "identifiers": identifiers,
        # For telling apart editions that share a runtime; see matcher.
        "narrators": [name.strip() for name in (metadata.get("narratorName") or "").split(",") if name.strip()],
        "publisher": metadata.get("publisher") or None,
        "language": metadata.get("language") or None,
        "storygraph_tag": _storygraph_tag_id(media.get("tags")),
        "progress_percent": round((progress.get("progress") or 0) * 100, 1),
        "current_minutes": round((progress.get("currentTime") or 0) / 60, 1),
        "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        "is_finished": bool(progress.get("isFinished")),
    }
    book["state_key"] = _book_state_key(book)
    return book


def get_abs_progress(user_id: str) -> dict[str, dict]:
    """One lightweight snapshot of every progress record for this ABS user."""
    me = _abs_get(user_id, "/api/me").json()
    return {
        progress["libraryItemId"]: progress
        for progress in me.get("mediaProgress", [])
        if progress.get("libraryItemId")
    }


def get_abs_book(user_id: str, item_id: str, progress: dict) -> dict | None:
    # Only the expanded item carries media.duration; without it every book
    # fetched this way would have a zero runtime.
    resp = _abs_get(user_id, f"/api/items/{item_id}", {"expanded": 1}, required=False)
    return _item_to_book(resp.json(), progress) if resp is not None else None


def get_abs_books(
    user_id: str,
    scope: str,
    progress_by_item: dict[str, dict] | None = None,
) -> list[dict]:
    if progress_by_item is None:
        progress_by_item = get_abs_progress(user_id)

    if scope == "in_progress":
        items = _abs_get(user_id, "/api/me/items-in-progress").json().get("libraryItems", [])
        books = [_item_to_book(item, progress_by_item.get(item.get("id"), {})) for item in items]
        return [book for book in books if book]

    if scope == "in_progress_finished":
        books = []
        for item_id, progress in progress_by_item.items():
            if not ((progress.get("currentTime") or 0) > 0 or progress.get("isFinished")):
                continue
            book = get_abs_book(user_id, item_id, progress)
            if book:
                books.append(book)
        return books

    # scope == "library": every book, including untouched ones
    libraries = _abs_get(user_id, "/api/libraries").json().get("libraries", [])
    books = []
    for lib in libraries:
        page = 0
        while True:
            resp = _abs_get(user_id, f"/api/libraries/{lib['id']}/items", params={"limit": 100, "page": page})
            data = resp.json()
            results = data.get("results", [])
            total = data.get("total", len(results))
            for item in results:
                progress = progress_by_item.get(item.get("id"), {})
                book = _item_to_book(item, progress)
                if book:
                    books.append(book)
            page += 1
            if not results or page * 100 >= total:
                break
    return books


def get_abs_listening_sessions(user_id: str, item_id: str) -> list[dict]:
    sessions = []
    page = 0
    while True:
        data = _abs_get(
            user_id,
            f"/api/me/item/listening-sessions/{item_id}",
            params={"page": page, "itemsPerPage": 100},
        ).json()
        sessions.extend(data.get("sessions", []))
        num_pages = int(data.get("numPages") or 0)
        page += 1
        if page >= num_pages:
            break
    return sessions

# ── StoryGraph ────────────────────────────────────────────────────────────────

# StoryGraph has no public API — these status slugs are reverse-engineered from
# the site's own /update-status.js requests and may change without notice.
_STATUS_LABELS = {
    "to-read": "to read",
    "currently-reading": "currently reading",
    "read": "read",
}


def _audiobook_details(book: dict) -> AudiobookDetails:
    return AudiobookDetails(
        narrators=tuple(book.get("narrators") or ()),
        publisher=book.get("publisher"),
        language=book.get("language"),
    )


def _match_audio_edition(candidates, title, duration_minutes=0, identifiers=None, details=None, tagged_id=None):
    """The single confident audio-edition match from an already-loaded
    candidate list (full candidate, for display) or None, with the reason from
    matcher.match_audio_edition."""
    matched, reason = match_audio_edition(
        candidates,
        target_duration_minutes=duration_minutes,
        identifiers=identifiers or [],
        details=details,
        tagged_id=tagged_id,
    )
    if matched:
        logger.info(
            "Matched '%s' to audio edition id=%s by %s (runtime %.1f min vs ABS %.1f min)",
            title, matched.book_id, reason["code"], matched.duration_minutes or 0, duration_minutes,
        )
    else:
        logger.warning(
            "No confident audio edition match for '%s': %s (ABS runtime %.1f min, %d candidates)",
            title, reason["code"], duration_minutes, len(candidates),
        )
    return matched, reason


def _input_value(html: str, name: str) -> str | None:
    """The value of the named <input>, whatever order its attributes come in."""
    for tag in re.findall(r"<input\b[^>]*>", html):
        if f'name="{name}"' in tag:
            m = re.search(r'\bvalue="([^"]*)"', tag)
            return m.group(1) if m else None
    return None


def _parse_read_status(html: str) -> str:
    """The status label on a book page ("currently reading", "read", ...), or
    "" if it can't be found. The label carries a long list of utility classes,
    so match read-status-label as one class token among them."""
    m = re.search(r'class="(?:[^"]*\s)?read-status-label(?:\s[^"]*)?"[^>]*>([^<]+)<', html)
    return " ".join(m.group(1).split()).lower() if m else ""


def _parse_current_progress(html) -> float | None:
    """The percentage StoryGraph has on file for this book, or None if it
    can't be read — treat that as unknown, never as a reason to skip a write."""
    value = _input_value(html, "read_status[progress_number]")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class StoryGraphAuthError(Exception):
    """StoryGraph bounced a request to its sign-in page."""


class StoryGraphClient:
    def __init__(self, session_cookie: str, remember_token: str = ""):
        self._session = req.Session()
        for name, val in [
            ("_storygraph_session", session_cookie),
            ("remember_user_token", remember_token),
            ("cookies_popup_seen", "yes"),
            ("plus_popup_seen", "yes"),
        ]:
            self._session.cookies.set(name, val, domain="app.thestorygraph.com")
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept-Language": "en",
            "Origin": STORYGRAPH_BASE,
        })
        self._last_csrf = None
        self._authed = False

    def _extract_csrf(self, html):
        m = (
            re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', html)
            or re.search(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        )
        if m:
            self._last_csrf = m.group(1)
        return self._last_csrf

    def _get(self, path):
        resp = self._session.get(f"{STORYGRAPH_BASE}{path}", timeout=15)
        self._extract_csrf(resp.text)
        if "_storygraph_session" in resp.cookies:
            self._session.cookies.set("_storygraph_session", resp.cookies["_storygraph_session"], domain="app.thestorygraph.com")
        return resp

    def _post(self, path, data):
        return self._session.post(
            f"{STORYGRAPH_BASE}{path}", data=data,
            headers={
                "X-CSRF-Token": self._last_csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, */*; q=0.01",
                "Referer": STORYGRAPH_BASE,
            },
            allow_redirects=False, timeout=15,
        )

    def check_auth(self) -> bool:
        # One client serves every sync in a poll, so a valid session only
        # needs confirming once.
        if not self._authed:
            self._authed = "sign_in" not in self._get("/").url
        return self._authed

    def _find_initial_book_id(self, query) -> str | None:
        """The top search result's book id, or None (logged) if there isn't one."""
        resp = self._get(f"/browse?search_term={req.utils.quote(query)}")
        if "sign_in" in resp.url:
            raise StoryGraphAuthError("StoryGraph session invalid — update it in Settings")
        m = None
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            link = soup.find("a", class_="book-title-link")
            if not link:
                container = soup.find(class_="book-title-author-and-series")
                if container:
                    link = container.find("a", href=re.compile(r"^/books/"))
            if link:
                m = re.search(r"/books/([^/?]+)", link.get("href", ""))
        if not m:
            logger.warning("No StoryGraph result for '%s'", query)
            return None
        return m.group(1)

    def load_editions(self, query, language: str | None = None) -> list:
        """The editions of the top search result for `query`: the first page of
        every format, plus up to MAX_AUDIO_EDITION_PAGES of audio editions
        only, in `language` when that's known. Pass the result to
        _match_audio_edition().

        The editions list is paginated and mostly print, so its first page
        alone can miss audio editions entirely. It's still read, because it
        carries the edition you've read and the page's current edition, which
        the filter leaves out."""
        initial_id = self._find_initial_book_id(query)
        if not initial_id:
            return []
        editions_resp = self._get(f"/books/{initial_id}/editions")
        if editions_resp.status_code != 200:
            logger.warning("Could not load StoryGraph editions for '%s'", query)
            return []
        pages = [parse_storygraph_editions(editions_resp.text)]
        try:
            pages.extend(self._audio_editions(initial_id, language))
        except (req.RequestException, ValueError) as exc:
            # The plain list still gives a (poorer) answer.
            logger.warning("StoryGraph audio edition filter failed for '%s': %s", query, exc)
        return merge_editions(*pages)

    def _audio_editions(self, book_id: str, language: str | None) -> list[list]:
        """Pages of the editions page's own filter, with only audio ticked.
        It's an XHR endpoint that answers with jQuery; see
        matcher.parse_filtered_editions."""
        params = {"book_id": book_id, "format_audio": "true", "commit": "Filter"}
        if language:
            params["languages[]"] = language
        pages, page = [], 1
        while page and page <= MAX_AUDIO_EDITION_PAGES:
            resp = self._session.get(
                f"{STORYGRAPH_BASE}/filter-editions",
                params={**params, "page": page},
                headers={
                    "Accept": "text/javascript, application/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{STORYGRAPH_BASE}/books/{book_id}/editions",
                },
                timeout=30,
            )
            resp.raise_for_status()
            editions, next_page = parse_filtered_editions(resp.text)
            pages.append(editions)
            page = next_page if next_page and next_page > page else None
        return pages

    def get_book_page(self, book_id) -> str:
        return self._get(f"/books/{book_id}").text

    def ensure_status(self, book_id, target_status, html: str | None = None) -> tuple[bool, bool, str | None]:
        """Returns (ok, already_matched, html) — already_matched is True when the book's
        StoryGraph status already matched target_status and nothing was posted. html is
        the page markup the match was read from (reusable for a progress check), or None
        if a status-changing POST was made and any previously-fetched markup is now stale."""
        html = html if html is not None else self.get_book_page(book_id)
        current = _parse_read_status(html)
        # Exact match: "read" is a substring of "currently reading". Re-posting
        # the status a book already has is not a no-op on StoryGraph — it appears
        # to wipe the book's current progress, which the daily run never rewrites.
        if current == _STATUS_LABELS[target_status] or (
            target_status == "currently-reading" and "rereading" in current
        ):
            return True, True, html
        r = self._post(
            f"/update-status.js?book_id={book_id}&status={target_status}",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set status=%s for %s: HTTP %s", target_status, book_id, r.status_code)
        return r.status_code in (200, 302), False, None

    def update_progress(self, book_id, progress_percent, html: str | None = None) -> bool:
        html = html if html is not None else self.get_book_page(book_id)
        book_pages = _input_value(html, "read_status[book_num_of_pages]") or "0"
        r = self._post("/update-progress", {
            "read_status[progress_number]": str(round(progress_percent, 1)),
            "read_status[progress_type]": "percentage",
            "read_status[book_num_of_pages]": book_pages,
            "book_id": book_id,
            "on_book_page": "true",
            "authenticity_token": self._last_csrf,
        })
        ok = r.status_code in (200, 302)
        logger.info("Progress for %s -> %.1f%%: HTTP %s", book_id, progress_percent, r.status_code)
        return ok

    def get_logged_progress_dates(self, book_id) -> set[str]:
        """The dates StoryGraph already has a *progress* entry on for this book
        (status-only entries excluded, see journal.progress_dates).

        The journal renders ~20 entries per page, so this pages until one adds
        nothing new, capped since StoryGraph documents no limit."""
        seen_ids: set[str] = set()
        dates: set[str] = set()
        page = 1
        while page <= 50:
            resp = self._get(f"/journal?book_id={book_id}&page={page}")
            if resp.status_code != 200:
                break
            new_entries = [e for e in parse_journal_page(resp.text) if e.entry_id not in seen_ids]
            if not new_entries:
                break
            seen_ids.update(e.entry_id for e in new_entries)
            dates |= progress_dates(new_entries)
            page += 1
        return dates

    def add_dated_progress_entry(self, book_id, date: str, percent: float) -> bool:
        """Writes one backdated journal entry via StoryGraph's 'Add note/Edit
        date' progress form.

        Posts a percentage so StoryGraph derives the position from its own
        edition length, which tolerates a runtime mismatch with the ABS
        audiobook. The form page is re-fetched first for a fresh CSRF token and
        the baseline fields it expects."""
        page_html = self._get(f"/progress-update?book_id={book_id}").text

        def _hidden(name: str) -> str:
            m = re.search(rf'(?:name|id)="{re.escape(name)}"[^>]*value="([^"]*)"', page_html)
            return m.group(1) if m else ""

        year, month, day = date.split("-")
        r = self._post("/update-progress-with-note", {
            "progress_update_date[day]": str(int(day)),
            "progress_update_date[month]": str(int(month)),
            "progress_update_date[year]": str(int(year)),
            "progress_minutes": "",
            "progress_type": "percentage",
            "progress_number": str(round(percent, 1)),
            "last_reached_pages": _hidden("last_reached_pages"),
            "book_num_of_pages": _hidden("book_num_of_pages") or "0",
            "last_reached_percent": _hidden("last_reached_percent"),
            "note": "",
            "href": "",
            "book_id": book_id,
            "return_to": "",
            "authenticity_token": self._last_csrf,
        })
        ok = r.status_code in (200, 302)
        logger.info(
            "Dated progress entry for %s on %s -> %.1f%%: HTTP %s", book_id, date, percent, r.status_code
        )
        return ok


def _storygraph_client(user_id: str) -> StoryGraphClient:
    return StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))

# ── Sync logic ────────────────────────────────────────────────────────────────

_status_cache: dict[str, dict] = {}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # seconds


def _cache_books(user_id: str, scope: str, books: list[dict]):
    with _status_cache_lock:
        _status_cache[user_id] = {"scope": scope, "books": books, "ts": time.time()}


def _fresh_cached_books(user_id: str, scope: str) -> list[dict] | None:
    """The cached book list, or None if it's stale or for another scope."""
    with _status_cache_lock:
        cache = _status_cache.get(user_id)
    if cache and cache["scope"] == scope and time.time() - cache["ts"] < STATUS_CACHE_TTL:
        return cache["books"]
    return None


def get_cached_books(user_id: str, scope: str) -> tuple[list[dict], bool]:
    books = _fresh_cached_books(user_id, scope)
    if books is not None:
        return books, True
    try:
        books = get_abs_books(user_id, scope)
    except Exception:
        with _status_cache_lock:
            cache = _status_cache.get(user_id) or {}
        return (cache["books"] if cache.get("scope") == scope else []), False
    _cache_books(user_id, scope, books)
    return books, True


def _target_status(book: dict) -> str:
    if book.get("is_finished"):
        return "read"
    if book["progress_percent"] > 0:
        return "currently-reading"
    return "to-read"


def _book_state_key(book: dict) -> str:
    return book.get("abs_item_id") or f"{book['title']}|{book.get('author', '')}"


def _previous_sync(synced: dict, book: dict) -> dict | None:
    """This book's last successful sync, including legacy title-keyed state."""
    return synced.get(_book_state_key(book)) or synced.get(book["title"])


def _last_synced_minutes(user_id: str, book: dict) -> float | None:
    """Durable last position, with a migration fallback for older state files."""
    previous = _previous_sync(_sync_store.get(user_id), book)
    if not previous:
        return None
    if previous.get("current_minutes") is not None:
        return float(previous["current_minutes"])
    if previous.get("pct") is not None and book.get("duration_minutes"):
        return round(book["duration_minutes"] * float(previous["pct"]) / 100, 1)
    return None


def _sync_result(book: dict, status: str) -> dict:
    return {
        "title": book["title"],
        "status": status,
        "progress_percent": book["progress_percent"],
        "current_minutes": book["current_minutes"],
    }


def do_sync(
    user_id: str,
    books: list[dict],
    *,
    label: str,
    start_before_finish: set[str] | None = None,
    write_progress: bool = True,
    client: StoryGraphClient | None = None,
) -> list[dict]:
    client = client or _storygraph_client(user_id)
    if not client.check_auth():
        logger.error("[%s] StoryGraph session invalid — update STORYGRAPH_SESSION", label)
        return [{"title": b["title"], "status": "auth_error"} for b in books]
    synced = _sync_store.get(user_id)
    start_before_finish = start_before_finish or set()
    results = []
    for book in books:
        try:
            pct = book["progress_percent"]
            status = _target_status(book)
            state_key = _book_state_key(book)
            prev = _previous_sync(synced, book)

            # Only a confirmed edition is ever written to. Confirming a
            # different one than an earlier sync used is a correction, so prev
            # is dropped, which also defeats the unchanged check below: the new
            # edition has none of the old one's progress.
            book_id = _confirmed_edition_id(user_id, book.get("abs_item_id"))
            if not book_id:
                logger.info("[%s] '%s' has no confirmed StoryGraph edition — skipping", label, book["title"])
                results.append(_sync_result(book, "needs_edition"))
                continue
            if prev and prev.get("storygraph_book_id") != book_id:
                if prev.get("storygraph_book_id"):
                    logger.info(
                        "[%s] '%s': confirmed edition %s replaces %s",
                        label, book["title"], book_id, prev.get("storygraph_book_id"),
                    )
                prev = None

            if (
                state_key not in start_before_finish
                and prev is not None
                and prev.get("status") == status
                and abs(pct - prev.get("pct", -1)) < 0.5
            ):
                logger.info("[%s] '%s' unchanged (%s, %.1f%%) — skipping", label, book["title"], status, pct)
                results.append(_sync_result(book, "unchanged"))
                continue

            # A short book can be first observed after it has already finished.
            # Preserve both lifecycle transitions without creating a separate
            # StoryGraph write path for the scheduler.
            if status == "read" and state_key in start_before_finish:
                started_ok, _, _ = client.ensure_status(book_id, "currently-reading")
                if not started_ok:
                    results.append(_sync_result(book, "failed"))
                    continue

            ok, already_matched, status_html = client.ensure_status(book_id, status)
            if not ok:
                results.append(_sync_result(book, "failed"))
                continue
            target_pct = 100 if status == "read" else pct
            # Skip the progress POST when StoryGraph already agrees, since every
            # POST adds a journal entry. A matched "read" status is 100% by
            # definition; a matched "currently reading" label says nothing about
            # the percentage, so compare against what StoryGraph has on file and
            # still post if that can't be read.
            current_pct = _parse_current_progress(status_html or "") if already_matched else None
            skip_progress = status == "to-read" or (
                already_matched
                and (status == "read" or (current_pct is not None and abs(current_pct - target_pct) < 0.5))
            )
            if write_progress and not skip_progress:
                ok = client.update_progress(book_id, target_pct, html=status_html)
            if ok:
                synced[state_key] = {
                    "pct": pct,
                    "current_minutes": book["current_minutes"],
                    "status": status,
                    "storygraph_book_id": book_id,
                }
                if state_key != book["title"]:
                    synced.pop(book["title"], None)
                _sync_store.save(user_id)
            results.append(_sync_result(book, "success" if ok else "failed"))
        except Exception as e:
            logger.error("[%s] Error syncing '%s': %s", label, book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    return results


def _progress_lifecycle(progress: dict) -> tuple[int | str | None, int | str | None]:
    """Stable tokens for lifecycle events, including older ABS responses that
    omit their timestamps."""
    started = progress.get("startedAt")
    if started is None and ((progress.get("currentTime") or 0) > 0 or progress.get("isFinished")):
        started = "started"
    finished = progress.get("finishedAt")
    if finished is None and progress.get("isFinished"):
        finished = "finished"
    return started, finished


def _lifecycle_changes(progress_by_item: dict[str, dict], state: dict) -> dict[str, dict]:
    """Return unhandled starts/finishes. The first snapshot is a quiet baseline
    so an upgrade cannot replay a user's historical library."""
    handled = state.setdefault("books", {})
    if not state.get("initialized"):
        for item_id, progress in progress_by_item.items():
            started, finished = _progress_lifecycle(progress)
            handled[item_id] = {
                "handled_started_at": started,
                "handled_finished_at": finished,
            }
        state["initialized"] = True
        return {}

    changes = {}
    for item_id, progress in progress_by_item.items():
        started, finished = _progress_lifecycle(progress)
        item_state = handled.setdefault(item_id, {})
        start_changed = started is not None and started != item_state.get("handled_started_at")
        finish_changed = finished is not None and finished != item_state.get("handled_finished_at")
        if start_changed or finish_changed:
            changes[item_id] = {"start": start_changed, "finish": finish_changed}
    return changes


def _parse_daily_sync_time(value: str) -> tuple[int, int]:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour, parsed.minute


def _daily_schedule(user_id: str, now: datetime | None = None) -> tuple[bool, str]:
    timezone_name = cfg(user_id, "TIMEZONE", DEFAULT_TIMEZONE)
    try:
        user_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        user_timezone = ZoneInfo(DEFAULT_TIMEZONE)
    local_now = (now or datetime.now(timezone.utc)).astimezone(user_timezone)
    try:
        hour, minute = _parse_daily_sync_time(cfg(user_id, "DAILY_SYNC_TIME", DEFAULT_DAILY_SYNC_TIME))
    except (TypeError, ValueError):
        hour, minute = _parse_daily_sync_time(DEFAULT_DAILY_SYNC_TIME)
    scheduled = datetime.combine(local_now.date(), datetime_time(hour, minute), user_timezone)
    today = local_now.date().isoformat()
    ran_today = _scheduler_store.get(user_id).get("last_daily_run") == today
    due = not ran_today and local_now >= scheduled
    return due, today


def _frequent_sync_candidates(user_id: str, books: list[dict]) -> list[dict]:
    candidates = []
    synced = _sync_store.get(user_id)
    for book in books:
        previous_state = _previous_sync(synced, book) or {}
        previous = _last_synced_minutes(user_id, book) or 0.0
        newly_finished = book["is_finished"] and previous_state.get("status") != "read"
        progressed = not book["is_finished"] and book["current_minutes"] - previous >= SYNC_THRESHOLD
        if newly_finished or progressed:
            candidates.append(book)
    return candidates


def _history_checkpoints(
    sessions: list[dict],
    duration_minutes: float,
    start_date: date_cls | None = None,
    end_date: date_cls | None = None,
) -> dict[str, dict]:
    """Rebuild trusted ABS checkpoints, optionally limited to a date range."""
    checkpoints = {}
    for day in build_history_preview(sessions, duration_minutes)["days"]:
        checkpoint_date = date_cls.fromisoformat(day["date"])
        if start_date and checkpoint_date < start_date:
            continue
        if end_date and checkpoint_date > end_date:
            continue
        checkpoints[day_key(day["date"], day["end_position_minutes"])] = day
    return checkpoints


class HistoryReconcileError(Exception):
    """A book-level precondition failed before dated writes could be checked."""


def _reconcile_history_checkpoints(
    user_id: str,
    item_id: str,
    storygraph_book_id: str,
    checkpoints: dict[str, dict],
    requested: set[str],
    client: StoryGraphClient,
    label: str,
    ensure_read_status: bool = True,
) -> list[dict]:
    """Write and verify trusted daily checkpoints for manual and daily sync.

    `requested` contains checkpoint keys, never caller-supplied dates or
    percentages. Both call sites therefore share exactly the same duplicate
    checks, durable import state, write path, and post-write verification.
    """
    if ensure_read_status:
        status_ok, _, _ = client.ensure_status(storygraph_book_id, "currently-reading")
        if not status_ok:
            raise HistoryReconcileError(
                "Could not set this book to 'currently reading' on StoryGraph, "
                "which a dated entry needs to attach to"
            )

    item_state = _import_store.get(user_id).setdefault(item_id, {})
    imported_days = item_state.setdefault("imported_days", {})
    # Daily sync retries every poll until it succeeds, so a book whose days are
    # all already imported must not cost a StoryGraph journal fetch each time.
    needs_journal = any(key in checkpoints and key not in imported_days for key in requested)
    already_logged = client.get_logged_progress_dates(storygraph_book_id) if needs_journal else set()
    results = []
    posted = []

    for key in sorted(requested):
        day = checkpoints.get(key)
        if day is None:
            results.append({
                "date": key.split("@", 1)[0],
                "status": "skipped",
                "reason": "stale_preview",
            })
            continue
        checkpoint_date, percent = day["date"], day["progress_percent"]
        if key in imported_days:
            results.append({"date": checkpoint_date, "status": "skipped", "reason": "already_imported"})
            continue
        if checkpoint_date in already_logged:
            results.append({
                "date": checkpoint_date,
                "status": "skipped",
                "reason": "already_logged_on_storygraph",
            })
            continue
        if percent is None:
            results.append({"date": checkpoint_date, "status": "skipped", "reason": "no_percent"})
            continue
        try:
            ok = client.add_dated_progress_entry(storygraph_book_id, checkpoint_date, percent)
        except req.RequestException as exc:
            logger.warning("[%s] History write failed for %s on %s: %s", label, item_id, checkpoint_date, exc)
            results.append({"date": checkpoint_date, "status": "failed", "reason": str(exc)})
            continue
        if ok:
            posted.append({"date": checkpoint_date, "percent": percent, "key": key})
        else:
            results.append({"date": checkpoint_date, "status": "failed", "reason": "storygraph_rejected"})

    # The form can return HTTP success without saving. Only the journal is
    # authoritative, so never advance durable state until the entry appears.
    if posted:
        try:
            now_logged = client.get_logged_progress_dates(storygraph_book_id)
        except req.RequestException:
            now_logged = set()
        for entry in posted:
            if entry["date"] in now_logged:
                imported_days[entry["key"]] = {
                    "percent": entry["percent"],
                    "imported_at": time.time(),
                }
                results.append({
                    "date": entry["date"],
                    "status": "success",
                    "progress_percent": entry["percent"],
                })
            else:
                results.append({
                    "date": entry["date"],
                    "status": "failed",
                    "reason": "storygraph_did_not_save",
                })
        _import_store.save(user_id)

    results.sort(key=lambda result: result["date"])
    return results


def _daily_history_range(state: dict, local_date: str) -> tuple[date_cls, date_cls]:
    """Completed local days this run owns: yesterday, plus downtime catch-up."""
    run_date = date_cls.fromisoformat(local_date)
    end_date = run_date - timedelta(days=1)
    try:
        start_date = date_cls.fromisoformat(state.get("last_daily_run", ""))
    except (TypeError, ValueError):
        start_date = end_date
    return min(start_date, end_date), end_date


def _daily_history_sync(
    user_id: str,
    books: list[dict],
    start_date: date_cls,
    end_date: date_cls,
    client: StoryGraphClient,
    label: str,
) -> bool:
    """Reconcile completed ABS listening days through History Import's path."""
    all_ok = True
    for book in books:
        item_id = book.get("abs_item_id")
        if not item_id or book.get("current_minutes", 0) <= 0:
            continue
        try:
            sessions = get_abs_listening_sessions(user_id, item_id)
            checkpoints = _history_checkpoints(
                sessions,
                book.get("duration_minutes", 0),
                start_date,
                end_date,
            )
            if not checkpoints:
                continue
            storygraph_book_id = _confirmed_edition_id(user_id, item_id)
            if not storygraph_book_id:
                # Retrying can't conjure a confirmation, so this must not hold
                # the whole daily run back for every other book.
                logger.warning("[%s] Daily history has no confirmed edition for '%s'; skipping it", label, book["title"])
                continue
            results = _reconcile_history_checkpoints(
                user_id,
                item_id,
                storygraph_book_id,
                checkpoints,
                set(checkpoints),
                client,
                label,
                ensure_read_status=False,
            )
            if any(result["status"] == "failed" for result in results):
                all_ok = False
            written = sum(1 for result in results if result["status"] == "success")
            logger.info(
                "[%s] Daily history for '%s' (%s to %s): %d/%d days written",
                label, book["title"], start_date, end_date, written, len(results),
            )
        except (req.RequestException, ValueError) as exc:
            logger.warning("[%s] Daily history failed for '%s': %s", label, book["title"], exc)
            all_ok = False
    return all_ok


def _mark_handled_lifecycle(state: dict, progress: dict, item_id: str):
    started, finished = _progress_lifecycle(progress)
    item_state = state.setdefault("books", {}).setdefault(item_id, {})
    if started is not None:
        item_state["handled_started_at"] = started
    if finished is not None:
        item_state["handled_finished_at"] = finished
    elif not progress.get("isFinished"):
        item_state["handled_finished_at"] = None


def _poll_user(user: dict, now: datetime | None = None):
    user_id = user["id"]
    if _missing_cfg(user_id, SYNC_KEYS):
        return
    scope = cfg(user_id, "SYNC_SCOPE", DEFAULT_SYNC_SCOPE)
    mode = cfg(user_id, "SYNC_MODE", DEFAULT_SYNC_MODE)
    label = _user_label(user)
    try:
        progress_by_item = get_abs_progress(user_id)
        scheduler_state = _scheduler_store.get(user_id)
        was_initialized = bool(scheduler_state.get("initialized"))
        changes = _lifecycle_changes(progress_by_item, scheduler_state)
        synced_state = _sync_store.get(user_id)
        for item_id, progress in progress_by_item.items():
            # ABS can retain an old startedAt when a completed book is reopened.
            # The last successful StoryGraph status still makes that transition
            # unambiguous and keeps it retryable if the write fails.
            if (
                (progress.get("currentTime") or 0) > 0
                and not progress.get("isFinished")
                and (synced_state.get(item_id) or {}).get("status") == "read"
            ):
                changes.setdefault(item_id, {"start": False, "finish": False})["start"] = True
        if not was_initialized:
            _scheduler_store.save(user_id)

        daily_due, local_date = _daily_schedule(user_id, now)
        scheduled_run = mode == "daily" and daily_due
        pending_daily = set(scheduler_state.get("pending_daily_items", []))
        if mode == "daily" and changes:
            # A finished book can leave the default In Progress scope before
            # midnight. Retain lifecycle items until their completed listening
            # days have been reconciled successfully.
            pending_daily.update(changes)
            scheduler_state["pending_daily_items"] = sorted(pending_daily)

        scoped_books = []
        if mode == "frequent" or scheduled_run:
            scoped_books = get_abs_books(user_id, scope, progress_by_item=progress_by_item)
            if scheduled_run:
                present = {_book_state_key(book) for book in scoped_books}
                for item_id in sorted(pending_daily - present):
                    progress = progress_by_item.get(item_id)
                    book = get_abs_book(user_id, item_id, progress) if progress is not None else None
                    if book:
                        scoped_books.append(book)
                    else:
                        # Removed from ABS since it finished. Waiting for it
                        # would block every other book's daily run forever.
                        logger.warning("[%s] Daily sync: ABS item %s is gone; dropping it", label, item_id)
            _cache_books(user_id, scope, scoped_books)

        selected = _frequent_sync_candidates(user_id, scoped_books) if mode == "frequent" else []
        by_item = {_book_state_key(book): book for book in selected}
        scoped_by_item = {_book_state_key(book): book for book in scoped_books}
        for item_id in changes:
            if item_id not in by_item:
                book = scoped_by_item.get(item_id) or get_abs_book(
                    user_id,
                    item_id,
                    progress_by_item[item_id],
                )
                if book:
                    by_item[item_id] = book

        client = _storygraph_client(user_id)
        lifecycle_books = list(by_item.values())
        if lifecycle_books:
            start_before_finish = {
                item_id
                for item_id, change in changes.items()
                if change["start"] and change["finish"]
            }
            lifecycle_results = do_sync(
                user_id,
                lifecycle_books,
                start_before_finish=start_before_finish,
                client=client,
                label=label,
            )
            for book, result in zip(lifecycle_books, lifecycle_results):
                item_id = book.get("abs_item_id")
                if item_id and result["status"] in {"success", "unchanged"}:
                    _mark_handled_lifecycle(scheduler_state, progress_by_item.get(item_id, {}), item_id)
            synced = sum(1 for result in lifecycle_results if result["status"] == "success")
            logger.info("[%s] Auto-sync: %d/%d synced", label, synced, len(lifecycle_books))

        if scheduled_run:
            status_results = do_sync(
                user_id,
                scoped_books,
                write_progress=False,
                client=client,
                label=label,
            ) if scoped_books else []
            # Judge each book on its own. A book without a confirmed edition
            # won't gain one on a retry, so it is skipped rather than holding
            # back every other book's history and the day itself.
            # Anything else (auth, network, a rejected write) may clear up, so
            # it keeps the day open for the next poll.
            history_books = []
            retry = False
            for book, result in zip(scoped_books, status_results):
                if result["status"] in {"success", "unchanged"}:
                    history_books.append(book)
                elif result["status"] == "needs_edition":
                    logger.warning(
                        "[%s] Daily sync: no confirmed StoryGraph edition for '%s'; skipping it",
                        label, book["title"],
                    )
                else:
                    retry = True
            start_date, end_date = _daily_history_range(scheduler_state, local_date)
            history_ok = _daily_history_sync(
                user_id,
                history_books,
                start_date,
                end_date,
                client,
                label,
            )
            if history_ok and not retry:
                scheduler_state["last_daily_run"] = local_date
                scheduler_state["pending_daily_items"] = []
            else:
                logger.warning(
                    "[%s] Daily history incomplete; it will retry without duplicating verified days",
                    label,
                )
        if changes or scheduled_run:
            _scheduler_store.save(user_id)
    except Exception as e:
        logger.error("[%s] Auto-sync error: %s", label, e)


def _poll_loop():
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD)
    while True:
        for user in list_users():
            with _user_lock(user["id"]):
                _poll_user(user)
        time.sleep(POLL_INTERVAL)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = _get_secret_key()
# `flask run --reload` only watches .py files; this picks up template-only
# edits too, for the cost of an mtime check per render. The dev compose file is
# the only thing that runs read-only, so that doubles as the dev switch.
app.config["TEMPLATES_AUTO_RELOAD"] = READ_ONLY


@app.context_processor
def _asset_url():
    """url_for('static') with the file's mtime appended, so a browser never
    runs a cached copy of the script or styles after an update."""
    def asset_url(filename: str) -> str:
        mtime = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        return url_for("static", filename=filename, v=mtime)
    return {"asset_url": asset_url}

# Trust one hop of X-Forwarded-* from a reverse proxy, so the OIDC redirect_uri
# built by url_for(..., _external=True) is https:// behind a TLS-terminating proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

oauth = OAuth(app)
if OIDC_ENABLED:
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

PUBLIC_ENDPOINTS = {"login", "login_oidc", "auth_callback", "logout", "setup", "static"}


def needs_abs_item(*required_cfg: str, writes: bool = False):
    """Shared preamble for the /api/<thing>/<item_id> routes: refuse in
    read-only development mode if the route writes to StoryGraph, reject a
    malformed ABS item id, and reject a half-configured account."""
    def decorator(view):
        @wraps(view)
        def wrapped(item_id, *args, **kwargs):
            if writes and READ_ONLY:
                return jsonify({"error": "This is disabled in read-only development mode"}), 403
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", item_id):
                return jsonify({"error": "Invalid Audiobookshelf item ID"}), 400
            missing = _missing_cfg(g.user["id"], required_cfg)
            if missing:
                return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
            return view(item_id, *args, **kwargs)
        return wrapped
    return decorator


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def load_user():
    g.user = None
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if user_id:
        g.user = get_user(user_id)
        if g.user is None:
            session.clear()
    if g.user is None and not any_users_exist():
        if request.endpoint != "setup":
            return redirect(url_for("setup"))
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if g.user is None:
        return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_users_exist():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Username and password are required."
        else:
            user = create_user(username=username, password=password, is_admin=True)
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = get_user_by_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        if user and user.get("password_hash") and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, oidc_enabled=OIDC_ENABLED)


@app.route("/login/oidc")
def login_oidc():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    redirect_uri = f"{PUBLIC_URL}/auth/callback" if PUBLIC_URL else url_for("auth_callback", _external=True)
    return oauth.oidc.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    token = oauth.oidc.authorize_access_token()
    claims = token.get("userinfo") or oauth.oidc.userinfo(token=token)
    sub = claims.get("sub")
    if not sub:
        return redirect(url_for("login"))
    user = get_user_by_oidc_sub(sub)
    if not user:
        display_name = claims.get("preferred_username") or claims.get("email") or sub
        user = create_user(oidc_sub=sub, display_name=display_name, is_admin=False)
    session["user_id"] = user["id"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template(
        "index.html",
        is_admin=bool(g.user.get("is_admin")),
        display_name=g.user.get("display_name") or g.user.get("username"),
    )


@app.route("/editions")
def editions_page():
    return render_template(
        "editions.html",
        is_admin=bool(g.user.get("is_admin")),
        display_name=g.user.get("display_name") or g.user.get("username"),
    )


@app.route("/api/status")
def api_status():
    user_id = g.user["id"]
    scope = cfg(user_id, "SYNC_SCOPE", DEFAULT_SYNC_SCOPE)
    books, abs_ok = [], False
    if not _missing_cfg(user_id, ABS_KEYS):
        books, abs_ok = get_cached_books(user_id, scope)
    mode = cfg(user_id, "SYNC_MODE", DEFAULT_SYNC_MODE)
    last = {
        _book_state_key(book): synced_minutes
        for book in books
        if (synced_minutes := _last_synced_minutes(user_id, book)) is not None
    }
    return jsonify({
        "abs_ok": abs_ok,
        "sg_ok": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "read_only": READ_ONLY,
        "poll_interval": POLL_INTERVAL,
        "sync_scope": scope,
        "sync_mode": mode,
        "daily_sync_time": cfg(user_id, "DAILY_SYNC_TIME", DEFAULT_DAILY_SYNC_TIME),
        "books": books,
        "last_synced": last,
        "edition_states": {
            book["abs_item_id"]: _edition_entry(user_id, book["abs_item_id"]).get("state", "unchecked")
            for book in books
        },
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    user_id = g.user["id"]
    if READ_ONLY:
        return jsonify({"error": "Sync is disabled in read-only development mode"}), 403
    missing = _missing_cfg(user_id, SYNC_KEYS)
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    scope = cfg(user_id, "SYNC_SCOPE", DEFAULT_SYNC_SCOPE)
    label = _user_label(g.user)
    try:
        books = get_abs_books(user_id, scope)
        if not books:
            return jsonify({"message": "No books found", "synced": 0, "total": 0, "results": []})
        with _user_lock(user_id):
            results = do_sync(user_id, books, label=label)
        synced = sum(1 for r in results if r["status"] == "success")
        return jsonify({"message": "Sync complete", "synced": synced, "total": len(books), "results": results})
    except Exception as e:
        logger.error("[%s] Sync failed: %s", label, e)
        return jsonify({"error": str(e)}), 500


def _resolve_abs_book(user_id: str, item_id: str) -> dict | None:
    """Find a book's ABS metadata: from the /api/status list if it's already
    cached, else a direct fetch of just this item (never a whole-scope refresh)."""
    books = _fresh_cached_books(user_id, cfg(user_id, "SYNC_SCOPE", DEFAULT_SYNC_SCOPE)) or []
    book = next((candidate for candidate in books if candidate.get("abs_item_id") == item_id), None)
    if book:
        return book
    progress_resp = _abs_get(user_id, f"/api/me/progress/{item_id}", required=False)
    return get_abs_book(user_id, item_id, progress_resp.json() if progress_resp is not None else {})


def _edition_json(candidate, details: AudiobookDetails | None = None) -> dict:
    return {
        "storygraph_book_id": candidate.book_id,
        "title": candidate.title,
        "format": candidate.format,
        "duration_minutes": candidate.duration_minutes,
        "identifier": candidate.identifier,
        "narrators": list(candidate.narrators),
        "publisher": candidate.publisher,
        "language": candidate.language,
        "read_by_you": candidate.read_by_you,
        # Against this ABS book's narrator, publisher and language, so a
        # person choosing between candidates can see what agrees.
        "checks": edition_checks(candidate, details),
    }


def _tagged_edition_title(client: StoryGraphClient, book_id: str) -> str:
    """The title on a tagged edition's book page, or "" if it can't be read.
    Only a label, so a failure here never stops the lookup."""
    try:
        html = client.get_book_page(book_id)
    except req.RequestException as exc:
        logger.warning("Could not read the tagged StoryGraph edition %s: %s", book_id, exc)
        return ""
    if not html or "/sign_in" in html[:200]:
        return ""
    return _storygraph_page_title(html) or ""


def _storygraph_page_title(html: str) -> str | None:
    """Book title read from an already-fetched StoryGraph book page."""
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return None
    return re.sub(r"\s*\|\s*The StoryGraph\s*$", "", m.group(1)).strip() or None


def _look_up_edition(user_id: str, client: StoryGraphClient, book: dict, query: str = "") -> dict:
    """Search StoryGraph for this ABS book and record what came back, as a
    suggestion or a list of candidates. Never confirms anything, and never
    unconfirms: a lookup on a confirmed book only refreshes its candidates, so
    a person can pick a different edition."""
    search = query or f"{book['title']} {book['author']}".strip()
    editions = client.load_editions(search, normalise_language(book.get("language")) or None)
    tagged_id = book.get("storygraph_tag")
    tagged_unlisted = bool(tagged_id) and all(candidate.book_id != tagged_id for candidate in editions)
    if tagged_unlisted:
        # The search didn't turn up the edition ABS is tagged with, but it's
        # still the one a person confirmed, so it's offered with what its book
        # page says (only the title).
        editions.append(EditionCandidate(tagged_id, _tagged_edition_title(client, tagged_id), "", None, None, None, None))
    details = _audiobook_details(book)
    matched, reason = _match_audio_edition(
        editions, book["title"],
        duration_minutes=book.get("duration_minutes", 0),
        identifiers=book.get("identifiers", []),
        details=details,
        tagged_id=tagged_id,
    )
    if tagged_unlisted:
        reason["tagged_unlisted"] = True
    audio = [candidate for candidate in editions if candidate.is_audio]
    read = next((candidate for candidate in editions if candidate.read_by_you), None)
    if not matched and read:
        # Nothing matched as audio, so suggest staying on the StoryGraph entry
        # you already have. It still needs confirming like any suggestion.
        matched = read
        reason["read_edition"]["fallback"] = True
    offered = _offered_candidates(audio or editions, book, details, keep=(matched, read))
    with _user_lock(user_id):
        entry = _editions(user_id).setdefault(book["abs_item_id"], {})
        entry["candidates"] = [_edition_json(candidate, details) for candidate in offered]
        entry["checked_at"] = time.time()
        # Why the last lookup did or didn't find a confident match, in terms
        # of this book's ABS runtime — shown so a "no match" isn't a mystery.
        entry["reason"] = {**reason, "query": search, "abs_runtime_minutes": book.get("duration_minutes", 0)}
        if entry.get("state") != "confirmed":
            entry["state"] = "suggested" if matched else "unmatched"
            entry["edition"] = _edition_json(matched, details) if matched else None
        _edition_store.save(user_id)
        return dict(entry)


MAX_OFFERED_EDITIONS = 12


def _offered_candidates(candidates: list, book: dict, details: AudiobookDetails, keep) -> list:
    """The editions worth offering a person, closest first: matching language,
    then nearest runtime. A popular book has dozens of near-duplicate audio
    editions, so only the closest few are kept, plus the suggestion and the
    edition you've read whatever they are. Without any audio edition (an
    ebook, or the wrong search hit), every edition is offered rather than none."""
    target = book.get("duration_minutes") or 0

    def closeness(candidate):
        wrong_language = edition_checks(candidate, details)["language"] is False
        runtime = candidate.duration_minutes
        return (wrong_language, runtime is None, abs(runtime - target) if runtime is not None and target else 0)

    offered = sorted(candidates, key=closeness)[:MAX_OFFERED_EDITIONS]
    for candidate in keep:
        if candidate and candidate not in offered:
            offered.append(candidate)
    return offered


def _edition_row(user_id: str, book: dict, entry: dict) -> dict:
    """One ABS book and its edition entry, as the Editions page shows it."""
    synced = _previous_sync(_sync_store.get(user_id), book) or {}
    return {
        "abs_item_id": book["abs_item_id"],
        "title": book["title"],
        "author": book["author"],
        "duration_minutes": book["duration_minutes"],
        "identifiers": book.get("identifiers", []),
        "narrators": book.get("narrators", []),
        "publisher": book.get("publisher"),
        "language": book.get("language"),
        "storygraph_tag": book.get("storygraph_tag"),
        "progress_percent": book["progress_percent"],
        "is_finished": book["is_finished"],
        "state": entry.get("state", "unchecked"),
        "edition": entry.get("edition"),
        "candidates": entry.get("candidates", []),
        "checked_at": entry.get("checked_at"),
        "reason": entry.get("reason"),
        # Progress already sent here stays on StoryGraph if a different
        # edition is confirmed, so the page can warn before that happens.
        "synced_book_id": synced.get("storygraph_book_id"),
    }


@app.route("/api/history-import-preview/<item_id>")
@needs_abs_item("ABS_URL", "ABS_TOKEN")
def api_history_import_preview(item_id):
    """The day-by-day history ABS reports for this book, plus whatever
    StoryGraph needs for an import. Without a StoryGraph session it is just the
    read-only history, with no edition matched."""
    user_id = g.user["id"]
    try:
        book = _resolve_abs_book(user_id, item_id)
        if not book:
            return jsonify({"error": "Audiobook metadata was incomplete"}), 422

        sessions = get_abs_listening_sessions(user_id, item_id)
        preview = build_history_preview(sessions, book["duration_minutes"])

        storygraph_ready = bool(cfg(user_id, "STORYGRAPH_SESSION"))
        entry = {}
        logged_dates = set()
        if storygraph_ready:
            client = _storygraph_client(user_id)
            try:
                entry = _edition_entry(user_id, item_id)
                # Opening a book's History is a person asking about it, so an
                # unchecked book gets its lookup here.
                if not entry:
                    entry = _look_up_edition(user_id, client, book)
                if entry.get("state") == "confirmed":
                    logged_dates = client.get_logged_progress_dates(entry["edition"]["storygraph_book_id"])
            except StoryGraphAuthError:
                storygraph_ready = False

        imported_days = (_import_store.get(user_id).get(item_id) or {}).get("imported_days", {})
        days = []
        for day in preview["days"]:
            key = day_key(day["date"], day["end_position_minutes"])
            days.append({
                **day,
                "key": key,
                "already_imported": key in imported_days,
                "already_logged_on_storygraph": day["date"] in logged_dates,
            })

        return jsonify({
            "book": {
                "abs_item_id": item_id,
                "title": book["title"],
                "author": book["author"],
                "duration_minutes": book["duration_minutes"],
                "identifiers": book.get("identifiers", []),
            },
            "storygraph_ready": storygraph_ready,
            "edition_state": entry.get("state", "unchecked"),
            "matched_edition": entry.get("edition"),
            "candidates": entry.get("candidates", []),
            "match_reason": entry.get("reason"),
            "synced_book_id": (_previous_sync(_sync_store.get(user_id), book) or {}).get("storygraph_book_id"),
            "summary": preview["summary"],
            "days": days,
        })
    except req.RequestException as exc:
        logger.warning("History import preview failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502


@app.route("/api/history-import/<item_id>", methods=["POST"])
@needs_abs_item("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION", writes=True)
def api_history_import(item_id):
    """The actual write: posts one dated StoryGraph journal entry per confirmed
    checkpoint, in chronological order, skipping anything already imported by
    this tool or already logged on StoryGraph.

    The request body carries checkpoint *keys* only (history.day_key), never
    dates or percentages. Every value written is rebuilt here from the user's
    own Audiobookshelf sessions, so a crafted request can't invent a date or a
    percentage ABS never reported, and can't reach the write path with a
    malformed one. A key whose day has since shifted simply won't match the
    rebuilt set and is reported back as a stale preview."""
    user_id = g.user["id"]
    days = (request.json or {}).get("days")
    requested = {k for k in days if isinstance(k, str)} if isinstance(days, list) else set()
    if not requested:
        return jsonify({"error": "No days were confirmed for import"}), 400

    storygraph_book_id = _confirmed_edition_id(user_id, item_id)
    if not storygraph_book_id:
        return jsonify({"error": "Confirm a StoryGraph edition for this book before importing"}), 400

    try:
        book = _resolve_abs_book(user_id, item_id)
        if not book:
            return jsonify({"error": "Audiobook metadata was incomplete"}), 422
        sessions = get_abs_listening_sessions(user_id, item_id)
    except req.RequestException as exc:
        logger.warning("History import could not re-read ABS for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502
    checkpoints = _history_checkpoints(sessions, book["duration_minutes"])

    label = _user_label(g.user)
    try:
        client = _storygraph_client(user_id)
        if not client.check_auth():
            return jsonify({"error": "StoryGraph session invalid — update it in Settings"}), 401
        with _user_lock(user_id):
            results = _reconcile_history_checkpoints(
                user_id,
                item_id,
                storygraph_book_id,
                checkpoints,
                requested,
                client,
                label,
            )
    except HistoryReconcileError as exc:
        return jsonify({"error": str(exc)}), 502
    except req.RequestException as exc:
        logger.warning("History import failed to reach StoryGraph for %s: %s", item_id, exc)
        return jsonify({"error": "Could not reach StoryGraph"}), 502
    imported = sum(1 for r in results if r["status"] == "success")
    logger.info("[%s] History import for %s: %d/%d days written", label, item_id, imported, len(results))
    return jsonify({"imported": imported, "total": len(results), "results": results})


@app.route("/api/editions")
def api_editions():
    """Every book in the ABS library, whatever the sync scope, with its edition
    entry. Reads only local state and ABS — never StoryGraph."""
    user_id = g.user["id"]
    missing = _missing_cfg(user_id, ABS_KEYS)
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    try:
        books = get_abs_books(user_id, "library")
    except req.RequestException as exc:
        logger.warning("Editions list could not read ABS: %s", exc)
        return jsonify({"error": "Could not load your library from Audiobookshelf"}), 502
    editions = _editions(user_id)
    return jsonify({
        "storygraph_ready": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "books": [_edition_row(user_id, book, editions.get(book["abs_item_id"]) or {}) for book in books],
    })


@app.route("/api/editions/<item_id>/lookup", methods=["POST"])
@needs_abs_item(*ABS_KEYS, "STORYGRAPH_SESSION")
def api_edition_lookup(item_id):
    """Search StoryGraph for one book, optionally with a person's own search
    words when the title search lands on the wrong work. Records a suggestion
    or candidates only, so it stays available in read-only development mode."""
    user_id = g.user["id"]
    query = str((request.get_json(silent=True) or {}).get("query") or "").strip()[:200]
    try:
        book = _resolve_abs_book(user_id, item_id)
        if not book:
            return jsonify({"error": "Audiobook metadata was incomplete"}), 422
        entry = _look_up_edition(user_id, _storygraph_client(user_id), book, query)
    except StoryGraphAuthError as exc:
        return jsonify({"error": str(exc)}), 401
    except req.RequestException as exc:
        logger.warning("Edition lookup failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not reach StoryGraph or Audiobookshelf"}), 502
    return jsonify(_edition_row(user_id, book, entry))


@app.route("/api/editions/<item_id>/confirm", methods=["POST"])
@needs_abs_item("STORYGRAPH_SESSION")
def api_edition_confirm(item_id):
    """Confirm which StoryGraph edition this ABS book is: the suggestion, one
    of the candidates, or any pasted book URL or id. Only ever records the
    choice — never writes any progress."""
    user_id = g.user["id"]
    data = request.get_json(silent=True) or {}
    match = re.search(r"([0-9a-fA-F-]{36})", str(data.get("storygraph_book_id") or "").strip())
    if not match:
        return jsonify({"error": "That doesn't look like a StoryGraph book id or URL"}), 400
    storygraph_book_id = match.group(1)

    edition = _known_edition(_edition_entry(user_id, item_id), storygraph_book_id)
    if edition is None:
        try:
            html = _storygraph_client(user_id).get_book_page(storygraph_book_id)
        except req.RequestException as exc:
            logger.warning("Edition lookup failed for %s: %s", storygraph_book_id, exc)
            return jsonify({"error": "Could not reach StoryGraph"}), 502
        if not html or "/sign_in" in html[:200]:
            return jsonify({"error": "Could not reach that StoryGraph book"}), 400
        # A book page has the title but not the format or runtime, so those
        # stay unknown for a pasted edition.
        edition = {
            "storygraph_book_id": storygraph_book_id,
            "title": _storygraph_page_title(html),
            "format": None,
            "duration_minutes": None,
            "identifier": None,
        }

    with _user_lock(user_id):
        entry = _editions(user_id).setdefault(item_id, {})
        entry.update(state="confirmed", edition=edition, confirmed_at=time.time())
        entry.setdefault("candidates", [])
        _edition_store.save(user_id)
    logger.info("[%s] Confirmed StoryGraph edition %s for %s", _user_label(g.user), storygraph_book_id, item_id)
    return jsonify({
        "ok": True, "state": "confirmed", "edition": edition,
        "tag_error": _tag_confirmed_edition(user_id, item_id, storygraph_book_id),
    })


def _known_edition(entry: dict, storygraph_book_id: str) -> dict | None:
    """This book's suggestion or one of its candidates, if it's that edition
    and has its details (a title at least), so confirming it needs no fetch."""
    known = [entry.get("edition"), *entry.get("candidates", [])]
    return next(
        (dict(e) for e in known if e and e.get("storygraph_book_id") == storygraph_book_id and e.get("title")),
        None,
    )


def _tag_confirmed_edition(user_id: str, item_id: str, storygraph_book_id: str) -> str | None:
    """Tag the ABS book with the edition just confirmed, so the next lookup
    (from anyone, or after losing this app's data) starts from it. The
    confirmation stands either way; this returns why tagging failed, if it did."""
    if READ_ONLY or _missing_cfg(user_id, ABS_KEYS):
        return None
    try:
        _write_tag_logged(user_id, item_id, storygraph_book_id)
    except req.RequestException as exc:
        return _tag_error(exc)
    return None


def _write_tag_logged(user_id: str, item_id: str, storygraph_book_id: str):
    try:
        if write_storygraph_tag(user_id, item_id, storygraph_book_id):
            logger.info("Tagged ABS item %s with %s%s", item_id, STORYGRAPH_TAG_PREFIX, storygraph_book_id)
    except req.RequestException as exc:
        logger.warning("Could not tag ABS item %s with its StoryGraph edition: %s", item_id, exc)
        raise


def _tag_error(exc: req.RequestException) -> str:
    if getattr(exc.response, "status_code", None) == 403:
        return "Your Audiobookshelf user isn't allowed to update books, so editions can't be tagged there"
    return "Could not tag the edition in Audiobookshelf"


@app.route("/api/editions/sync-tags", methods=["POST"])
def api_edition_tag_sync():
    """Line up confirmed editions and ABS tags, both ways, across the whole
    library: a book tagged in ABS but not confirmed here is confirmed as the
    tagged edition, and a confirmed book without a tag gets one. A book
    confirmed here as a different edition from its tag is left alone and
    reported, since each was somebody's pick. Never touches StoryGraph."""
    user_id = g.user["id"]
    missing = _missing_cfg(user_id, ABS_KEYS)
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    try:
        books = get_abs_books(user_id, "library")
    except req.RequestException as exc:
        logger.warning("Tag sync could not read ABS: %s", exc)
        return jsonify({"error": "Could not load your library from Audiobookshelf"}), 502

    confirmed, to_tag, conflicts = [], [], []
    with _user_lock(user_id):
        editions = _editions(user_id)
        for book in books:
            item_id, tagged_id = book["abs_item_id"], book.get("storygraph_tag")
            confirmed_id = _confirmed_edition_id(user_id, item_id)
            if tagged_id and not confirmed_id:
                entry = editions.setdefault(item_id, {})
                entry.update(
                    state="confirmed",
                    # A tag carries only the id; the details are kept when a
                    # lookup already found this edition.
                    edition=_known_edition(entry, tagged_id) or {
                        "storygraph_book_id": tagged_id, "title": None, "format": None,
                        "duration_minutes": None, "identifier": None,
                    },
                    confirmed_at=time.time(),
                )
                entry.setdefault("candidates", [])
                confirmed.append(item_id)
            elif confirmed_id and not tagged_id:
                to_tag.append((item_id, confirmed_id))
            elif tagged_id and tagged_id != confirmed_id:
                conflicts.append(book["title"])
        if confirmed:
            _edition_store.save(user_id)

    tagged, tag_error = 0, None
    for item_id, storygraph_book_id in ([] if READ_ONLY else to_tag):
        try:
            _write_tag_logged(user_id, item_id, storygraph_book_id)
            tagged += 1
        except req.RequestException as exc:
            tag_error = _tag_error(exc)
            if getattr(exc.response, "status_code", None) == 403:
                break  # every other book would be refused too
    logger.info(
        "[%s] Tag sync: %d confirmed from ABS tags, %d tagged in ABS, %d differ",
        _user_label(g.user), len(confirmed), tagged, len(conflicts),
    )
    return jsonify({
        "confirmed": len(confirmed),
        "tagged": tagged,
        "untagged": len(to_tag) - tagged,
        "conflicts": conflicts,
        "tag_error": tag_error,
        "read_only": READ_ONLY,
    })


@app.route("/api/logs")
@admin_required
def api_logs():
    return jsonify({"logs": _log_buffer.get()})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    user_id = g.user["id"]
    data = request.json or {}
    allowed = {
        "ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION", "STORYGRAPH_REMEMBER_TOKEN",
        "SYNC_SCOPE", "SYNC_MODE", "DAILY_SYNC_TIME", "TIMEZONE",
    }
    if "ABS_URL" in data and data["ABS_URL"]:
        parsed = urlparse(data["ABS_URL"])
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "ABS_URL must use http or https"}), 400
        if not parsed.hostname:
            return jsonify({"error": "ABS_URL must include a hostname"}), 400
    if data.get("SYNC_SCOPE") and data["SYNC_SCOPE"] not in SYNC_SCOPES:
        return jsonify({"error": "Invalid SYNC_SCOPE"}), 400
    if data.get("SYNC_MODE") and data["SYNC_MODE"] not in SYNC_MODES:
        return jsonify({"error": "Invalid SYNC_MODE"}), 400
    if data.get("DAILY_SYNC_TIME"):
        try:
            _parse_daily_sync_time(data["DAILY_SYNC_TIME"])
        except (TypeError, ValueError):
            return jsonify({"error": "DAILY_SYNC_TIME must use HH:MM"}), 400
    if data.get("TIMEZONE"):
        try:
            ZoneInfo(data["TIMEZONE"])
        except (TypeError, ZoneInfoNotFoundError):
            return jsonify({"error": "Invalid TIMEZONE"}), 400
    set_cfg(user_id, {k: v for k, v in data.items() if k in allowed})
    with _status_cache_lock:
        _status_cache.pop(user_id, None)
    logger.info("[%s] Settings updated via UI", _user_label(g.user))
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current config keys (masked values) so the UI can show what's set."""
    user_id = g.user["id"]
    return jsonify({
        "ABS_URL": cfg(user_id, "ABS_URL"),
        "ABS_TOKEN": "set" if cfg(user_id, "ABS_TOKEN") else "",
        "STORYGRAPH_SESSION": "set" if cfg(user_id, "STORYGRAPH_SESSION") else "",
        "STORYGRAPH_REMEMBER_TOKEN": "set" if cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN") else "",
        "SYNC_SCOPE": cfg(user_id, "SYNC_SCOPE", DEFAULT_SYNC_SCOPE),
        "SYNC_MODE": cfg(user_id, "SYNC_MODE", DEFAULT_SYNC_MODE),
        "DAILY_SYNC_TIME": cfg(user_id, "DAILY_SYNC_TIME", DEFAULT_DAILY_SYNC_TIME),
        "TIMEZONE": cfg(user_id, "TIMEZONE", DEFAULT_TIMEZONE),
    })


@app.route("/api/users")
@admin_required
def api_users():
    users = [{
        "id": u["id"],
        "username": u.get("username"),
        "display_name": u.get("display_name"),
        "is_admin": bool(u.get("is_admin")),
        "via_oidc": bool(u.get("oidc_sub")),
    } for u in list_users()]
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "Username already taken"}), 400
    user = create_user(username=username, password=password, is_admin=bool(data.get("is_admin")))
    return jsonify({"id": user["id"]})


@app.route("/api/users/<user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot delete your own account"}), 400
    delete_user(user_id)
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>/admin", methods=["POST"])
@admin_required
def api_set_admin(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot change your own admin status"}), 400
    data = request.json or {}
    update_user(user_id, is_admin=bool(data.get("is_admin")))
    return jsonify({"ok": True})


if __name__ == "__main__":
    if not READ_ONLY:
        threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
