"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, jsonify, request, render_template, session, redirect, url_for, g
from urllib.parse import urlparse
from functools import wraps
import os, re, json, logging, threading, time, uuid
from collections import deque
from datetime import datetime, timezone, time as datetime_time
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
DEFAULT_DAILY_SYNC_TIME = "00:00"
DEFAULT_TIMEZONE = "UTC"


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
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


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
            data = self._cache.get(user_id, {})
            os.makedirs(self._dir, exist_ok=True)
            with open(self._path(user_id), "w") as f:
                json.dump(data, f, indent=2)


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


def cfg(user_id: str, key: str, default: str = "") -> str:
    return _config_store.get(user_id).get(key) or default


def set_cfg(user_id: str, updates: dict):
    _config_store.get(user_id).update({k: v for k, v in updates.items() if v})
    _config_store.save(user_id)


def _saved_edition(user_id: str, item_id: str) -> dict | None:
    """The StoryGraph edition History Import has resolved for this ABS item, if
    any — the shared source of truth for the import routes and regular sync."""
    return (_import_store.get(user_id).get(item_id) or {}).get("edition")

# ── ABS ───────────────────────────────────────────────────────────────────────

def _abs_headers(user_id: str) -> dict:
    return {"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"}


def _abs_get(user_id: str, path: str, params: dict | None = None):
    resp = req.get(f"{cfg(user_id, 'ABS_URL')}{path}", headers=_abs_headers(user_id), params=params, timeout=10)
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
    item_resp = req.get(
        f"{cfg(user_id, 'ABS_URL')}/api/items/{item_id}",
        headers=_abs_headers(user_id),
        timeout=10,
    )
    if item_resp.status_code != 200:
        return None
    return _item_to_book(item_resp.json(), progress)


def get_abs_books(
    user_id: str,
    scope: str,
    progress_by_item: dict[str, dict] | None = None,
) -> list[dict]:
    if scope == "in_progress":
        resp = _abs_get(user_id, "/api/me/items-in-progress")
        books = []
        for item in resp.json().get("libraryItems", []):
            item_id = item.get("id", "")
            if progress_by_item is not None:
                progress = progress_by_item.get(item_id, {})
            elif item_id:
                pr = req.get(f"{cfg(user_id, 'ABS_URL')}/api/me/progress/{item_id}", headers=_abs_headers(user_id), timeout=10)
                progress = pr.json() if pr.status_code == 200 else {}
            else:
                progress = {}
            book = _item_to_book(item, progress)
            if book:
                books.append(book)
        return books

    # in_progress_finished / library both need every progress entry the user has
    if progress_by_item is None:
        progress_by_item = get_abs_progress(user_id)

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
        (title, author) — one browse plus one editions fetch. Callers that need
        both a confident match and the full candidate list (to offer a manual
        pick when the match fails) load once and pass the result to
        match_audio_edition(), rather than paying for the round trips twice."""
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

    def find_audio_edition(self, title, author, duration_minutes=0, identifiers=None):
        """load_editions() + match_audio_edition(), for callers that don't also
        need the unfiltered candidate list."""
        return self.match_audio_edition(
            self.load_editions(title, author), title, duration_minutes, identifiers
        )

    def search_book(self, title, author, duration_minutes=0, identifiers=None) -> str | None:
        if not duration_minutes and not identifiers:
            initial_id = self._find_initial_book_id(title, author)
            if initial_id:
                logger.info("Found '%s' -> id=%s", title, initial_id)
            else:
                logger.warning("No StoryGraph result for '%s'", title)
            return initial_id
        matched = self.find_audio_edition(title, author, duration_minutes, identifiers)
        return matched.book_id if matched else None

    def get_book_page(self, book_id) -> str:
        return self._get(f"/books/{book_id}").text

    def _parse_current_progress(self, html) -> float | None:
        """Best-effort read of the percentage StoryGraph currently has on file for
        this book, from the same hidden field update_progress() fills in. Returns
        None if it can't be found — callers should treat that as 'unknown' and not
        skip a write on its account."""
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
        """The dates StoryGraph already has a *progress* entry on for this book —
        the read side of duplicate-avoidance for History Import, entirely
        GET-based. Status-only entries are excluded (see journal.progress_dates):
        counting them would let this route's own ensure_status() write block the
        import of today's listening.

        Paginated (confirmed live): the journal page only renders ~20 entries
        before needing page=2, 3, ... — a book with more than that silently
        hid the rest, which under-reported real successes as failures the
        first time this was tested against a live account. Stops once a page
        adds no entries we haven't already seen, with a sane upper bound
        since StoryGraph documents no page cap."""
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
        """Writes one backdated journal entry via the same 'Add note/Edit date'
        progress form a person would use by hand.

        Always posts a percentage: StoryGraph computes the actual position from
        its own known edition length, so it's immune to any runtime mismatch
        between our matched/selected edition and the source ABS audiobook (a
        manually-picked or close-tolerance match can differ by many minutes).
        Confirmed against the real form (read-only GET): for an audiobook
        edition "percentage" is a real, submittable progress_type, not just UI
        toggle state.

        Re-fetches the progress-update page immediately before posting so the
        CSRF token and any baseline context fields StoryGraph expects reflect
        whatever it actually has on file at that moment."""
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

# ── Sync logic ────────────────────────────────────────────────────────────────

_status_cache: dict[str, dict] = {}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # seconds


def get_cached_books(user_id: str, scope: str) -> tuple[list[dict], bool]:
    with _status_cache_lock:
        cache = _status_cache.get(user_id, {"books": [], "abs_ok": False, "ts": 0.0})
        if time.time() - cache["ts"] < STATUS_CACHE_TTL:
            return cache["books"], cache["abs_ok"]
    try:
        books = get_abs_books(user_id, scope)
        with _status_cache_lock:
            _status_cache[user_id] = {"books": books, "abs_ok": True, "ts": time.time()}
        return books, True
    except Exception:
        return cache["books"], False


def _target_status(book: dict) -> str:
    if book.get("is_finished"):
        return "read"
    if book["progress_percent"] > 0:
        return "currently-reading"
    return "to-read"


def _book_state_key(book: dict) -> str:
    return book.get("abs_item_id") or f"{book['title']}|{book.get('author', '')}"


def _last_synced_minutes(user_id: str, book: dict) -> float | None:
    """Durable last position, with a migration fallback for older state files."""
    previous = _sync_store.get(user_id).get(_book_state_key(book))
    if not previous:
        return None
    if previous.get("current_minutes") is not None:
        return float(previous["current_minutes"])
    if previous.get("pct") is not None and book.get("duration_minutes"):
        return round(book["duration_minutes"] * float(previous["pct"]) / 100, 1)
    return None


def do_sync(
    user_id: str,
    books: list[dict],
    start_before_finish: set[str] | None = None,
) -> list[dict]:
    user = get_user(user_id)
    label = (user or {}).get("username") or (user or {}).get("display_name") or user_id
    client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
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
            prev = synced.get(state_key) or synced.get(book["title"])

            # A manual pin is a correction, so it has to outrank the edition an
            # earlier sync settled on — otherwise "pins this book for both Sync
            # and Import" would only ever hold for books that hadn't synced yet,
            # and a bad match could never be fixed. Dropping prev also defeats
            # the unchanged check below, which is exactly right: the newly
            # pinned edition has none of the old one's progress on it.
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
                results.append({"title": book["title"], "status": "unchanged", "progress_percent": pct})
                continue

            book_id = (prev.get("storygraph_book_id") if prev else None) or pinned
            if not book_id and book.get("abs_item_id"):
                # Fall back to an edition History Import auto-matched, before
                # doing a fresh search — otherwise a match made there would only
                # ever help Import and regular sync would keep failing the same
                # way forever.
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
                    results.append({
                        "title": book["title"],
                        "status": "failed",
                        "progress_percent": pct,
                        "current_minutes": book["current_minutes"],
                    })
                    continue

            ok, already_matched, status_html = client.ensure_status(book_id, status)
            target_pct = 100 if status == "read" else pct
            # Skip the progress POST when StoryGraph already agrees with us. "read" is
            # always 100% by definition, so an already-matched read status is always
            # safe to skip (this was the original fix for finished-book duplicates).
            # For "currently reading" we can't assume that — the status label matching
            # doesn't tell us the percentage matches too — so we additionally parse the
            # progress StoryGraph has on file and only skip if it's already within
            # rounding distance of what we'd post. Without this, a book that already
            # matched (e.g. every "currently reading" book on a fresh install, before
            # this tool has any local sync-state for it) still got a progress POST on
            # every single poll, creating a spurious "started" entry and a duplicate
            # progress update in the StoryGraph journal even though nothing about the
            # book had actually changed. If we can't parse the current percentage at
            # all, we conservatively still post — no worse than the old behavior.
            current_pct = client._parse_current_progress(status_html or "") if already_matched else None
            skip_progress = status == "to-read" or (
                already_matched
                and (status == "read" or (current_pct is not None and abs(current_pct - target_pct) < 0.5))
            )
            if not skip_progress:
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
            results.append({
                "title": book["title"],
                "status": "success" if ok else "failed",
                "progress_percent": pct,
                "current_minutes": book["current_minutes"],
            })
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
        previous_state = synced.get(_book_state_key(book)) or {}
        previous = _last_synced_minutes(user_id, book) or 0.0
        newly_finished = book["is_finished"] and previous_state.get("status") != "read"
        progressed = not book["is_finished"] and book["current_minutes"] - previous >= SYNC_THRESHOLD
        if newly_finished or progressed:
            candidates.append(book)
    return candidates


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
    if not all([cfg(user_id, "ABS_URL"), cfg(user_id, "ABS_TOKEN"), cfg(user_id, "STORYGRAPH_SESSION")]):
        return
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    mode = cfg(user_id, "SYNC_MODE", "frequent")
    label = user.get("username") or user.get("display_name") or user_id
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
        scoped_books = []
        if mode == "frequent" or scheduled_run:
            scoped_books = get_abs_books(user_id, scope, progress_by_item=progress_by_item)
            with _status_cache_lock:
                _status_cache[user_id] = {"books": scoped_books, "abs_ok": True, "ts": time.time()}

        selected = (
            _frequent_sync_candidates(user_id, scoped_books)
            if mode == "frequent"
            else list(scoped_books)
        )
        by_item = {_book_state_key(book): book for book in selected}
        for item_id in changes:
            if item_id not in by_item:
                book = get_abs_book(user_id, item_id, progress_by_item[item_id])
                if book:
                    by_item[item_id] = book

        to_sync = list(by_item.values())
        results = []
        if to_sync:
            start_before_finish = {
                item_id
                for item_id, change in changes.items()
                if change["start"] and change["finish"]
            }
            results = do_sync(user_id, to_sync, start_before_finish=start_before_finish)
            for book, result in zip(to_sync, results):
                item_id = book.get("abs_item_id")
                if item_id and result["status"] in {"success", "unchanged"}:
                    _mark_handled_lifecycle(scheduler_state, progress_by_item.get(item_id, {}), item_id)
            synced = sum(1 for r in results if r["status"] == "success")
            logger.info("[%s] Auto-sync: %d/%d synced", label, synced, len(to_sync))

        if scheduled_run:
            scheduler_state["last_daily_run"] = local_date
        if changes or scheduled_run:
            _scheduler_store.save(user_id)
    except Exception as e:
        logger.error("[%s] Auto-sync error: %s", label, e)


def _poll_loop():
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD)
    while True:
        time.sleep(POLL_INTERVAL)
        for user in list_users():
            _poll_user(user)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = _get_secret_key()
# `flask run --reload` only restarts the process on .py changes, which
# incidentally picks up fresh templates too. A template-only edit never
# triggers that restart, so Jinja's cached template would otherwise go stale
# until something else causes a restart. Unconditional (not tied to
# READ_ONLY) — toggling that for write testing shouldn't also silently break
# template hot-reload, which is exactly what happened when this was gated on
# it. Negligible cost (an mtime check per render) for a low-traffic
# self-hosted tool, on or off.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Trust one hop of X-Forwarded-Proto/Host/For/Prefix from a reverse proxy in
# front of the container (Caddy, nginx, Traefik, ...). Without this, Flask has
# no way to know the original request came in over https when the proxy talks
# plain http to the container — so url_for(..., _external=True) (used to build
# the OIDC redirect_uri sent to the provider) generates an http:// URL even
# behind an https-terminating proxy, which providers like Google reject as a
# redirect_uri mismatch. Setting the proxy's forwarded headers alone doesn't
# fix this on its own — Flask/Werkzeug ignore them unless told to trust them,
# which is what this does.
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
            missing = [k for k in required_cfg if not cfg(g.user["id"], k)]
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
    if not any_users_exist():
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
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    books, abs_ok = [], False
    if cfg(user_id, "ABS_URL") and cfg(user_id, "ABS_TOKEN"):
        books, abs_ok = get_cached_books(user_id, scope)
    mode = cfg(user_id, "SYNC_MODE", "frequent")
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
    missing = [k for k in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION") if not cfg(user_id, k)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 500
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    try:
        books = get_abs_books(user_id, scope)
        if not books:
            return jsonify({"message": "No books found", "synced": 0, "total": 0, "results": []})
        results = do_sync(user_id, books)
        synced = sum(1 for r in results if r["status"] == "success")
        return jsonify({"message": "Sync complete", "synced": synced, "total": len(books), "results": results})
    except Exception as e:
        logger.error("Sync failed: %s", e)
        return jsonify({"error": str(e)}), 500


def _resolve_abs_book(user_id: str, item_id: str) -> dict | None:
    """Find a book's ABS metadata: from the cached /api/status list (which has
    the complete per-item runtime) if present, else a direct fetch."""
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    books, _ = get_cached_books(user_id, scope)
    book = next((candidate for candidate in books if candidate.get("abs_item_id") == item_id), None)
    if book:
        return book
    item = _abs_get(user_id, f"/api/items/{item_id}").json()
    progress_resp = req.get(
        f"{cfg(user_id, 'ABS_URL')}/api/me/progress/{item_id}",
        headers=_abs_headers(user_id),
        timeout=10,
    )
    progress = progress_resp.json() if progress_resp.status_code == 200 else {}
    return _item_to_book(item, progress)


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
            "read_only": True,
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

        client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
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
                # Persist the auto-match now, same as a manual pick, so the write
                # route (and a rerun of this preview) can find it without
                # re-searching — and so it stays fixed even if a later search
                # would land somewhere else.
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
        client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
        html = client.get_book_page(storygraph_book_id)
        if not html or "/sign_in" in html[:200]:
            return jsonify({"error": "Could not reach that StoryGraph book"}), 400
    except req.RequestException as exc:
        logger.warning("Manual edition lookup failed for %s: %s", storygraph_book_id, exc)
        return jsonify({"error": "Could not reach StoryGraph"}), 502

    # Title comes from the page just fetched to validate the id — the edition's
    # format and runtime aren't on a book page, so they stay unknown for a
    # hand-picked edition and the UI simply omits them.
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
    checkpoints = {
        day_key(day["date"], day["end_position_minutes"]): day
        for day in build_history_preview(sessions, book["duration_minutes"])["days"]
    }

    imported_days = _import_store.get(user_id)[item_id].setdefault("imported_days", {})

    label = (get_user(user_id) or {}).get("username") or user_id
    try:
        client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
        if not client.check_auth():
            return jsonify({"error": "StoryGraph session invalid — update it in Settings"}), 401
        # A dated progress entry needs an existing read status to attach to —
        # a book that's never been touched on StoryGraph (still "to read") has
        # no such record. ensure_status() is a no-op if one already exists.
        status_ok, _, _ = client.ensure_status(storygraph_book_id, "currently-reading")
        if not status_ok:
            return jsonify({"error": "Could not set this book to 'currently reading' on StoryGraph, which a dated entry needs to attach to"}), 502
        already_logged = client.get_logged_progress_dates(storygraph_book_id)
    except req.RequestException as exc:
        logger.warning("History import failed to reach StoryGraph for %s: %s", item_id, exc)
        return jsonify({"error": "Could not reach StoryGraph"}), 502

    results = []
    posted = []  # entries StoryGraph gave an HTTP-success response for, pending verification
    for key in sorted(requested):
        day = checkpoints.get(key)
        if day is None:
            # Either the browser sent a key from a preview ABS has since moved
            # past, or it made one up. Same answer either way.
            results.append({"date": key.split("@", 1)[0], "status": "skipped", "reason": "stale_preview"})
            continue
        date, percent = day["date"], day["progress_percent"]
        if key in imported_days:
            results.append({"date": date, "status": "skipped", "reason": "already_imported"})
            continue
        if date in already_logged:
            results.append({"date": date, "status": "skipped", "reason": "already_logged_on_storygraph"})
            continue
        if percent is None:
            results.append({"date": date, "status": "skipped", "reason": "no_percent"})
            continue
        try:
            ok = client.add_dated_progress_entry(storygraph_book_id, date, percent)
        except req.RequestException as exc:
            logger.warning("[%s] Import write failed for %s on %s: %s", label, item_id, date, exc)
            results.append({"date": date, "status": "failed", "reason": str(exc)})
            continue
        if ok:
            posted.append({"date": date, "percent": percent, "key": key})
        else:
            results.append({"date": date, "status": "failed", "reason": "storygraph_rejected"})

    # /update-progress-with-note is a full-page form, not the small AJAX widgets
    # ensure_status()/update_progress() use — a 200/302 there doesn't reliably
    # mean StoryGraph actually saved anything (e.g. a rejected CSRF token or
    # format mismatch can still redirect). Confirm against the real journal
    # before ever treating a day as imported or recording it as done.
    if posted:
        try:
            now_logged = client.get_logged_progress_dates(storygraph_book_id)
        except req.RequestException:
            now_logged = set()
        for p in posted:
            if p["date"] in now_logged:
                imported_days[p["key"]] = {"percent": p["percent"], "imported_at": time.time()}
                results.append({"date": p["date"], "status": "success", "progress_percent": p["percent"]})
            else:
                results.append({"date": p["date"], "status": "failed", "reason": "storygraph_did_not_save"})
        _import_store.save(user_id)

    results.sort(key=lambda r: r["date"])
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
    logger.info("[%s] Settings updated via UI", g.user.get("username") or g.user.get("display_name") or user_id)
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
        "SYNC_SCOPE": cfg(user_id, "SYNC_SCOPE", "in_progress"),
        "SYNC_MODE": cfg(user_id, "SYNC_MODE", "frequent"),
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
