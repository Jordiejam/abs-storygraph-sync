"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, abort, g, jsonify, make_response, redirect, render_template, request, session, url_for
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
    STORYGRAPH_ID_PATTERN, AudiobookDetails, bare_edition, edition_checks,
    is_strong_match, match_audio_edition, merge_editions, normalise_language, parse_filtered_editions,
    parse_storygraph_editions,
)
from history import build_history_preview, day_key
from journal import journal_entry_ids, parse_journal_page, progress_dates, soup, started_entry_ids

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
USERS_FILE = f"{DATA_DIR}/users.json"
CONFIG_DIR = f"{DATA_DIR}/config"
SYNC_STATE_DIR = f"{DATA_DIR}/sync_state"
IMPORT_STATE_DIR = f"{DATA_DIR}/import_state"
SCHEDULER_STATE_DIR = f"{DATA_DIR}/scheduler_state"
EDITIONS_DIR = f"{DATA_DIR}/editions"

STORYGRAPH_BASE = "https://app.thestorygraph.com"
# What StoryGraph's own pages send with their XHR requests.
_XHR_HEADERS = {
    "Accept": "text/javascript, application/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
# Audio-filter pages read per lookup (ten editions, ~1.5 MB each). Later pages
# are mostly user-added duplicates.
MAX_AUDIO_EDITION_PAGES = 3
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 600))
SYNC_THRESHOLD = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() in {"1", "true", "yes", "on"}

OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

# Fallback for when ProxyFix's forwarded headers get the scheme/host wrong, e.g.
# PUBLIC_URL=https://abs-sync.example.com (no trailing slash).
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")

# The values a setting with fixed choices may take.
SETTING_CHOICES = {
    "SYNC_SCOPE": ("in_progress", "in_progress_finished", "library"),
    "SYNC_MODE": ("frequent", "daily"),
    "AUTO_CONFIRM_EDITIONS": ("on", "off"),
}
# What cfg() returns for a setting a user hasn't saved.
CFG_DEFAULTS = {
    "SYNC_SCOPE": "in_progress",
    "SYNC_MODE": "frequent",
    "DAILY_SYNC_TIME": "00:00",
    "TIMEZONE": "UTC",
    # See _auto_confirm_editions.
    "AUTO_CONFIRM_EDITIONS": "on",
}
# Settings the UI may save; the secret ones are never sent back to it.
SECRET_SETTINGS = ("ABS_TOKEN", "STORYGRAPH_SESSION", "STORYGRAPH_REMEMBER_TOKEN")
SETTINGS = ("ABS_URL", *SECRET_SETTINGS, *CFG_DEFAULTS)

# An Audiobookshelf library item id, as the /api/<thing>/<item_id> routes accept it.
_ITEM_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


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
        self._seq = 0

    def emit(self, record):
        with self._lock:
            # The page tracks new lines by number; a full buffer's length stops changing.
            self._seq += 1
            self._records.append({
                "seq": self._seq,
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            })

    def get(self, since: int = 0) -> tuple[list[dict], int]:
        """The lines numbered after `since`, and the newest line's number."""
        with self._lock:
            return [record for record in self._records if record["seq"] > since], self._seq


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
# One {user_id}.json per store, cached in memory and rewritten whole on save.


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

# {abs_item_id: {"imported_days": {day_key: {percent, imported_at}}}}, saved
# after each verified write so a rerun never imports a day twice.
_import_store = _UserJsonStore(IMPORT_STATE_DIR)

# {"books": {abs_item_id: edition entry}}. See _editions().
_edition_store = _UserJsonStore(EDITIONS_DIR)

# Starts/finishes auto-sync has handled, and the last daily run. Separate from
# _sync_store, which only records writes that reached StoryGraph.
_scheduler_store = _UserJsonStore(SCHEDULER_STATE_DIR)

_user_locks: dict[str, threading.RLock] = {}
_user_locks_guard = threading.Lock()


def _user_lock(user_id: str) -> threading.RLock:
    """Held around anything that mutates a user's (live, shared) store dicts."""
    with _user_locks_guard:
        return _user_locks.setdefault(user_id, threading.RLock())


def cfg(user_id: str, key: str) -> str:
    return _config_store.get(user_id).get(key) or CFG_DEFAULTS.get(key, "")


ABS_KEYS = ("ABS_URL", "ABS_TOKEN")
SYNC_KEYS = (*ABS_KEYS, "STORYGRAPH_SESSION")


def _missing_cfg(user_id: str, keys) -> list[str]:
    return [k for k in keys if not cfg(user_id, k)]


def _missing_cfg_error(user_id: str, keys):
    """The 400 response for a route this user hasn't configured, or None."""
    missing = _missing_cfg(user_id, keys)
    return (jsonify({"error": f"Missing: {', '.join(missing)}"}), 400) if missing else None


def set_cfg(user_id: str, updates: dict):
    _config_store.get(user_id).update({k: v for k, v in updates.items() if v})
    _config_store.save(user_id)


# ── Editions ─────────────────────────────────────────────────────────────────
# An edition entry is {"state", "edition", "candidates", "reason"}, with state:
#   suggested  a confident match nobody has confirmed; never written to
#   unmatched  nothing confident; "candidates" holds the options
#   confirmed  the only state sync and History Import write to. With
#              "auto_confirmed_at" set, auto-sync picked it and nobody has
#              reviewed it yet ("auto" on the pages)
# No entry means never looked up. "auto_declined" marks a book whose automatic
# pick a person undid, so it's never auto-confirmed again.


def _editions(user_id: str) -> dict[str, dict]:
    """This user's live {abs_item_id: entry} map."""
    return _edition_store.get(user_id).setdefault("books", {})


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
        # How its narrator, publisher and language compare with ABS's.
        "checks": edition_checks(candidate, details),
    }


def _bare_edition_json(storygraph_book_id: str, title: str | None = None) -> dict:
    """An edition known only by its id (and maybe its page title): a pasted
    URL, an ABS tag, or one sync wrote to before editions were confirmed."""
    return _edition_json(bare_edition(storygraph_book_id, title or ""))


def _confirm_entry(entry: dict, edition: dict):
    """A person's confirmation, which also counts as reviewing any automatic one."""
    entry.update(state="confirmed", edition=edition)
    entry.setdefault("candidates", [])
    entry.pop("auto_confirmed_at", None)
    entry.pop("auto_declined", None)


def _migrate_legacy_state(user_id: str):
    """Idempotent startup upgrade of older state files; remove once every
    install has run it. Drops sync state keyed by title (those books just sync
    once more), and turns History Import's saved edition (confirmed if picked
    by hand) and the ids sync was writing to (suggested) into edition entries."""
    with _user_lock(user_id):
        synced = _sync_store.get(user_id)
        legacy_keys = [key for key in synced if not _ITEM_ID_RE.fullmatch(key)]
        for key in legacy_keys:
            del synced[key]
        if legacy_keys:
            _sync_store.save(user_id)

        store = _edition_store.get(user_id)
        if "books" in store:
            return
        books = {}
        for item_id, previous in synced.items():
            book_id = (previous or {}).get("storygraph_book_id")
            if book_id:
                books[item_id] = {"state": "suggested", "edition": _bare_edition_json(book_id), "candidates": []}
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


def _edition_entry(user_id: str, item_id: str | None) -> dict:
    return (_editions(user_id).get(item_id) or {}) if item_id else {}


def _confirmed_edition_id(user_id: str, item_id: str | None) -> str | None:
    """The StoryGraph book id confirmed for this ABS item, if any."""
    entry = _edition_entry(user_id, item_id)
    if entry.get("state") != "confirmed":
        return None
    return (entry.get("edition") or {}).get("storygraph_book_id")


def _auto_hold_until(entry: dict) -> float | None:
    """When sync may first write to an auto-confirmed edition: a poll later,
    leaving time to undo a wrong pick."""
    confirmed_at = entry.get("auto_confirmed_at")
    return confirmed_at + POLL_INTERVAL if confirmed_at else None


def _held_until(user_id: str, item_id: str) -> float | None:
    """The end of this book's auto-confirm hold, while it's still on."""
    held_until = _auto_hold_until(_edition_entry(user_id, item_id))
    return held_until if held_until and time.time() < held_until else None


def _edition_state(entry: dict) -> str:
    """The state the pages show, with an unreviewed automatic pick as "auto"."""
    if entry.get("state") == "confirmed" and entry.get("auto_confirmed_at"):
        return "auto"
    return entry.get("state", "unchecked")

# ── ABS ───────────────────────────────────────────────────────────────────────

def _abs_request(method: str, user_id: str, path: str, *, required: bool = True, **kwargs):
    """A request to this user's ABS. `required=False` returns None on a
    non-200 instead of raising."""
    resp = req.request(
        method,
        f"{cfg(user_id, 'ABS_URL')}{path}",
        headers={"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"},
        timeout=10,
        **kwargs,
    )
    if not required and resp.status_code != 200:
        return None
    resp.raise_for_status()
    return resp


def _abs_get(user_id: str, path: str, params: dict | None = None, *, required: bool = True):
    return _abs_request("GET", user_id, path, params=params, required=required)


# ABS has no custom fields, so a book's confirmed edition is kept as a tag.
STORYGRAPH_TAG_PREFIX = "storygraph:"
_STORYGRAPH_TAG_RE = re.compile(rf"{STORYGRAPH_TAG_PREFIX}\s*({STORYGRAPH_ID_PATTERN})", re.IGNORECASE)


def _storygraph_tag_id(tags) -> str | None:
    """The StoryGraph book id from the first storygraph:<id> tag, if any."""
    for tag in tags or []:
        m = _STORYGRAPH_TAG_RE.fullmatch(str(tag).strip())
        if m:
            return m.group(1).lower()
    return None


def write_storygraph_tag(user_id: str, item_id: str, storygraph_book_id: str) -> bool:
    """Tag the ABS book with its edition, replacing any older storygraph: tag.
    False when it was already tagged."""
    tags = (_abs_get(user_id, f"/api/items/{item_id}").json().get("media") or {}).get("tags") or []
    wanted = f"{STORYGRAPH_TAG_PREFIX}{storygraph_book_id}"
    if wanted in tags:
        return False
    kept = [tag for tag in tags if not _storygraph_tag_id([tag])]
    _abs_request("PATCH", user_id, f"/api/items/{item_id}/media", json={"tags": [*kept, wanted]})
    return True


def _item_to_book(item: dict, progress: dict) -> dict | None:
    media = item.get("media", {})
    metadata = media.get("metadata", {})
    title = metadata.get("title", "").strip()
    if not title or not item.get("id"):
        return None
    identifiers = [
        str(metadata[key]).strip()
        for key in ("isbn", "asin")
        if metadata.get(key) and str(metadata[key]).strip()
    ]
    return {
        "abs_item_id": item["id"],
        "title": title,
        "author": metadata.get("authorName", ""),
        "identifiers": identifiers,
        # For telling apart editions that share a runtime; see matcher.
        "narrators": [name.strip() for name in (metadata.get("narratorName") or "").split(",") if name.strip()],
        "publisher": metadata.get("publisher") or None,
        "language": metadata.get("language") or None,
        "storygraph_tag": _storygraph_tag_id(media.get("tags")),
        "progress_percent": round((progress.get("progress") or 0) * 100, 1),
        "current_minutes": _current_minutes(progress),
        "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        "is_finished": bool(progress.get("isFinished")),
        # Epoch milliseconds, for dating a read on StoryGraph; see _read_dates.
        "started_at": progress.get("startedAt"),
        "finished_at": progress.get("finishedAt"),
    }


def _has_started(progress: dict) -> bool:
    return (progress.get("currentTime") or 0) > 0 or bool(progress.get("isFinished"))


def _current_minutes(progress: dict) -> float:
    return round((progress.get("currentTime") or 0) / 60, 1)


def _left_continue_listening(progress: dict) -> bool:
    """What ABS's own in-progress list leaves out."""
    return bool(progress.get("isFinished") or progress.get("hideFromContinueListening"))


def get_abs_progress(user_id: str) -> dict[str, dict]:
    """Every progress record for this ABS user, in one request."""
    me = _abs_get(user_id, "/api/me").json()
    return {
        progress["libraryItemId"]: progress
        for progress in me.get("mediaProgress", [])
        if progress.get("libraryItemId")
    }


def get_abs_book(user_id: str, item_id: str, progress: dict) -> dict | None:
    # Only the expanded item carries media.duration.
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
            if not _has_started(progress):
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

# Reverse-engineered from the site's /update-status.js requests; StoryGraph has
# no public API.
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


def _input_value(html: str, name: str) -> str | None:
    """The value of the named <input>, whatever order its attributes come in."""
    for tag in re.findall(r"<input\b[^>]*>", html):
        if f'name="{name}"' in tag:
            m = re.search(r'\bvalue="([^"]*)"', tag)
            return m.group(1) if m else None
    return None


def _parse_read_status(html: str) -> str:
    """A book page's status label ("currently reading", "read", ...), or "".
    read-status-label is one class token among many utility classes."""
    m = re.search(r'class="(?:[^"]*\s)?read-status-label(?:\s[^"]*)?"[^>]*>([^<]+)<', html)
    return " ".join(m.group(1).split()).lower() if m else ""


def _form_data(html: str, action: str) -> dict | None:
    """What a browser would submit, unchanged, from the form posting to `action`."""
    form = BeautifulSoup(html, "html.parser").find("form", action=action)
    if form is None:
        return None
    data = {}
    for field in form.find_all(["input", "select", "textarea"]):
        name = field.get("name")
        if not name or field.get("type") in {"submit", "button"}:
            continue
        if field.name == "select":
            option = field.find("option", selected=True)
            data[name] = option.get("value", "") if option else ""
        elif field.name == "textarea":
            data[name] = field.get_text()
        else:
            data[name] = field.get("value", "")
    return data


def _date_fields(template: str, value: date_cls) -> dict:
    """A form's day/month/year selects, e.g. template "journal_entry[{}]"."""
    return {template.format(part): str(getattr(value, part)) for part in ("day", "month", "year")}


def _read_date_fields(prefix: str, value: date_cls) -> dict:
    """A read form's day/month/year selects; prefix "start_" for its start."""
    return _date_fields(f"read_instance[{prefix}{{}}]", value)


def _read_form_date(data: dict, prefix: str) -> date_cls | None:
    try:
        return date_cls(*(int(data[f"read_instance[{prefix}{part}]"]) for part in ("year", "month", "day")))
    except (KeyError, ValueError):
        return None


def _status_matches(label: str, target_status: str) -> bool:
    # Exact match: "read" is a substring of "currently reading".
    return label == _STATUS_LABELS[target_status] or (
        target_status == "currently-reading" and "rereading" in label
    )


def _page_has_status(html: str, target_status: str) -> bool:
    """Whether a fetched book page shows the book as target_status."""
    return _status_matches(_parse_read_status(html), target_status)


def _parse_current_progress(html) -> float | None:
    """StoryGraph's percentage for this book, or None if unreadable (unknown,
    never a reason to skip a write)."""
    value = _input_value(html, "read_status[progress_number]")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class StoryGraphAuthError(Exception):
    """StoryGraph bounced a request to its sign-in page."""


STORYGRAPH_SIGNED_OUT = "StoryGraph session invalid — update it in Settings"


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
        self._authed: bool | None = None

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
            headers={**_XHR_HEADERS, "X-CSRF-Token": self._last_csrf, "Referer": STORYGRAPH_BASE},
            allow_redirects=False, timeout=15,
        )

    def check_auth(self) -> bool:
        # Checked once per client, which serves a whole poll.
        if self._authed is None:
            self._authed = "sign_in" not in self._get("/").url
        return self._authed

    @staticmethod
    def _require_signed_in(resp):
        if "sign_in" in resp.url:
            raise StoryGraphAuthError(STORYGRAPH_SIGNED_OUT)
        return resp

    def _find_initial_book_id(self, query) -> str | None:
        """The top search result's book id, or None (logged) if there isn't one."""
        resp = self._require_signed_in(self._get(f"/browse?search_term={req.utils.quote(query)}"))
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
        """The top search result's editions: the first page of all formats
        (the only one listing the edition you've read) plus the audio filter's
        pages, in `language` when known, since page one is mostly print."""
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
        """Pages of the editions page's audio filter (an XHR endpoint answering
        with jQuery; see matcher.parse_filtered_editions)."""
        params = {"book_id": book_id, "format_audio": "true", "commit": "Filter"}
        if language:
            params["languages[]"] = language
        pages, page = [], 1
        while page and page <= MAX_AUDIO_EDITION_PAGES:
            resp = self._session.get(
                f"{STORYGRAPH_BASE}/filter-editions",
                params={**params, "page": page},
                headers={**_XHR_HEADERS, "Referer": f"{STORYGRAPH_BASE}/books/{book_id}/editions"},
                timeout=30,
            )
            resp.raise_for_status()
            editions, next_page = parse_filtered_editions(resp.text)
            pages.append(editions)
            page = next_page if next_page and next_page > page else None
        return pages

    def get_book_page(self, book_id) -> str:
        return self._get_signed_in(f"/books/{book_id}")

    def _get_signed_in(self, path) -> str:
        resp = self._require_signed_in(self._get(path))
        resp.raise_for_status()
        return resp.text

    def read_ids(self, book_id) -> list[str]:
        """This book's finished reads, newest first, from the links on its
        add-a-read page. A read only exists once it's finished."""
        html = self._get_signed_in(f"/read_instances/new?book_id={book_id}")
        return list(dict.fromkeys(re.findall(r"/read_instances/(\d+)/edit", html)))

    def _edit_form(self, path: str, action: str) -> dict | None:
        """The edit form at `path` posting to `action`, as _form_data reads it."""
        data = _form_data(self._get_signed_in(path), action)
        if data is None:
            logger.warning("No edit form for %s", action)
        return data

    def set_read_dates(self, book_id, read_id, started: date_cls | None, finished: date_cls | None) -> bool:
        """Date one read (None leaves that date alone); its journal entries
        move with it. True once the form shows the new dates."""
        path, action = f"/read_instances/{read_id}/edit?book_id={book_id}", f"/read_instances/{read_id}"
        data = self._edit_form(path, action)
        if data is None:
            return False
        changes = {}
        if started:
            changes.update(_read_date_fields("start_", started))
        if finished:
            changes.update(_read_date_fields("", finished))
            # StoryGraph's own start can be later than ABS's finish.
            current_start = _read_form_date({**data, **changes}, "start_")
            if current_start and current_start > finished:
                changes.update(_read_date_fields("start_", finished))
        ok = self._save_form(path, action, data, changes)
        logger.info("Dated read %s of %s (%s to %s): %s", read_id, book_id, started, finished, "ok" if ok else "failed")
        return ok

    def set_journal_entry_date(self, entry_id, value: date_cls) -> bool:
        """Move one journal entry to another day."""
        path, action = f"/journal_entries/{entry_id}/edit", f"/journal_entries/{entry_id}"
        data = self._edit_form(path, action)
        if data is None:
            return False
        changes = _date_fields("journal_entry[{}]", value)
        ok = self._save_form(path, action, data, changes)
        logger.info("Dated journal entry %s to %s: %s", entry_id, value, "ok" if ok else "failed")
        return ok

    def _save_form(self, page_path: str, action: str, data: dict, changes: dict) -> bool:
        """Submit a form with `changes` applied; true once it reads back with them."""
        r = self._session.post(
            f"{STORYGRAPH_BASE}{action}", data={**data, **changes},
            headers={"X-CSRF-Token": self._last_csrf, "Referer": f"{STORYGRAPH_BASE}{page_path}"},
            allow_redirects=False, timeout=15,
        )
        if r.status_code not in (200, 302, 303):
            logger.warning("StoryGraph refused %s: HTTP %s", action, r.status_code)
            return False
        saved = _form_data(self._get_signed_in(page_path), action) or {}
        return all(saved.get(name) == value for name, value in changes.items())

    def latest_finish(self, book_id, read_ids) -> date_cls | None:
        """When the latest of these reads finished, from their edit forms."""
        finishes = []
        for read_id in read_ids:
            path = f"/read_instances/{read_id}/edit?book_id={book_id}"
            finish = _read_form_date(self._edit_form(path, f"/read_instances/{read_id}") or {}, "")
            if finish:
                finishes.append(finish)
        return max(finishes, default=None)

    def _after_earlier_reads(self, book_id, started: date_cls, read_ids) -> date_cls:
        """`started`, moved up to the end of the latest earlier read. ABS keeps
        a relistened book's first startedAt, and a reread dated before the last
        read turns that read's "Started reading" entry into a plain 0% one."""
        latest = self.latest_finish(book_id, read_ids) if read_ids else None
        return max(started, latest) if latest else started

    def start_reading(
        self, book_id, started: date_cls | None = None, html: str | None = None,
    ) -> tuple[bool, bool, str | None]:
        """ensure_status(book_id, "currently-reading"), then move the "Started
        reading" entry StoryGraph dates today to `started`. An undated start
        still counts. `html` is the book page, if the caller has it."""
        html = html if html is not None else self.get_book_page(book_id)
        if not started or _page_has_status(html, "currently-reading"):
            return self.ensure_status(book_id, "currently-reading", html=html)
        started = self._after_earlier_reads(book_id, started, self.read_ids(book_id))
        before = self.started_entry_ids(book_id)
        result = self.ensure_status(book_id, "currently-reading", html=html)
        if result[0]:
            added = self.started_entry_ids(book_id) - before
            if len(added) != 1 or not self.set_journal_entry_date(next(iter(added)), started):
                logger.warning("Set %s to currently reading but couldn't date its start (%d new)", book_id, len(added))
        return result

    def mark_read(
        self, book_id, started: date_cls | None = None, finished: date_cls | None = None,
        html: str | None = None,
    ) -> tuple[bool, bool, str | None]:
        """ensure_status(book_id, "read"), then date the read that adds, which
        StoryGraph would otherwise end today. An undated read still counts."""
        html = html if html is not None else self.get_book_page(book_id)
        if not (started or finished) or _page_has_status(html, "read"):
            return self.ensure_status(book_id, "read", html=html)
        before = set(self.read_ids(book_id))
        if started:
            started = self._after_earlier_reads(book_id, started, before)
            finished = max(finished, started) if finished else None
        result = self.ensure_status(book_id, "read", html=html)
        if result[0]:
            added = [read_id for read_id in self.read_ids(book_id) if read_id not in before]
            if len(added) != 1 or not self.set_read_dates(book_id, added[0], started, finished):
                logger.warning("Marked %s read but couldn't date it (%d new reads)", book_id, len(added))
        return result

    def ensure_status(self, book_id, target_status, html: str | None = None) -> tuple[bool, bool, str | None]:
        """(ok, already_matched, html): already_matched when nothing needed
        posting; html is the book page afterwards, or None if the POST failed."""
        html = html if html is not None else self.get_book_page(book_id)
        current = _parse_read_status(html)
        # Re-posting a book's status isn't a no-op: it wipes its progress, and
        # for "read" adds a second read.
        if _status_matches(current, target_status):
            return True, True, html
        r = self._post(
            f"/update-status.js?book_id={book_id}&status={target_status}",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set status=%s for %s: HTTP %s", target_status, book_id, r.status_code)
        if r.status_code not in (200, 302):
            return False, False, None
        return self._confirm_status(book_id, target_status)

    def _confirm_status(self, book_id, target_status) -> tuple[bool, bool, str]:
        """Re-read the book after a status POST, which StoryGraph can accept
        without making the change."""
        html = self.get_book_page(book_id)
        if _page_has_status(html, target_status):
            return True, False, html
        current = _parse_read_status(html)
        logger.warning(
            "StoryGraph shows %s as '%s' after setting %s", book_id, current or "not on your shelf", target_status,
        )
        return False, False, html

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

    def _journal_pages(self, book_id):
        """Each page of this book's journal, parsed, until one adds no entry."""
        seen: set[str] = set()
        for page in range(1, 51):
            resp = self._get(f"/journal?book_id={book_id}&page={page}")
            if resp.status_code != 200:
                return
            parsed = soup(resp.text)
            ids = set(journal_entry_ids(parsed))
            if not ids - seen:
                return
            seen |= ids
            yield parsed

    def get_logged_progress_dates(self, book_id) -> set[str]:
        """The dates this book already has a progress entry on (see
        journal.progress_dates)."""
        entries = {entry.entry_id: entry for page in self._journal_pages(book_id) for entry in parse_journal_page(page)}
        return progress_dates(entries.values())

    def started_entry_ids(self, book_id) -> set[str]:
        return {entry_id for page in self._journal_pages(book_id) for entry_id in started_entry_ids(page)}

    def add_dated_progress_entry(self, book_id, date: str, percent: float) -> bool:
        """Write one backdated journal entry through the progress form. A
        percentage tolerates a runtime mismatch with ABS. The form is fetched
        first for a fresh CSRF token and its baseline fields."""
        page_html = self._get(f"/progress-update?book_id={book_id}").text

        def _hidden(name: str) -> str:
            m = re.search(rf'(?:name|id)="{re.escape(name)}"[^>]*value="([^"]*)"', page_html)
            return m.group(1) if m else ""

        r = self._post("/update-progress-with-note", {
            **_date_fields("progress_update_date[{}]", date_cls.fromisoformat(date)),
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
# One ABS refetch per user at a time; requests arriving meanwhile share it.
_status_refetch_locks: dict[str, threading.Lock] = {}
STATUS_CACHE_TTL = 60  # seconds


def _cache_books(user_id: str, scope: str, books: list[dict]):
    with _status_cache_lock:
        _status_cache[user_id] = {"scope": scope, "books": books, "ts": time.time()}


def _cached_books(user_id: str, scope: str) -> tuple[list[dict] | None, bool]:
    """(the cached book list for this scope or None, whether it's fresh)."""
    with _status_cache_lock:
        cache = _status_cache.get(user_id)
    if not cache or cache["scope"] != scope:
        return None, False
    return cache["books"], time.time() - cache["ts"] < STATUS_CACHE_TTL


def _fresh_cached_books(user_id: str, scope: str) -> list[dict] | None:
    """The cached book list, or None if it's stale or for another scope."""
    books, fresh = _cached_books(user_id, scope)
    return books if fresh else None


def get_cached_books(user_id: str, scope: str) -> tuple[list[dict], bool]:
    with _status_cache_lock:
        refetch_lock = _status_refetch_locks.setdefault(user_id, threading.Lock())
    with refetch_lock:
        books, fresh = _cached_books(user_id, scope)
        if fresh:
            return books, True
        try:
            books = get_abs_books(user_id, scope)
        except Exception:
            return books or [], False
        _cache_books(user_id, scope, books)
        return books, True


def _target_status(book: dict) -> str:
    if book.get("is_finished"):
        return "read"
    if book["progress_percent"] > 0:
        return "currently-reading"
    return "to-read"


def _count(results: list[dict], status: str) -> int:
    return sum(1 for result in results if result["status"] == status)


def _sync_result(book: dict, status: str) -> dict:
    return {
        "title": book["title"],
        "status": status,
        "progress_percent": book["progress_percent"],
        "current_minutes": book["current_minutes"],
    }


# do_sync statuses for a book StoryGraph now agrees with.
SYNCED_STATUSES = {"success", "unchanged"}


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
    start_before_finish = start_before_finish or set()
    results = []
    for book in books:
        try:
            status = _sync_book(
                user_id, book, client, label,
                start_first=book["abs_item_id"] in start_before_finish, write_progress=write_progress,
            )
            results.append(_sync_result(book, status))
        except Exception as e:
            logger.error("[%s] Error syncing '%s': %s", label, book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    if _count(results, "auth_error"):
        logger.error("[%s] StoryGraph session invalid — update STORYGRAPH_SESSION", label)
    return results


def _sync_book(
    user_id: str, book: dict, client: StoryGraphClient, label: str, *, start_first: bool, write_progress: bool,
) -> str:
    """Bring one book's StoryGraph status and progress in line with ABS, and
    return its do_sync status. `start_first` writes a start before the finish,
    for a book first seen already finished."""
    synced = _sync_store.get(user_id)
    pct = book["progress_percent"]
    status = _target_status(book)
    item_id = book["abs_item_id"]
    prev = synced.get(item_id)

    # A different confirmed edition from the last sync's is a correction: the
    # new one has none of its progress, so prev is dropped.
    book_id = _confirmed_edition_id(user_id, item_id)
    if not book_id:
        logger.info("[%s] '%s' has no confirmed StoryGraph edition — skipping", label, book["title"])
        return "needs_edition"
    if _held_until(user_id, item_id):
        logger.info(
            "[%s] '%s': edition %s was confirmed automatically — holding its first write until the next sync",
            label, book["title"], book_id,
        )
        return "held"
    if prev and prev.get("storygraph_book_id") != book_id:
        if prev.get("storygraph_book_id"):
            logger.info(
                "[%s] '%s': confirmed edition %s replaces %s",
                label, book["title"], book_id, prev.get("storygraph_book_id"),
            )
        prev = None

    if not start_first and prev is not None and prev.get("status") == status and abs(pct - prev.get("pct", -1)) < 0.5:
        logger.info("[%s] '%s' unchanged (%s, %.1f%%) — skipping", label, book["title"], status, pct)
        # StoryGraph agrees, so frequent mode waits another SYNC_THRESHOLD from here.
        if prev.get("current_minutes") != book["current_minutes"]:
            prev["current_minutes"] = book["current_minutes"]
            _sync_store.save(user_id)
        return "unchanged"

    # Only once there's something to write, so an idle run never touches StoryGraph.
    if not client.check_auth():
        return "auth_error"

    page = None
    if status == "read" and start_first:
        started_ok, _, page = client.ensure_status(book_id, "currently-reading")
        if not started_ok:
            return "failed"

    if status == "read":
        ok, already_matched, status_html = client.mark_read(book_id, *_read_dates(user_id, book), html=page)
    elif status == "currently-reading":
        ok, already_matched, status_html = client.start_reading(book_id, _read_dates(user_id, book)[0])
    else:
        ok, already_matched, status_html = client.ensure_status(book_id, status)
    if not ok:
        return "failed"
    if write_progress and not _progress_already_there(status, pct, already_matched, status_html):
        ok = client.update_progress(book_id, 100 if status == "read" else pct, html=status_html)
    if not ok:
        return "failed"
    synced[item_id] = {
        "pct": pct,
        "current_minutes": book["current_minutes"],
        "status": status,
        "storygraph_book_id": book_id,
    }
    _sync_store.save(user_id)
    return "success"


def _progress_already_there(status: str, pct: float, already_matched: bool, status_html: str | None) -> bool:
    """Whether posting progress would be redundant (every POST adds a journal
    entry). A matched "read" is 100%; a matched "currently reading" says
    nothing, so the page's percentage decides, posting if it's unreadable."""
    if status == "to-read":
        return True
    if not already_matched:
        return False
    if status == "read":
        return True
    current_pct = _parse_current_progress(status_html or "")
    return current_pct is not None and abs(current_pct - pct) < 0.5


def _progress_lifecycle(progress: dict) -> tuple[int | str | None, int | str | None]:
    """Stable start/finish tokens, even from ABS responses without timestamps."""
    started = progress.get("startedAt")
    if started is None and _has_started(progress):
        started = "started"
    finished = progress.get("finishedAt")
    if finished is None and progress.get("isFinished"):
        finished = "finished"
    return started, finished


def _lifecycle_changes(progress_by_item: dict[str, dict], state: dict) -> dict[str, dict]:
    """Unhandled starts/finishes. The first snapshot is only a baseline, so an
    upgrade doesn't replay the whole library."""
    if not state.get("initialized"):
        for item_id, progress in progress_by_item.items():
            _mark_handled_lifecycle(state, progress, item_id)
        state["initialized"] = True
        return {}

    handled = state.setdefault("books", {})
    changes = {}
    for item_id, progress in progress_by_item.items():
        started, finished = _progress_lifecycle(progress)
        item_state = handled.get(item_id, {})
        start_changed = started is not None and started != item_state.get("handled_started_at")
        finish_changed = finished is not None and finished != item_state.get("handled_finished_at")
        if start_changed or finish_changed:
            changes[item_id] = {"start": start_changed, "finish": finish_changed}
    return changes


def _mark_handled_lifecycle(state: dict, progress: dict, item_id: str):
    started, finished = _progress_lifecycle(progress)
    item_state = state.setdefault("books", {}).setdefault(item_id, {})
    if started is not None:
        item_state["handled_started_at"] = started
    if finished is not None:
        item_state["handled_finished_at"] = finished
    elif not progress.get("isFinished"):
        item_state["handled_finished_at"] = None


def _parse_daily_sync_time(value: str) -> tuple[int, int]:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour, parsed.minute


def _user_timezone(user_id: str) -> ZoneInfo:
    try:
        return ZoneInfo(cfg(user_id, "TIMEZONE"))
    except ZoneInfoNotFoundError:
        return ZoneInfo(CFG_DEFAULTS["TIMEZONE"])


def _abs_date(user_id: str, epoch_ms) -> date_cls | None:
    """The user's local date of an ABS timestamp, or None if there isn't one."""
    try:
        return datetime.fromtimestamp(float(epoch_ms) / 1000, tz=_user_timezone(user_id)).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _read_dates(user_id: str, book: dict) -> tuple[date_cls | None, date_cls | None]:
    """When ABS says a finished book was started and finished."""
    return _abs_date(user_id, book.get("started_at")), _abs_date(user_id, book.get("finished_at"))


def _daily_schedule(user_id: str, now: datetime | None = None) -> tuple[bool, str]:
    user_timezone = _user_timezone(user_id)
    local_now = (now or datetime.now(timezone.utc)).astimezone(user_timezone)
    try:
        hour, minute = _parse_daily_sync_time(cfg(user_id, "DAILY_SYNC_TIME"))
    except (TypeError, ValueError):
        hour, minute = _parse_daily_sync_time(CFG_DEFAULTS["DAILY_SYNC_TIME"])
    scheduled = datetime.combine(local_now.date(), datetime_time(hour, minute), user_timezone)
    today = local_now.date().isoformat()
    ran_today = _scheduler_store.get(user_id).get("last_daily_run") == today
    due = not ran_today and local_now >= scheduled
    return due, today


def _frequent_sync_candidates(user_id: str, progress_by_item: dict[str, dict], scope: str) -> list[str]:
    """Confirmed books frequent mode should sync: finished, or SYNC_THRESHOLD
    minutes on, since the last sync. Judged from progress records alone, so an
    idle poll fetches no books."""
    synced = _sync_store.get(user_id)
    candidates = []
    for item_id, progress in progress_by_item.items():
        finished = bool(progress.get("isFinished"))
        if scope == "in_progress" and _left_continue_listening(progress):
            continue
        previous = synced.get(item_id) or {}
        if finished:
            due = previous.get("status") != "read"
        else:
            due = _current_minutes(progress) - (previous.get("current_minutes") or 0.0) >= SYNC_THRESHOLD
        if due and _confirmed_edition_id(user_id, item_id):
            candidates.append(item_id)
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


def _history_read_dates(
    user_id: str, book: dict, checkpoints: dict[str, dict], keys,
) -> tuple[date_cls | None, date_cls | None]:
    """ABS's start and finish, widened to cover the days at `keys` (only
    those: a relistened book's history holds earlier listens too)."""
    started, finished = _read_dates(user_id, book)
    days = sorted(date_cls.fromisoformat(checkpoints[key]["date"]) for key in keys if key in checkpoints)
    if days:
        started = min(started or days[0], days[0])
        finished = max(finished or days[-1], days[-1])
    return started, finished


def _finish_imported_book(
    client: StoryGraphClient,
    user_id: str,
    book: dict,
    storygraph_book_id: str,
    checkpoints: dict[str, dict],
    requested: set[str],
    *,
    any_failed: bool,
) -> str:
    """Mark a finished book read, dated to span its imported days. While a day
    has failed it stays currently reading, since any entry on a read book
    starts a new read."""
    if any_failed:
        return "left_currently_reading"
    ok, _, _ = client.mark_read(storygraph_book_id, *_history_read_dates(user_id, book, checkpoints, requested))
    return "marked_read" if ok else "failed"


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
    started: date_cls | None = None,
    book_html: str | None = None,
) -> list[dict]:
    """Write and verify the checkpoints at `requested` (keys, never dates or
    percentages), for History Import and daily sync alike. With
    ensure_read_status the book is first set to currently reading, dated
    `started`, since StoryGraph only keeps dated entries for a read in
    progress. `book_html` is the book page, if the caller has it."""
    if ensure_read_status:
        status_ok, _, _ = client.start_reading(storygraph_book_id, started, html=book_html)
        if not status_ok:
            raise HistoryReconcileError(
                "Could not set this book to 'currently reading' on StoryGraph, "
                "which a dated entry needs to attach to"
            )

    item_state = _import_store.get(user_id).setdefault(item_id, {})
    imported_days = item_state.setdefault("imported_days", {})
    # Daily sync retries every poll, so skip the journal when there's nothing new.
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

    # The form can report success without saving; only the journal counts.
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


def _write_history_days(
    user_id: str,
    book: dict,
    storygraph_book_id: str,
    client: StoryGraphClient,
    label: str,
    kind: str,
    start_date: date_cls,
    end_date: date_cls,
    **reconcile_options,
) -> bool:
    """Write this book's listening days in the range; False if any failed."""
    try:
        checkpoints = _history_checkpoints(
            get_abs_listening_sessions(user_id, book["abs_item_id"]),
            book.get("duration_minutes", 0),
            start_date,
            end_date,
        )
        if not checkpoints:
            return True
        results = _reconcile_history_checkpoints(
            user_id, book["abs_item_id"], storygraph_book_id, checkpoints, set(checkpoints), client, label,
            **reconcile_options,
        )
    except (req.RequestException, ValueError, HistoryReconcileError, StoryGraphAuthError) as exc:
        logger.warning("[%s] %s history failed for '%s': %s", label, kind, book["title"], exc)
        return False
    logger.info(
        "[%s] %s history for '%s' (%s to %s): %d/%d days written",
        label, kind, book["title"], start_date, end_date, _count(results, "success"), len(results),
    )
    return not _count(results, "failed")


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
        if book.get("current_minutes", 0) <= 0:
            continue
        storygraph_book_id = _confirmed_edition_id(user_id, book["abs_item_id"])
        if not storygraph_book_id:
            # Retrying won't confirm it, so don't hold back the rest.
            logger.warning("[%s] Daily history has no confirmed edition for '%s'; skipping it", label, book["title"])
            continue
        if not _write_history_days(
            user_id, book, storygraph_book_id, client, label, "Daily", start_date, end_date,
            ensure_read_status=False,
        ):
            all_ok = False
    return all_ok


def _finish_daily_history(
    user_id: str,
    book: dict,
    client: StoryGraphClient,
    label: str,
    start_date: date_cls,
    end_date: date_cls,
) -> bool:
    """Write a just-finished book's last listening days before it's marked
    read, after which StoryGraph won't take them. False retries next poll."""
    storygraph_book_id = _confirmed_edition_id(user_id, book["abs_item_id"])
    if not storygraph_book_id:
        return True  # do_sync reports it as needing an edition
    if _held_until(user_id, book["abs_item_id"]):
        # do_sync holds it too, so the finish stays pending until the hold ends.
        return True
    try:
        html = client.get_book_page(storygraph_book_id)
    except (req.RequestException, StoryGraphAuthError) as exc:
        logger.warning("[%s] Finish history failed for '%s': %s", label, book["title"], exc)
        return False
    if _page_has_status(html, "read"):
        return True  # already finished there; more entries would start a reread
    return _write_history_days(
        user_id, book, storygraph_book_id, client, label, "Finish", start_date, end_date,
        started=_read_dates(user_id, book)[0], book_html=html,
    )


def _pending_lifecycle_changes(user_id: str, progress_by_item: dict[str, dict], state: dict) -> dict[str, dict]:
    """Starts and finishes still to write, for confirmed books only (the rest
    wait until they are)."""
    changes = _lifecycle_changes(progress_by_item, state)
    synced = _sync_store.get(user_id)
    for item_id, progress in progress_by_item.items():
        # A reopened finished book keeps its old startedAt, so the last synced
        # status is what shows it restarted.
        if (
            _has_started(progress)
            and not progress.get("isFinished")
            and (synced.get(item_id) or {}).get("status") == "read"
        ):
            changes.setdefault(item_id, {"start": False, "finish": False})["start"] = True
    return {item_id: change for item_id, change in changes.items() if _confirmed_edition_id(user_id, item_id)}


def _lifecycle_books(
    user_id: str,
    mode: str,
    changes: dict[str, dict],
    progress_by_item: dict[str, dict],
    scoped_books: list[dict],
) -> list[dict]:
    """The books to sync this poll outside the daily run: lifecycle changes
    plus frequent mode's candidates, reusing the daily run's copies."""
    wanted = list(changes)
    if mode == "frequent":
        scope = cfg(user_id, "SYNC_SCOPE")
        wanted += [item_id for item_id in _frequent_sync_candidates(user_id, progress_by_item, scope)
                   if item_id not in changes]
    scoped = {book["abs_item_id"]: book for book in scoped_books}
    books = []
    for item_id in wanted:
        book = scoped.get(item_id) or get_abs_book(user_id, item_id, progress_by_item[item_id])
        if book:
            books.append(book)
    return books


def _sync_lifecycle(
    user_id: str,
    label: str,
    mode: str,
    books: list[dict],
    changes: dict[str, dict],
    progress_by_item: dict[str, dict],
    scheduler_state: dict,
    local_date: str,
    client: StoryGraphClient,
):
    if mode == "daily":
        # A finish writes its own last days; the daily run skips finished books.
        history_start, _ = _daily_history_range(scheduler_state, local_date)
        books = [
            book for book in books
            if not (book["is_finished"] and changes.get(book["abs_item_id"], {}).get("finish"))
            or _finish_daily_history(
                user_id, book, client, label, history_start, date_cls.fromisoformat(local_date),
            )
        ]
    if not books:
        return
    start_before_finish = {item_id for item_id, change in changes.items() if change["start"] and change["finish"]}
    results = do_sync(user_id, books, start_before_finish=start_before_finish, client=client, label=label)
    for book, result in zip(books, results):
        if result["status"] in SYNCED_STATUSES:
            item_id = book["abs_item_id"]
            _mark_handled_lifecycle(scheduler_state, progress_by_item.get(item_id, {}), item_id)
    logger.info("[%s] Auto-sync: %d/%d synced", label, _count(results, "success"), len(books))


def _run_daily(
    user_id: str,
    label: str,
    scoped_books: list[dict],
    scheduler_state: dict,
    local_date: str,
    client: StoryGraphClient,
):
    """Every book's status, then its listening days since the last run. The
    day only counts as run once all of it is in."""
    status_results = do_sync(
        user_id,
        scoped_books,
        write_progress=False,
        client=client,
        label=label,
    ) if scoped_books else []
    # A book with no confirmed edition won't gain one on a retry, so it's
    # skipped; any other failure may clear up, so it keeps the day open.
    history_books = []
    retry = False
    for book, result in zip(scoped_books, status_results):
        if book["is_finished"]:
            # Its finish wrote its last days; more would start another read.
            continue
        if result["status"] in SYNCED_STATUSES:
            history_books.append(book)
        elif result["status"] == "needs_edition":
            logger.warning(
                "[%s] Daily sync: no confirmed StoryGraph edition for '%s'; skipping it",
                label, book["title"],
            )
        else:
            retry = True
    start_date, end_date = _daily_history_range(scheduler_state, local_date)
    history_ok = _daily_history_sync(user_id, history_books, start_date, end_date, client, label)
    if history_ok and not retry:
        scheduler_state["last_daily_run"] = local_date
    else:
        logger.warning(
            "[%s] Daily history incomplete; it will retry without duplicating verified days",
            label,
        )


AUTO_LOOKUPS_PER_POLL = 3


def _auto_confirm_editions(user_id: str, label: str, progress_by_item: dict[str, dict], client: StoryGraphClient):
    """Look up books being listened to that never were (a few per poll), and
    confirm strong suggestions (matcher.is_strong_match). An automatic pick
    isn't tagged in ABS until a person keeps it, and isn't written to until
    the next poll (_auto_hold_until)."""
    if cfg(user_id, "AUTO_CONFIRM_EDITIONS") != "on":
        return
    editions = _editions(user_id)
    lookups = 0
    for item_id, progress in progress_by_item.items():
        if _left_continue_listening(progress) or not _has_started(progress):
            continue
        if item_id not in editions:
            if lookups >= AUTO_LOOKUPS_PER_POLL:
                continue
            lookups += 1
            try:
                book = get_abs_book(user_id, item_id, progress)
                if not book:
                    continue
                _look_up_edition(user_id, client, book)
            except StoryGraphAuthError as exc:
                logger.warning("[%s] Auto-confirm: %s", label, exc)
                return
            except req.RequestException as exc:
                logger.warning("[%s] Auto-confirm: could not look up an edition for %s: %s", label, item_id, exc)
                continue
        entry = editions[item_id]
        edition = entry.get("edition") or {}
        if (
            entry.get("state") == "suggested"
            and not entry.get("auto_declined")
            and is_strong_match(entry.get("reason"), edition.get("checks"))
        ):
            _confirm_entry(entry, edition)
            entry["auto_confirmed_at"] = time.time()
            _edition_store.save(user_id)
            logger.info(
                "[%s] Auto-confirmed StoryGraph edition %s for %s (%s match); nothing is written to it until the next sync",
                label, edition.get("storygraph_book_id"), item_id, (entry.get("reason") or {}).get("code"),
            )


def _poll_user(user: dict, now: datetime | None = None):
    user_id = user["id"]
    if _missing_cfg(user_id, SYNC_KEYS):
        return
    mode = cfg(user_id, "SYNC_MODE")
    label = _user_label(user)
    try:
        progress_by_item = get_abs_progress(user_id)
        client = _storygraph_client(user_id)
        _auto_confirm_editions(user_id, label, progress_by_item, client)
        scheduler_state = _scheduler_store.get(user_id)
        was_initialized = bool(scheduler_state.get("initialized"))
        changes = _pending_lifecycle_changes(user_id, progress_by_item, scheduler_state)
        if not was_initialized:
            _scheduler_store.save(user_id)

        daily_due, local_date = _daily_schedule(user_id, now)
        scheduled_run = mode == "daily" and daily_due
        scoped_books = []
        if scheduled_run:
            scope = cfg(user_id, "SYNC_SCOPE")
            scoped_books = get_abs_books(user_id, scope, progress_by_item=progress_by_item)
            _cache_books(user_id, scope, scoped_books)

        books = _lifecycle_books(user_id, mode, changes, progress_by_item, scoped_books)
        _sync_lifecycle(
            user_id, label, mode, books, changes, progress_by_item, scheduler_state, local_date, client,
        )
        if scheduled_run:
            _run_daily(user_id, label, scoped_books, scheduler_state, local_date, client)
        if changes or scheduled_run:
            _scheduler_store.save(user_id)
    except Exception as e:
        logger.error("[%s] Auto-sync error: %s", label, e)


def _migrate_all_users():
    for user in list_users():
        _migrate_legacy_state(user["id"])


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
# Reload edited templates too in dev, which is the only read-only setup.
app.config["TEMPLATES_AUTO_RELOAD"] = READ_ONLY


@app.context_processor
def _asset_url():
    """url_for('static') with the file's mtime, so an update busts the cache."""
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
    """For /api/<thing>/<item_id> routes: refuse writes in read-only mode, a
    malformed item id, or a half-configured account."""
    def decorator(view):
        @wraps(view)
        def wrapped(item_id, *args, **kwargs):
            if writes and READ_ONLY:
                return jsonify({"error": "This is disabled in read-only development mode"}), 403
            if not _ITEM_ID_RE.fullmatch(item_id):
                return jsonify({"error": "Invalid Audiobookshelf item ID"}), 400
            return _missing_cfg_error(g.user["id"], required_cfg) or view(item_id, *args, **kwargs)
        return wrapped
    return decorator


@app.errorhandler(StoryGraphAuthError)
def _storygraph_auth_error(exc):
    return jsonify({"error": str(exc)}), 401


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


@app.context_processor
def _signed_in_user():
    user = getattr(g, "user", None)
    if not user:
        return {}
    return {
        "is_admin": bool(user.get("is_admin")),
        "display_name": user.get("display_name") or user.get("username"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/editions")
def editions_page():
    return render_template("editions.html")


@app.route("/api/status")
def api_status():
    user_id = g.user["id"]
    scope = cfg(user_id, "SYNC_SCOPE")
    books, abs_ok = [], False
    if not _missing_cfg(user_id, ABS_KEYS):
        books, abs_ok = get_cached_books(user_id, scope)
    synced = _sync_store.get(user_id)
    return jsonify({
        "abs_ok": abs_ok,
        "sg_ok": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "read_only": READ_ONLY,
        "poll_interval": POLL_INTERVAL,
        "sync_scope": scope,
        "sync_mode": cfg(user_id, "SYNC_MODE"),
        "daily_sync_time": cfg(user_id, "DAILY_SYNC_TIME"),
        "books": [{
            **book,
            "last_synced_minutes": (synced.get(book["abs_item_id"]) or {}).get("current_minutes"),
            "edition_state": _edition_state(_edition_entry(user_id, book["abs_item_id"])),
        } for book in books],
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    user_id = g.user["id"]
    if READ_ONLY:
        return jsonify({"error": "Sync is disabled in read-only development mode"}), 403
    if error := _missing_cfg_error(user_id, SYNC_KEYS):
        return error
    scope = cfg(user_id, "SYNC_SCOPE")
    label = _user_label(g.user)
    try:
        books = get_abs_books(user_id, scope)
        if not books:
            return jsonify({"message": "No books found", "synced": 0, "total": 0, "results": []})
        with _user_lock(user_id):
            results = do_sync(user_id, books, label=label)
        return jsonify({"message": "Sync complete", "synced": _count(results, "success"), "total": len(books), "results": results})
    except Exception as e:
        logger.error("[%s] Sync failed: %s", label, e)
        return jsonify({"error": str(e)}), 500


def _resolve_abs_book(user_id: str, item_id: str) -> dict | None:
    """A book's ABS metadata, from the status cache or a fetch of just this item."""
    books = _fresh_cached_books(user_id, cfg(user_id, "SYNC_SCOPE")) or []
    book = next((candidate for candidate in books if candidate.get("abs_item_id") == item_id), None)
    if book:
        return book
    progress_resp = _abs_get(user_id, f"/api/me/progress/{item_id}", required=False)
    return get_abs_book(user_id, item_id, progress_resp.json() if progress_resp is not None else {})


def _require_abs_book(user_id: str, item_id: str) -> dict:
    """_resolve_abs_book, or a 422 when the metadata is too incomplete."""
    book = _resolve_abs_book(user_id, item_id)
    if not book:
        abort(make_response(jsonify({"error": "Audiobook metadata was incomplete"}), 422))
    return book


def _tagged_edition_title(client: StoryGraphClient, book_id: str) -> str:
    """A tagged edition's page title, or "" (only a label, so never fatal)."""
    try:
        return _storygraph_page_title(client.get_book_page(book_id)) or ""
    except req.RequestException as exc:
        logger.warning("Could not read the tagged StoryGraph edition %s: %s", book_id, exc)
        return ""


def _storygraph_page_title(html: str) -> str | None:
    """Book title read from an already-fetched StoryGraph book page."""
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return None
    return re.sub(r"\s*\|\s*The StoryGraph\s*$", "", m.group(1)).strip() or None


def _look_up_edition(user_id: str, client: StoryGraphClient, book: dict, query: str = "") -> dict:
    """Search StoryGraph for this book and record a suggestion or candidates.
    Never confirms, or unconfirms: a confirmed book only gets new candidates."""
    search = query or f"{book['title']} {book['author']}".strip()
    editions = client.load_editions(search, normalise_language(book.get("language")) or None)
    tagged_id = book.get("storygraph_tag")
    tagged_unlisted = bool(tagged_id) and all(candidate.book_id != tagged_id for candidate in editions)
    if tagged_unlisted:
        # Still a person's pick, so offer it with the title from its page.
        editions.append(bare_edition(tagged_id, _tagged_edition_title(client, tagged_id)))
    details = _audiobook_details(book)
    duration = book.get("duration_minutes", 0)
    matched, reason = match_audio_edition(
        editions,
        target_duration_minutes=duration,
        identifiers=book.get("identifiers", []),
        details=details,
        tagged_id=tagged_id,
    )
    if matched:
        logger.info(
            "Matched '%s' to audio edition id=%s by %s (runtime %.1f min vs ABS %.1f min)",
            book["title"], matched.book_id, reason["code"], matched.duration_minutes or 0, duration,
        )
    else:
        logger.warning(
            "No confident audio edition match for '%s': %s (ABS runtime %.1f min, %d candidates)",
            book["title"], reason["code"], duration, len(editions),
        )
    if tagged_unlisted:
        reason["tagged_unlisted"] = True
    audio = [candidate for candidate in editions if candidate.is_audio]
    read = next((candidate for candidate in editions if candidate.read_by_you), None)
    if not matched and read:
        # Nothing matched as audio, so suggest the edition you've read.
        matched = read
        reason["read_edition"]["fallback"] = True
    offered = _offered_candidates(audio or editions, book, details, keep=(matched, read))
    with _user_lock(user_id):
        entry = _editions(user_id).setdefault(book["abs_item_id"], {})
        entry["candidates"] = [_edition_json(candidate, details) for candidate in offered]
        # Shown so a "no match" isn't a mystery.
        entry["reason"] = {**reason, "query": search, "abs_runtime_minutes": duration}
        if entry.get("state") != "confirmed":
            entry["state"] = "suggested" if matched else "unmatched"
            entry["edition"] = _edition_json(matched, details) if matched else None
        _edition_store.save(user_id)
        return dict(entry)


MAX_OFFERED_EDITIONS = 12


def _offered_candidates(candidates: list, book: dict, details: AudiobookDetails, keep) -> list:
    """The closest few editions (language, then runtime), plus those in `keep`."""
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


def _entry_fields(entry: dict) -> dict:
    """An edition entry as every page is sent it."""
    return {
        "state": _edition_state(entry),
        "edition": entry.get("edition"),
        "candidates": entry.get("candidates", []),
        "held_until": _auto_hold_until(entry),
        "reason": entry.get("reason"),
    }


def _edition_row(user_id: str, book: dict, entry: dict) -> dict:
    """One ABS book and its edition entry, as the Editions page shows it."""
    synced = _sync_store.get(user_id).get(book["abs_item_id"]) or {}
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
        **_entry_fields(entry),
        # So the page can warn that confirming another edition strands this progress.
        "synced_book_id": synced.get("storygraph_book_id"),
    }


@app.route("/api/history-import-preview/<item_id>")
@needs_abs_item(*ABS_KEYS)
def api_history_import_preview(item_id):
    """This book's day-by-day ABS history, with its edition and what's already
    on StoryGraph when a session is set."""
    user_id = g.user["id"]
    try:
        book = _require_abs_book(user_id, item_id)

        sessions = get_abs_listening_sessions(user_id, item_id)
        preview = build_history_preview(sessions, book["duration_minutes"])

        storygraph_ready = bool(cfg(user_id, "STORYGRAPH_SESSION"))
        entry = {}
        logged_dates = set()
        if storygraph_ready:
            client = _storygraph_client(user_id)
            try:
                entry = _edition_entry(user_id, item_id)
                # Opening History counts as asking for a lookup.
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
                "title": book["title"],
                "author": book["author"],
                "duration_minutes": book["duration_minutes"],
                "identifiers": book.get("identifiers", []),
                "storygraph_tag": book.get("storygraph_tag"),
            },
            "storygraph_ready": storygraph_ready,
            **_entry_fields(entry),
            "summary": preview["summary"],
            "days": days,
        })
    except req.RequestException as exc:
        logger.warning("History import preview failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502


@app.route("/api/history-import/<item_id>", methods=["POST"])
@needs_abs_item(*SYNC_KEYS, writes=True)
def api_history_import(item_id):
    """Write one dated journal entry per chosen day not already there. The
    body only names days by key (history.day_key); every date and percentage
    is rebuilt from ABS, so a request can't invent one, and a key that no
    longer matches is reported as a stale preview."""
    user_id = g.user["id"]
    body = request.json or {}
    days = body.get("days")
    requested = {k for k in days if isinstance(k, str)} if isinstance(days, list) else set()
    if not requested:
        return jsonify({"error": "No days were confirmed for import"}), 400
    allow_reread = body.get("allow_reread") is True

    storygraph_book_id = _confirmed_edition_id(user_id, item_id)
    if not storygraph_book_id:
        return jsonify({"error": "Confirm a StoryGraph edition for this book before importing"}), 400
    # Imports can't be undone, so an automatic pick must be kept first.
    if _edition_entry(user_id, item_id).get("auto_confirmed_at"):
        return jsonify({"error": "Review the automatically confirmed edition on the Editions page before importing"}), 400

    try:
        book = _require_abs_book(user_id, item_id)
        sessions = get_abs_listening_sessions(user_id, item_id)
    except req.RequestException as exc:
        logger.warning("History import could not re-read ABS for %s: %s", item_id, exc)
        return jsonify({"error": "Could not load listening history from Audiobookshelf"}), 502
    checkpoints = _history_checkpoints(sessions, book["duration_minutes"])

    label = _user_label(g.user)
    try:
        client = _storygraph_client(user_id)
        if not client.check_auth():
            raise StoryGraphAuthError(STORYGRAPH_SIGNED_OUT)
        with _user_lock(user_id):
            # Entries on a read book start a new read, so ask first.
            html = None if allow_reread else client.get_book_page(storygraph_book_id)
            if html is not None and _page_has_status(html, "read"):
                return jsonify({
                    "error": "This book is already marked read on StoryGraph",
                    "already_read": True,
                }), 409
            results = _reconcile_history_checkpoints(
                user_id,
                item_id,
                storygraph_book_id,
                checkpoints,
                requested,
                client,
                label,
                started=_history_read_dates(user_id, book, checkpoints, requested)[0],
                book_html=html,
            )
            finish = (
                _finish_imported_book(client, user_id, book, storygraph_book_id, checkpoints, requested,
                                      any_failed=bool(_count(results, "failed")))
                if book["is_finished"] else None
            )
    except HistoryReconcileError as exc:
        return jsonify({"error": str(exc)}), 502
    except req.RequestException as exc:
        logger.warning("History import failed to reach StoryGraph for %s: %s", item_id, exc)
        return jsonify({"error": "Could not reach StoryGraph"}), 502
    imported = _count(results, "success")
    logger.info("[%s] History import for %s: %d/%d days written", label, item_id, imported, len(results))
    return jsonify({"imported": imported, "total": len(results), "results": results, "finish": finish})


def _whole_library(user_id: str):
    """(the whole ABS library, None), or (None, an error response)."""
    if error := _missing_cfg_error(user_id, ABS_KEYS):
        return None, error
    try:
        return get_abs_books(user_id, "library"), None
    except req.RequestException as exc:
        logger.warning("[%s] Could not read the ABS library: %s", _user_label(g.user), exc)
        return None, (jsonify({"error": "Could not load your library from Audiobookshelf"}), 502)


@app.route("/api/editions")
def api_editions():
    """Every ABS book, whatever the sync scope, with its edition entry."""
    user_id = g.user["id"]
    books, error = _whole_library(user_id)
    if error:
        return error
    editions = _editions(user_id)
    return jsonify({
        "storygraph_ready": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "books": [_edition_row(user_id, book, editions.get(book["abs_item_id"]) or {}) for book in books],
    })


@app.route("/api/editions/<item_id>/lookup", methods=["POST"])
@needs_abs_item(*SYNC_KEYS)
def api_edition_lookup(item_id):
    """Search StoryGraph for one book, optionally with a person's own words.
    Only records a suggestion, so it works in read-only mode."""
    user_id = g.user["id"]
    query = str((request.get_json(silent=True) or {}).get("query") or "").strip()[:200]
    try:
        book = _require_abs_book(user_id, item_id)
        entry = _look_up_edition(user_id, _storygraph_client(user_id), book, query)
    except req.RequestException as exc:
        logger.warning("Edition lookup failed for %s: %s", item_id, exc)
        return jsonify({"error": "Could not reach StoryGraph or Audiobookshelf"}), 502
    return jsonify(_edition_row(user_id, book, entry))


@app.route("/api/editions/<item_id>/confirm", methods=["POST"])
@needs_abs_item("STORYGRAPH_SESSION")
def api_edition_confirm(item_id):
    """Confirm an edition by id or pasted URL, and tag it in ABS. Writes no progress."""
    user_id = g.user["id"]
    data = request.get_json(silent=True) or {}
    match = re.search(f"({STORYGRAPH_ID_PATTERN})", str(data.get("storygraph_book_id") or "").strip())
    if not match:
        return jsonify({"error": "That doesn't look like a StoryGraph book id or URL"}), 400
    storygraph_book_id = match.group(1)

    edition = _known_edition(_edition_entry(user_id, item_id), storygraph_book_id)
    if edition is None:
        try:
            html = _storygraph_client(user_id).get_book_page(storygraph_book_id)
        except req.HTTPError:
            return jsonify({"error": "Could not reach that StoryGraph book"}), 400
        except req.RequestException as exc:
            logger.warning("Edition lookup failed for %s: %s", storygraph_book_id, exc)
            return jsonify({"error": "Could not reach StoryGraph"}), 502
        # A book page only gives the title.
        edition = _bare_edition_json(storygraph_book_id, _storygraph_page_title(html))

    with _user_lock(user_id):
        entry = _editions(user_id).setdefault(item_id, {})
        _confirm_entry(entry, edition)
        _edition_store.save(user_id)
    logger.info("[%s] Confirmed StoryGraph edition %s for %s", _user_label(g.user), storygraph_book_id, item_id)
    can_tag = not READ_ONLY and not _missing_cfg(user_id, ABS_KEYS)
    return jsonify({
        "ok": True, **_entry_fields(entry),
        "tag_error": _tag_in_abs(user_id, item_id, storygraph_book_id) if can_tag else None,
    })


@app.route("/api/editions/<item_id>/undo-auto", methods=["POST"])
@needs_abs_item()
def api_edition_undo_auto(item_id):
    """Turn an automatic pick back into a suggestion, for good. Progress
    already written to it stays on StoryGraph."""
    user_id = g.user["id"]
    with _user_lock(user_id):
        entry = _edition_entry(user_id, item_id)
        if not entry.get("auto_confirmed_at"):
            return jsonify({"error": "This edition wasn't confirmed automatically"}), 409
        entry["state"] = "suggested"
        entry["auto_declined"] = True
        entry.pop("auto_confirmed_at", None)
        _edition_store.save(user_id)
    logger.info("[%s] Undid the automatic StoryGraph edition for %s", _user_label(g.user), item_id)
    return jsonify({"ok": True, **_entry_fields(entry)})


def _known_edition(entry: dict, storygraph_book_id: str) -> dict | None:
    """This edition from the book's suggestion or candidates, if known with a
    title, so confirming it needs no fetch."""
    known = [entry.get("edition"), *entry.get("candidates", [])]
    return next(
        (dict(e) for e in known if e and e.get("storygraph_book_id") == storygraph_book_id and e.get("title")),
        None,
    )


TAG_FORBIDDEN = "Your Audiobookshelf user isn't allowed to update books, so editions can't be tagged there"


def _tag_in_abs(user_id: str, item_id: str, storygraph_book_id: str) -> str | None:
    """write_storygraph_tag, logged; returns why it failed, if it did."""
    try:
        if write_storygraph_tag(user_id, item_id, storygraph_book_id):
            logger.info("Tagged ABS item %s with %s%s", item_id, STORYGRAPH_TAG_PREFIX, storygraph_book_id)
    except req.RequestException as exc:
        logger.warning("Could not tag ABS item %s with its StoryGraph edition: %s", item_id, exc)
        if getattr(exc.response, "status_code", None) == 403:
            return TAG_FORBIDDEN
        return "Could not tag the edition in Audiobookshelf"
    return None


@app.route("/api/editions/sync-tags", methods=["POST"])
def api_edition_tag_sync():
    """Line up confirmed editions and ABS tags both ways: a tag confirms an
    unconfirmed (or automatic) edition, a person's untagged pick gets tagged,
    and a disagreement is only reported, since each was somebody's pick."""
    user_id = g.user["id"]
    books, error = _whole_library(user_id)
    if error:
        return error

    confirmed, to_tag, conflicts = [], [], []
    with _user_lock(user_id):
        editions = _editions(user_id)
        for book in books:
            item_id, tagged_id = book["abs_item_id"], book.get("storygraph_tag")
            confirmed_id = _confirmed_edition_id(user_id, item_id)
            # An automatic pick isn't anybody's, so a tag overrides it.
            auto = bool(_edition_entry(user_id, item_id).get("auto_confirmed_at"))
            if tagged_id and (not confirmed_id or (auto and tagged_id != confirmed_id)):
                entry = editions.setdefault(item_id, {})
                # A tag is only an id; keep any details a lookup found.
                _confirm_entry(entry, _known_edition(entry, tagged_id) or _bare_edition_json(tagged_id))
                confirmed.append(item_id)
            elif confirmed_id and not tagged_id and not auto:
                to_tag.append((item_id, confirmed_id))
            elif tagged_id and tagged_id != confirmed_id:
                conflicts.append(book["title"])
        if confirmed:
            _edition_store.save(user_id)

    tagged, tag_error = 0, None
    for item_id, storygraph_book_id in ([] if READ_ONLY else to_tag):
        error = _tag_in_abs(user_id, item_id, storygraph_book_id)
        if error is None:
            tagged += 1
            continue
        tag_error = error
        if error == TAG_FORBIDDEN:
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
    logs, latest = _log_buffer.get(request.args.get("since", 0, type=int))
    return jsonify({"logs": logs, "latest": latest})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    user_id = g.user["id"]
    data = request.json or {}
    if "ABS_URL" in data and data["ABS_URL"]:
        parsed = urlparse(data["ABS_URL"])
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "ABS_URL must use http or https"}), 400
        if not parsed.hostname:
            return jsonify({"error": "ABS_URL must include a hostname"}), 400
    for key, allowed in SETTING_CHOICES.items():
        if data.get(key) and data[key] not in allowed:
            return jsonify({"error": f"Invalid {key}"}), 400
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
    set_cfg(user_id, {k: v for k, v in data.items() if k in SETTINGS})
    with _status_cache_lock:
        _status_cache.pop(user_id, None)
    logger.info("[%s] Settings updated via UI", _user_label(g.user))
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """This user's settings, with secrets only shown as set or not."""
    user_id = g.user["id"]
    return jsonify({
        key: ("set" if cfg(user_id, key) else "") if key in SECRET_SETTINGS else cfg(user_id, key)
        for key in SETTINGS
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


# Before any request or poll, under `flask run` too, and last, so all it uses exists.
_migrate_all_users()

if __name__ == "__main__":
    if not READ_ONLY:
        threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
