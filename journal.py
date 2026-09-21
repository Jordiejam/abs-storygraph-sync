"""Read-only parsing of a StoryGraph reading journal page."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import re

from bs4 import BeautifulSoup


_ENTRY_PATH_RE = re.compile(r"/journal_entries/([0-9a-fA-F-]{36})/edit")
_ENTRY_DATE_RE = re.compile(
    r"\b(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})\b"
)
_PERCENT_STYLE_RE = re.compile(r"width:\s*([\d.]+)%")
_PERCENT_TEXT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    date: str
    percent: float | None


def _entry_container(link):
    """Return the ancestor holding one full entry (date row + status/percent row).

    StoryGraph's journal page puts the date in its own wrapper, sibling to the
    status/percent wrapper. Several nested ancestors repeat the same
    date-only text before an ancestor's text actually grows to include the
    status/percent content too — that first ancestor whose text differs from
    the date-only row is the full entry.
    """
    date_row_text = None
    for parent in link.parents:
        if getattr(parent, "name", None) in {"body", "html"}:
            break
        text = parent.get_text(" ", strip=True)
        if not _ENTRY_DATE_RE.search(text):
            continue
        if date_row_text is None:
            date_row_text = text
            continue
        if text != date_row_text:
            return parent
    return None


def _parse_entry_date(text: str) -> str | None:
    match = _ENTRY_DATE_RE.search(text)
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return datetime.strptime(f"{day} {month_name} {year}", "%d %B %Y").date().isoformat()
    except ValueError:
        return None


def _entry_percent(container) -> float | None:
    style_match = _PERCENT_STYLE_RE.search(str(container))
    if style_match:
        return float(style_match.group(1))
    text_match = _PERCENT_TEXT_RE.search(container.get_text(" ", strip=True))
    if text_match:
        return float(text_match.group(1))
    return None


def parse_journal_page(html: str) -> list[JournalEntry]:
    """Parse logged entries from a StoryGraph ``/journal?book_id=<id>`` page.

    Relies on the visible date text and percent readout rather than layout
    classes, since the site's markup changes fairly often (same philosophy as
    matcher.py's edition parsing).
    """
    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, JournalEntry] = {}

    for link in soup.find_all("a", href=True):
        match = _ENTRY_PATH_RE.search(link.get("href", ""))
        if not match:
            continue
        container = _entry_container(link)
        if container is None:
            continue
        date = _parse_entry_date(container.get_text(" ", strip=True))
        if date is None:
            continue
        entry_id = match.group(1)
        by_id[entry_id] = JournalEntry(
            entry_id=entry_id,
            date=date,
            percent=_entry_percent(container),
        )

    return list(by_id.values())


def progress_dates(entries: Iterable[JournalEntry]) -> set[str]:
    """The dates that already carry a real progress entry.

    Status-only entries ("Started reading", "Finished") are deliberately
    excluded: they carry no percentage, and the import route's own
    ensure_status() call creates one dated today — counting it would make the
    import skip today's listening as already logged.
    """
    return {entry.date for entry in entries if entry.percent is not None}
