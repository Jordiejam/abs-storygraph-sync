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
from matcher import choose_audio_edition, parse_storygraph_editions
from history import build_history_preview, day_key
from journal import parse_journal_page, progress_dates

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
USERS_FILE = f"{DATA_DIR}/users.json"
CONFIG_DIR = f"{DATA_DIR}/config"
SYNC_STATE_DIR = f"{DATA_DIR}/sync_state"
IMPORT_STATE_DIR = f"{DATA_DIR}/import_state"
SCHEDULER_STATE_DIR = f"{DATA_DIR}/scheduler_state"

STORYGRAPH_BASE = "https://app.thestorygraph.com"
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

# Per book (keyed by abs_item_id): {"edition": <the resolved StoryGraph
# edition, auto-matched or manually picked>, "imported_days": {day_key ->
# {percent, imported_at}}}. Saved immediately after each verified dated write,
# so a rerun can never double-import a day and a failure partway through still
# leaves correct partial state on disk.
_import_store = _UserJsonStore(IMPORT_STATE_DIR)

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


def _saved_edition(user_id: str, item_id: str) -> dict | None:
    """The StoryGraph edition History Import has resolved for this ABS item, if
    any — the shared source of truth for the import routes and regular sync."""
    return (_import_store.get(user_id).get(item_id) or {}).get("edition")

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
    resp = _abs_get(user_id, f"/api/items/{item_id}", required=False)
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
        resp = self._get("/")
        return "sign_in" not in resp.url

    def _find_initial_book_id(self, title, author) -> str | None:
        query = req.utils.quote(f"{title} {author}".strip())
        resp = self._get(f"/browse?search_term={query}")
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        link = soup.find("a", class_="book-title-link")
        if not link:
            container = soup.find(class_="book-title-author-and-series")
            if container:
                link = container.find("a", href=re.compile(r"^/books/"))
        if not link:
            return None
        m = re.search(r"/books/([^/?]+)", link.get("href", ""))
        return m.group(1) if m else None

    def load_editions(self, title, author) -> list:
        """Every edition StoryGraph lists for the closest search result to
        (title, author). Pass the result to match_audio_edition()."""
        initial_id = self._find_initial_book_id(title, author)
        if not initial_id:
            logger.warning("No StoryGraph result for '%s'", title)
            return []
        editions_resp = self._get(f"/books/{initial_id}/editions")
        if editions_resp.status_code != 200:
            logger.warning("Could not load StoryGraph editions for '%s'", title)
            return []
        return parse_storygraph_editions(editions_resp.text)

    def match_audio_edition(self, candidates, title, duration_minutes=0, identifiers=None):
        """The single confident audio-edition match from an already-loaded
        candidate list (full candidate, for display), or None if nothing meets
        matcher.choose_audio_edition's tolerance."""
        matched = choose_audio_edition(
            candidates,
            target_duration_minutes=duration_minutes,
            identifiers=identifiers or [],
        )
        if matched:
            delta = abs((matched.duration_minutes or duration_minutes) - duration_minutes)
            logger.info(
                "Matched '%s' to audio edition id=%s (runtime %.1f min, delta %.1f min)",
                title,
                matched.book_id,
                matched.duration_minutes or 0,
                delta,
            )
        else:
            logger.warning(
                "No confident audio edition match for '%s' (ABS runtime %.1f min, %d candidates)",
                title,
                duration_minutes,
                len(candidates),
            )
        return matched

    def search_book(self, title, author, duration_minutes=0, identifiers=None) -> str | None:
        # With nothing to match an edition on (e.g. an ebook in the library
        # scope), fall back to the top search result.
        if not duration_minutes and not identifiers:
            initial_id = self._find_initial_book_id(title, author)
            if initial_id:
                logger.info("Found '%s' -> id=%s", title, initial_id)
            else:
                logger.warning("No StoryGraph result for '%s'", title)
            return initial_id
        editions = self.load_editions(title, author)
        matched = self.match_audio_edition(editions, title, duration_minutes, identifiers)
        return matched.book_id if matched else None

    def get_book_page(self, book_id) -> str:
        return self._get(f"/books/{book_id}").text

    def parse_current_progress(self, html) -> float | None:
        """The percentage StoryGraph has on file for this book, or None if it
        can't be read — treat that as unknown, never as a reason to skip a write."""
        m = re.search(
            r'(?:name="read_status\[progress_number\]"|class="read-status-progress-number")[^>]*value="([^"]*)"',
            html,
        )
        if not m or not m.group(1):
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    def ensure_status(self, book_id, target_status, html: str | None = None) -> tuple[bool, bool, str | None]:
        """Returns (ok, already_matched, html) — already_matched is True when the book's
        StoryGraph status already matched target_status and nothing was posted. html is
        the page markup the match was read from (reusable for a progress check), or None
        if a status-changing POST was made and any previously-fetched markup is now stale."""
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(r'class="read-status-label"[^>]*>([^<]+)<', html)
        current = m.group(1).strip().lower() if m else ""
        label = _STATUS_LABELS[target_status]
        if label in current or (target_status == "currently-reading" and "rereading" in current):
            return True, True, html
        r = self._post(
            f"/update-status.js?book_id={book_id}&status={target_status}",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set status=%s for %s: HTTP %s", target_status, book_id, r.status_code)
        return r.status_code in (200, 302), False, None

    def update_progress(self, book_id, progress_percent, html: str | None = None) -> bool:
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(
            r'(?:name="read_status\[book_num_of_pages\]"|class="read-status-book-num-of-pages")[^>]*value="([^"]*)"',
            html,
        )
        book_pages = m.group(1) if m else "0"
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
    start_before_finish: set[str] | None = None,
    write_progress: bool = True,
    client: StoryGraphClient | None = None,
    label: str | None = None,
) -> list[dict]:
    label = label or _user_label(get_user(user_id) or {"id": user_id})
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

            # A manual pin is a correction, so it outranks the edition an
            # earlier sync settled on. Dropping prev also defeats the unchanged
            # check below: the pinned edition has none of the old one's progress.
            pinned = _pinned_edition_id(user_id, book.get("abs_item_id"))
            if pinned and prev and prev.get("storygraph_book_id") != pinned:
                logger.info(
                    "[%s] '%s': manually pinned edition %s replaces %s",
                    label, book["title"], pinned, prev.get("storygraph_book_id"),
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

            book_id = (prev.get("storygraph_book_id") if prev else None) or pinned
            if not book_id and book.get("abs_item_id"):
                # Reuse an edition History Import already matched before searching.
                edition = _saved_edition(user_id, book["abs_item_id"])
                if edition:
                    book_id = edition.get("storygraph_book_id")
            if not book_id:
                book_id = client.search_book(
                    book["title"],
                    book["author"],
                    duration_minutes=book.get("duration_minutes", 0),
                    identifiers=book.get("identifiers", []),
                )
            if not book_id:
                results.append({"title": book["title"], "status": "not_found", "reason": "no_confident_audio_edition"})
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
            current_pct = client.parse_current_progress(status_html or "") if already_matched else None
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
    synced = _sync_store.get(user_id)
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
            sync_state = _previous_sync(synced, book) or {}
            storygraph_book_id = sync_state.get("storygraph_book_id")
            if not storygraph_book_id:
                # Retrying can't conjure an edition, so this must not hold the
                # whole daily run back for every other book.
                logger.warning("[%s] Daily history has no matched edition for '%s'; skipping it", label, book["title"])
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
                label=label,
            )
            for book, result in zip(lifecycle_books, lifecycle_results):
                item_id = book.get("abs_item_id")
                if item_id and result["status"] in {"success", "unchanged"}:
                    _mark_handled_lifecycle(scheduler_state, progress_by_item.get(item_id, {}), item_id)
            synced = sum(1 for result in lifecycle_results if result["status"] == "success")
            logger.info("[%s] Auto-sync: %d/%d synced", label, synced, len(lifecycle_books))

        if scheduled_run:
            daily_client = _storygraph_client(user_id)
            status_results = do_sync(
                user_id,
                scoped_books,
                write_progress=False,
                client=daily_client,
                label=label,
            ) if scoped_books else []
            # Judge each book on its own. A book StoryGraph has no audio
            # edition for will never match on a retry, so it is skipped rather
            # than holding back every other book's history and the day itself.
            # Anything else (auth, network, a rejected write) may clear up, so
            # it keeps the day open for the next poll.
            history_books = []
            retry = False
            for book, result in zip(scoped_books, status_results):
                if result["status"] in {"success", "unchanged"}:
                    history_books.append(book)
                elif result["status"] == "not_found":
                    logger.warning(
                        "[%s] Daily sync: no StoryGraph match for '%s'; skipping it",
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
                daily_client,
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
# edits too, for the cost of an mtime check per render.
app.config["TEMPLATES_AUTO_RELOAD"] = True


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
        "auto_sync": not READ_ONLY,
        "read_only": READ_ONLY,
        "poll_interval": POLL_INTERVAL,
        "sync_threshold": SYNC_THRESHOLD,
        "sync_scope": scope,
        "sync_mode": mode,
        "daily_sync_time": cfg(user_id, "DAILY_SYNC_TIME", DEFAULT_DAILY_SYNC_TIME),
        "timezone": cfg(user_id, "TIMEZONE", DEFAULT_TIMEZONE),
        "books": books,
        "last_synced": last,
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
    item = _abs_get(user_id, f"/api/items/{item_id}").json()
    progress_resp = _abs_get(user_id, f"/api/me/progress/{item_id}", required=False)
    return _item_to_book(item, progress_resp.json() if progress_resp is not None else {})


@app.route("/api/history-preview/<item_id>")
@needs_abs_item("ABS_URL", "ABS_TOKEN")
def api_history_preview(item_id):
    user_id = g.user["id"]
    try:
        book = _resolve_abs_book(user_id, item_id)
        if not book:
            return jsonify({"error": "Audiobook metadata was incomplete"}), 422
        sessions = get_abs_listening_sessions(user_id, item_id)
        preview = build_history_preview(sessions, book["duration_minutes"])
        return jsonify({
            "book": {
                "abs_item_id": item_id,
                "title": book["title"],
                "author": book["author"],
                "duration_minutes": book["duration_minutes"],
                "current_minutes": book["current_minutes"],
                "progress_percent": book["progress_percent"],
            },
            **preview,
        })
    except req.RequestException as exc:
        logger.warning("ABS history preview failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502


def _edition_json(candidate) -> dict:
    return {
        "storygraph_book_id": candidate.book_id,
        "title": candidate.title,
        "format": candidate.format,
        "duration_minutes": candidate.duration_minutes,
        "identifier": candidate.identifier,
    }


def _storygraph_page_title(html: str) -> str | None:
    """Book title read from an already-fetched StoryGraph book page."""
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return None
    return re.sub(r"\s*\|\s*The StoryGraph\s*$", "", m.group(1)).strip() or None


def _save_edition(user_id: str, item_id: str, edition: dict, source: str):
    """Pin this ABS item to a StoryGraph edition, for both History Import and
    regular sync. Kept whole (not just the id) so a later preview can show what
    was matched without re-fetching the book page.

    `source` is "manual" when a person chose the edition, which lets it override
    an edition an earlier sync already settled on; an "auto" match only fills a
    gap and never retargets a book that is already syncing somewhere."""
    state = _import_store.get(user_id)
    state.setdefault(item_id, {})["edition"] = {**edition, "source": source}
    _import_store.save(user_id)


def _pinned_edition_id(user_id: str, item_id: str | None) -> str | None:
    """The StoryGraph book id a person explicitly chose for this ABS item — the
    only kind that outranks whatever regular sync last used. An edition saved
    before this field existed counts as "auto": never silently retarget a book
    on a guess about how its edition was picked."""
    if not item_id:
        return None
    edition = _saved_edition(user_id, item_id) or {}
    return edition.get("storygraph_book_id") if edition.get("source") == "manual" else None


@app.route("/api/history-import-preview/<item_id>")
@needs_abs_item("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION")
def api_history_import_preview(item_id):
    user_id = g.user["id"]
    try:
        book = _resolve_abs_book(user_id, item_id)
        if not book:
            return jsonify({"error": "Audiobook metadata was incomplete"}), 422

        sessions = get_abs_listening_sessions(user_id, item_id)
        preview = build_history_preview(sessions, book["duration_minutes"])

        client = _storygraph_client(user_id)
        book_state = _import_store.get(user_id).get(item_id) or {}

        matched_edition = _saved_edition(user_id, item_id)
        candidates = []
        if not matched_edition:
            editions = client.load_editions(book["title"], book["author"])
            matched = client.match_audio_edition(
                editions, book["title"],
                duration_minutes=book["duration_minutes"],
                identifiers=book.get("identifiers", []),
            )
            if matched:
                matched_edition = _edition_json(matched)
                # Persisted so the write route uses exactly this edition, even
                # if a later search would land elsewhere.
                with _user_lock(user_id):
                    _save_edition(user_id, item_id, matched_edition, source="auto")
            else:
                candidates = [_edition_json(c) for c in editions if c.is_audio]

        storygraph_book_id = (matched_edition or {}).get("storygraph_book_id")
        logged_dates = client.get_logged_progress_dates(storygraph_book_id) if storygraph_book_id else set()

        imported_days = book_state.get("imported_days", {})
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
            },
            "matched_edition": matched_edition,
            "candidates": candidates,
            "summary": preview["summary"],
            "days": days,
        })
    except req.RequestException as exc:
        logger.warning("History import preview failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502


@app.route("/api/history-import-edition/<item_id>", methods=["POST"])
@needs_abs_item("STORYGRAPH_SESSION")
def api_history_import_edition(item_id):
    """Manual edition override: pin which StoryGraph book id to use for this ABS
    item, for when auto-matching can't confidently choose one itself. Only ever
    records a choice — never writes any progress, so it stays available in
    read-only development mode."""
    user_id = g.user["id"]
    data = request.json or {}
    match = re.search(r"([0-9a-fA-F-]{36})", (data.get("storygraph_book_id") or "").strip())
    if not match:
        return jsonify({"error": "That doesn't look like a StoryGraph book id or URL"}), 400
    storygraph_book_id = match.group(1)

    try:
        client = _storygraph_client(user_id)
        html = client.get_book_page(storygraph_book_id)
        if not html or "/sign_in" in html[:200]:
            return jsonify({"error": "Could not reach that StoryGraph book"}), 400
    except req.RequestException as exc:
        logger.warning("Manual edition lookup failed for %s: %s", storygraph_book_id, exc)
        return jsonify({"error": "Could not reach StoryGraph"}), 502

    # A book page has the title but not the format or runtime, so those stay
    # unknown for a hand-picked edition.
    with _user_lock(user_id):
        _save_edition(user_id, item_id, {
            "storygraph_book_id": storygraph_book_id,
            "title": _storygraph_page_title(html),
            "format": None,
            "duration_minutes": None,
            "identifier": None,
        }, source="manual")
    return jsonify({"ok": True, "storygraph_book_id": storygraph_book_id})


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

    edition = _saved_edition(user_id, item_id)
    storygraph_book_id = (edition or {}).get("storygraph_book_id")
    if not storygraph_book_id:
        return jsonify({"error": "No StoryGraph edition has been matched or selected for this book yet"}), 400

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
