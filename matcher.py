"""StoryGraph edition parsing and deterministic audiobook matching."""

from __future__ import annotations

from dataclasses import dataclass
import re

from bs4 import BeautifulSoup


_BOOK_PATH_RE = re.compile(r"^/books/([0-9a-fA-F-]{36})/?$")
_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)", re.IGNORECASE)


@dataclass(frozen=True)
class EditionCandidate:
    book_id: str
    title: str
    format: str
    duration_minutes: float | None
    identifier: str | None
    language: str | None
    publisher: str | None

    @property
    def is_audio(self) -> bool:
        return "audio" in self.format.casefold()


def _field_value(lines: list[str], label: str) -> str | None:
    wanted = label.casefold().rstrip(":")
    for index, line in enumerate(lines):
        key, separator, value = line.partition(":")
        if key.strip().casefold() != wanted:
            continue
        if separator and value.strip():
            return value.strip()
        if index + 1 < len(lines):
            return lines[index + 1].strip() or None
    return None


def _duration_minutes(text: str) -> float | None:
    for match in _DURATION_RE.finditer(text):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        if hours or minutes:
            return float(hours * 60 + minutes)
    return None


def _edition_container(link):
    """Return the smallest ancestor containing one complete edition card."""
    for parent in link.parents:
        if getattr(parent, "name", None) in {"body", "html"}:
            break
        text = parent.get_text("\n", strip=True)
        if "ISBN/UID:" in text and "Format:" in text:
            return parent
    return None


def parse_storygraph_editions(html: str) -> list[EditionCandidate]:
    """Parse edition cards from a StoryGraph ``/books/<id>/editions`` page.

    StoryGraph renders duplicate desktop/mobile cards, so results are de-duplicated
    by book UUID. The parser intentionally relies on visible labels rather than a
    single layout class; the site changes its presentation markup fairly often.
    """
    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, EditionCandidate] = {}

    for link in soup.find_all("a", href=True):
        match = _BOOK_PATH_RE.match(link.get("href", ""))
        if not match:
            continue
        container = _edition_container(link)
        if container is None:
            continue

        lines = [line.strip() for line in container.get_text("\n", strip=True).splitlines() if line.strip()]
        candidate = EditionCandidate(
            book_id=match.group(1),
            title=link.get_text(" ", strip=True),
            format=_field_value(lines, "Format") or "",
            duration_minutes=_duration_minutes(container.get_text(" ", strip=True)),
            identifier=_field_value(lines, "ISBN/UID"),
            language=_field_value(lines, "Language"),
            publisher=_field_value(lines, "Publisher"),
        )
        previous = by_id.get(candidate.book_id)
        if previous is None or _candidate_completeness(candidate) > _candidate_completeness(previous):
            by_id[candidate.book_id] = candidate

    return list(by_id.values())


def _candidate_completeness(candidate: EditionCandidate) -> int:
    return sum(
        bool(value)
        for value in (
            candidate.title,
            candidate.format,
            candidate.duration_minutes,
            candidate.identifier,
            candidate.language,
            candidate.publisher,
        )
    )


def _normalise_identifier(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def choose_audio_edition(
    candidates: list[EditionCandidate],
    *,
    target_duration_minutes: float | None,
    identifiers: list[str] | None = None,
) -> EditionCandidate | None:
    """Choose a confident audiobook edition or return ``None``.

    An exact ISBN/UID/ASIN match wins. Otherwise the closest runtime must be
    within 2% of the ABS runtime, with a floor of three minutes and a ceiling of
    fifteen. Refusing an uncertain match is deliberate: no StoryGraph update is
    safer than writing progress to the wrong edition.
    """
    audio = [candidate for candidate in candidates if candidate.is_audio]
    if not audio:
        return None

    wanted_ids = {_normalise_identifier(value) for value in identifiers or [] if value}
    if wanted_ids:
        exact = [candidate for candidate in audio if _normalise_identifier(candidate.identifier) in wanted_ids]
        if exact:
            if target_duration_minutes:
                return min(
                    exact,
                    key=lambda candidate: abs((candidate.duration_minutes or target_duration_minutes) - target_duration_minutes),
                )
            return exact[0]

    if not target_duration_minutes or target_duration_minutes <= 0:
        return audio[0] if len(audio) == 1 else None

    with_duration = [candidate for candidate in audio if candidate.duration_minutes is not None]
    if not with_duration:
        return None

    best = min(with_duration, key=lambda candidate: abs(candidate.duration_minutes - target_duration_minutes))
    tolerance = max(3.0, min(15.0, target_duration_minutes * 0.02))
    if abs(best.duration_minutes - target_duration_minutes) > tolerance:
        return None
    return best
