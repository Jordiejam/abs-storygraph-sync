"""StoryGraph edition parsing and deterministic audiobook matching."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

import json
import unicodedata

from bs4 import BeautifulSoup


# A StoryGraph book or journal-entry id.
STORYGRAPH_ID_PATTERN = r"[0-9a-fA-F-]{36}"
_BOOK_PATH_RE = re.compile(rf"^/books/({STORYGRAPH_ID_PATTERN})/?$")
# The "16h 10m • audio • 2021" line ("13h" for whole hours). Requiring the
# bullet stops a series number or tag reading as a runtime.
_DURATION_RE = re.compile(r"(?<![\w#])(?:(\d+)h(?:\s*(\d+)m)?|(\d+)m)(?=\s*•)")


@dataclass(frozen=True)
class EditionCandidate:
    book_id: str
    title: str
    format: str
    duration_minutes: float | None
    identifier: str | None
    language: str | None
    publisher: str | None
    narrators: tuple[str, ...] = ()
    # The edition on your StoryGraph shelf; see parse_storygraph_editions.
    read_by_you: bool = False

    @property
    def is_audio(self) -> bool:
        return "audio" in self.format.casefold()


def bare_edition(book_id: str, title: str = "") -> EditionCandidate:
    """An edition known by its id alone, with no card to describe it."""
    return EditionCandidate(book_id, title, "", None, None, None, None)


@dataclass(frozen=True)
class AudiobookDetails:
    """What Audiobookshelf knows about a book beyond its runtime and ids."""
    narrators: tuple[str, ...] = ()
    publisher: str | None = None
    language: str | None = None


# What StoryGraph prints in a field it has no value for ("ISBN/UID: None").
_PLACEHOLDERS = {"none", "not specified"}


def _field_value(lines: list[str], label: str) -> str | None:
    wanted = label.casefold().rstrip(":")
    for index, line in enumerate(lines):
        key, separator, value = line.partition(":")
        if key.strip().casefold() != wanted:
            continue
        if separator and value.strip():
            value = value.strip()
        elif index + 1 < len(lines):
            value = lines[index + 1].strip()
        else:
            return None
        return None if value.casefold() in _PLACEHOLDERS else value or None
    return None


def _narrators(container, lines: list[str], is_audio: bool) -> tuple[str, ...]:
    """Contributors labelled "(Narrator)", plus, on an audio edition, any
    credited with no role at all ("with John Keating")."""
    names = [
        lines[index - 1]
        for index, line in enumerate(lines)
        if index and line.casefold().startswith("(narrator)")
    ]
    if is_audio:
        for credits in container.select(".contributor-names"):
            for person in credits.find_all("a"):
                role = person.next_sibling
                if not re.match(r"\s*\(", str(role or "")):
                    names.append(person.get_text(" ", strip=True))
    return tuple(dict.fromkeys(name for name in names if name))


def _duration_minutes(text: str) -> float | None:
    for match in _DURATION_RE.finditer(text):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or match.group(3) or 0)
        if hours or minutes:
            return float(hours * 60 + minutes)
    return None


def _edition_container(link):
    """The smallest ancestor holding one complete edition card, or None when
    it holds several (the work's own link at the top of the page)."""
    for parent in link.parents:
        if getattr(parent, "name", None) in {"body", "html"}:
            break
        text = parent.get_text("\n", strip=True)
        if "ISBN/UID:" in text and "Format:" in text:
            return parent if text.count("ISBN/UID:") == 1 else None
    return None


def parse_storygraph_editions(html: str) -> list[EditionCandidate]:
    """Edition cards from a ``/books/<id>/editions`` page, one per book id.
    Reads visible labels rather than layout classes, which change often."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[EditionCandidate] = []
    read_ids: list[str] = []

    for link in soup.find_all("a", href=True):
        match = _BOOK_PATH_RE.match(link.get("href", ""))
        if not match:
            continue
        # "You've read another edition" links sit in other cards and point at
        # the edition on your shelf; they describe no card themselves.
        status = link.get_text(" ", strip=True).casefold()
        if status.endswith("another edition"):
            if "read another edition" in status:
                read_ids.append(match.group(1))
            continue
        container = _edition_container(link)
        if container is None:
            continue

        lines = [line.strip() for line in container.get_text("\n", strip=True).splitlines() if line.strip()]
        edition_format = _field_value(lines, "Format") or ""
        candidates.append(EditionCandidate(
            book_id=match.group(1),
            title=link.get_text(" ", strip=True),
            format=edition_format,
            duration_minutes=_duration_minutes(container.get_text(" ", strip=True)),
            identifier=_field_value(lines, "ISBN/UID"),
            language=_field_value(lines, "Language"),
            publisher=_field_value(lines, "Publisher"),
            narrators=_narrators(container, lines, "audio" in edition_format.casefold()),
        ))

    if read_ids:
        # It may be on a later page, so keep it as a bare id; merging marks
        # its full card if there is one.
        candidates.append(replace(bare_edition(read_ids[0]), read_by_you=True))
    return merge_editions(candidates)


# A JS string literal in the filter's jQuery response (Rails escape_javascript).
_JS_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NEXT_PAGE_RE = re.compile(r"next_link[^>]*?href=\\?\"[^\"\\]*?[?&]page=(\d+)")


def parse_filtered_editions(script: str) -> tuple[list[EditionCandidate], int | None]:
    """One page of the edition filter (``/filter-editions``) and the next
    page's number, if any. The cards are HTML inside the jQuery response's
    string literals, twice over (one per branch of an if)."""
    fragments = []
    for literal in _JS_STRING_RE.findall(script):
        if "ISBN/UID" not in literal:
            continue
        try:
            # escape_javascript also escapes single quotes, which JSON doesn't.
            fragments.append(json.loads('"' + literal.replace("\\'", "'") + '"'))
        except ValueError:
            continue
    next_pages = [int(page) for page in _NEXT_PAGE_RE.findall(script)]
    # Each branch carries the same cards, so parse each distinct literal once.
    return parse_storygraph_editions("".join(dict.fromkeys(fragments))), max(next_pages) if next_pages else None


def merge_editions(*lists: list[EditionCandidate]) -> list[EditionCandidate]:
    """One candidate per book id: the most complete copy, marked as yours if
    any page said so."""
    by_id: dict[str, EditionCandidate] = {}
    for candidate in (candidate for editions in lists for candidate in editions):
        previous = by_id.get(candidate.book_id)
        best = candidate
        if previous is not None and _candidate_completeness(previous) >= _candidate_completeness(candidate):
            best = previous
        by_id[candidate.book_id] = replace(
            best, read_by_you=candidate.read_by_you or bool(previous and previous.read_by_you),
        )
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
            candidate.narrators,
        )
    )


def _normalise_identifier(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _wanted_identifiers(identifiers: list[str] | None) -> set[str]:
    return {_normalise_identifier(value) for value in identifiers or [] if value}


def _runtime_tolerance(target_duration_minutes: float) -> float:
    """2% of the runtime, between three and fifteen minutes."""
    return max(3.0, min(15.0, target_duration_minutes * 0.02))


def _normalise_name(value: str | None) -> str:
    # Accents dropped: "Kay Eluvian" in ABS is "Kay Elúvian" on StoryGraph.
    plain = "".join(char for char in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\s]", " ", plain.casefold()).split())


_PUBLISHER_NOISE = {"ltd", "limited", "inc", "llc", "co", "the", "publishing", "publishers"}

# ABS sometimes has an ISO code where StoryGraph shows the language name.
_LANGUAGE_CODES = {
    "en": "english", "eng": "english", "de": "german", "ger": "german", "deu": "german",
    "fr": "french", "fre": "french", "fra": "french", "es": "spanish", "spa": "spanish",
    "it": "italian", "ita": "italian", "nl": "dutch", "dut": "dutch", "nld": "dutch",
    "pt": "portuguese", "por": "portuguese", "ja": "japanese", "jpn": "japanese",
    "sv": "swedish", "swe": "swedish", "pl": "polish", "pol": "polish",
}


def normalise_language(value: str | None) -> str:
    name = _normalise_name(value)
    return _LANGUAGE_CODES.get(name, name)


def _publisher_words(value: str | None) -> set[str]:
    return set(_normalise_name(value).split()) - _PUBLISHER_NOISE


def edition_checks(candidate: EditionCandidate, details: AudiobookDetails | None) -> dict:
    """True/False/None (unknown) for whether this edition's narrator,
    publisher and language agree with ABS's. "narrator" means one is shared,
    "narrator_exact" the same list."""
    details = details or AudiobookDetails()
    checks = {"narrator": None, "narrator_exact": None, "publisher": None, "language": None}

    wanted = {_normalise_name(name) for name in details.narrators if _normalise_name(name)}
    listed = {_normalise_name(name) for name in candidate.narrators}
    if wanted and listed:
        checks["narrator"] = bool(wanted & listed)
        checks["narrator_exact"] = wanted == listed

    ours, theirs = _publisher_words(details.publisher), _publisher_words(candidate.publisher)
    if ours and theirs and _normalise_name(candidate.publisher) not in _PLACEHOLDERS:
        # Imprints are named loosely ("Penguin Audio" / "Penguin Books Ltd").
        checks["publisher"] = ours <= theirs or theirs <= ours

    ours, theirs = normalise_language(details.language), normalise_language(candidate.language)
    if ours and theirs:
        checks["language"] = ours == theirs
    return checks


def _signals(candidate: EditionCandidate, details: AudiobookDetails | None) -> dict:
    return {**edition_checks(candidate, details), "read_before": candidate.read_by_you or None}


def _metadata_score(candidate: EditionCandidate, details: AudiobookDetails | None) -> int:
    """Tiebreak between qualifying editions: the one you've read first (to
    keep progress on one entry), unless another has the right narrator; an
    exact narrator list edges out a shared one."""
    checks = _signals(candidate, details)
    return (
        (4 if checks["read_before"] else 0)
        + {True: 2, False: -2, None: 0}[checks["narrator"]]
        + (1 if checks["narrator_exact"] else 0)
        + (1 if checks["publisher"] else 0)
    )


def match_audio_edition(
    candidates: list[EditionCandidate],
    *,
    target_duration_minutes: float | None,
    identifiers: list[str] | None = None,
    details: AudiobookDetails | None = None,
    tagged_id: str | None = None,
) -> tuple[EditionCandidate | None, dict]:
    """_match_audio, plus whether (and if not, why not) the edition you've
    read is the match."""
    best, reason = _match_audio(
        candidates,
        target_duration_minutes=target_duration_minutes,
        identifiers=identifiers,
        details=details,
        tagged_id=tagged_id,
    )
    read = next((candidate for candidate in candidates if candidate.read_by_you), None)
    if read:
        reason["read_edition"] = {
            "storygraph_book_id": read.book_id,
            "title": read.title or None,
            "format": read.format or None,
            "language": read.language,
            "chosen": best is read,
            **({} if best is read else _why_not_chosen(read, best, target_duration_minutes, identifiers, details, tagged_id)),
        }
    return best, reason


def is_strong_match(reason: dict | None, checks: dict | None) -> bool:
    """Whether a suggestion can be confirmed without a person: the ABS tag;
    an ISBN/ASIN match whose narrator and language don't disagree; or a
    runtime match with an agreeing narrator that nothing else came close to,
    or that the narrator or your earlier read set apart. A runtime alone
    can't tell regional releases of one recording apart."""
    reason, checks = reason or {}, checks or {}
    code = reason.get("code")
    if code == "tagged":
        return True
    if checks.get("language") is False or checks.get("narrator") is False:
        return False
    if code == "identifier":
        return True
    if code == "runtime" and checks.get("narrator"):
        decided_by = set(reason.get("decided_by") or ())
        return not reason.get("others_within_tolerance") or bool(
            decided_by & {"read_before", "narrator", "narrator_exact"}
        )
    return False


def _why_not_chosen(read, best, target_duration_minutes, identifiers, details, tagged_id=None) -> dict:
    """Why the edition you've read isn't the match, as {"problem": code, ...}."""
    if best and best.book_id == tagged_id:
        return {"problem": "tagged_elsewhere"}
    if not read.format:
        return {"problem": "not_listed"}
    if not read.is_audio:
        return {"problem": "not_audio"}
    if edition_checks(read, details)["language"] is False:
        return {"problem": "other_language"}
    wanted_ids = _wanted_identifiers(identifiers)
    if best and wanted_ids and _normalise_identifier(best.identifier) in wanted_ids:
        return {"problem": "identifier_elsewhere"}
    if target_duration_minutes and read.duration_minutes is None:
        return {"problem": "no_runtime"}
    if target_duration_minutes and read.duration_minutes is not None:
        delta = round(read.duration_minutes - target_duration_minutes, 1)
        if abs(delta) > _runtime_tolerance(target_duration_minutes):
            return {"problem": "runtime_mismatch", "delta_minutes": delta}
    if best is None:
        return {"problem": "unclear"}
    # It qualifies too; the pick won on narrator.
    return {"problem": "outranked", "narrator_check": edition_checks(read, details)["narrator"]}


def _match_audio(
    candidates: list[EditionCandidate],
    *,
    target_duration_minutes: float | None,
    identifiers: list[str] | None = None,
    details: AudiobookDetails | None = None,
    tagged_id: str | None = None,
) -> tuple[EditionCandidate | None, dict]:
    """A confident audio edition or None, and a reason dict whose "code"
    names the deciding rule. The tagged edition (a person's pick) wins
    outright; otherwise, among editions in ABS's language, an ISBN/ASIN match,
    then a runtime within _runtime_tolerance. No match beats a wrong one.
    Narrator and publisher only choose between editions that already qualify,
    since regional releases often share a runtime to the minute."""
    all_audio = [candidate for candidate in candidates if candidate.is_audio]
    audio = [candidate for candidate in all_audio if edition_checks(candidate, details)["language"] is not False]
    wanted_ids = _wanted_identifiers(identifiers)
    reason = {
        "editions": len(candidates),
        "audio_editions": len(audio),
        "other_language_editions": len(all_audio) - len(audio),
        "abs_has_identifier": bool(wanted_ids),
    }
    tagged = next((candidate for candidate in candidates if candidate.book_id == tagged_id), None) if tagged_id else None
    if tagged:
        return tagged, {**reason, "code": "tagged"}
    if not candidates:
        return None, {**reason, "code": "no_results"}
    if not all_audio:
        return None, {**reason, "code": "no_audio"}
    if not audio:
        return None, {**reason, "code": "language_mismatch", "language": details.language}

    def pick(qualifying: list[EditionCandidate], delta) -> EditionCandidate:
        best = max(qualifying, key=lambda candidate: (_metadata_score(candidate, details), -delta(candidate)))
        others = [candidate for candidate in qualifying if candidate is not best]
        reason["others_within_tolerance"] = len(others)
        best_score = _metadata_score(best, details)
        if others and all(_metadata_score(candidate, details) < best_score for candidate in others):
            # Only checks some rival failed decided anything.
            checks = _signals(best, details)
            rivals = [_signals(candidate, details) for candidate in others]
            reason["decided_by"] = [
                name for name in ("read_before", "narrator", "narrator_exact", "publisher")
                if checks[name] and not all(rival[name] for rival in rivals)
            ]
        return best

    if wanted_ids:
        exact = [candidate for candidate in audio if _normalise_identifier(candidate.identifier) in wanted_ids]
        if exact:
            target = target_duration_minutes or 0
            best = pick(exact, lambda candidate: abs((candidate.duration_minutes or target) - target))
            return best, {**reason, "code": "identifier", "identifier": best.identifier}

    if not target_duration_minutes or target_duration_minutes <= 0:
        if len(audio) == 1:
            return audio[0], {**reason, "code": "only_audio"}
        return None, {**reason, "code": "no_abs_runtime"}

    with_duration = [candidate for candidate in audio if candidate.duration_minutes is not None]
    if not with_duration:
        return None, {**reason, "code": "no_edition_runtime"}

    def delta(candidate):
        return abs(candidate.duration_minutes - target_duration_minutes)

    tolerance = _runtime_tolerance(target_duration_minutes)
    closest = min(with_duration, key=delta)
    reason["tolerance_minutes"] = round(tolerance, 1)
    within = [candidate for candidate in with_duration if delta(candidate) <= tolerance]
    if not within:
        reason["closest_delta_minutes"] = round(closest.duration_minutes - target_duration_minutes, 1)
        return None, {**reason, "code": "runtime_mismatch"}
    best = pick(within, delta)
    reason["closest_delta_minutes"] = round(best.duration_minutes - target_duration_minutes, 1)
    return best, {**reason, "code": "runtime"}
