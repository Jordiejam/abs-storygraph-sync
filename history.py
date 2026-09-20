"""Read-only reconstruction of daily audiobook progress from ABS sessions."""

from __future__ import annotations

from datetime import datetime, timezone
import re


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _session_date(session: dict) -> str | None:
    value = session.get("date")
    if isinstance(value, str) and _DATE_RE.fullmatch(value):
        return value

    for key in ("updatedAt", "startedAt"):
        timestamp = _number(session.get(key))
        if timestamp <= 0:
            continue
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            continue
    return None


def build_history_preview(sessions: list[dict], duration_minutes: float) -> dict:
    """Collapse playback sessions into conservative daily checkpoints.

    The furthest position reached is monotonic across days. Rewinds and repeated
    passages still contribute listening time, but never move the proposed
    StoryGraph checkpoint backwards.
    """
    duration_seconds = max(0.0, _number(duration_minutes) * 60)
    grouped: dict[str, dict] = {}
    skipped = 0

    for session in sessions:
        date = _session_date(session)
        current = _number(session.get("currentTime"), -1)
        if not date or current < 0:
            skipped += 1
            continue
        day = grouped.setdefault(date, {
            "date": date,
            "session_count": 0,
            "listening_seconds": 0.0,
            "furthest_session_position": 0.0,
        })
        day["session_count"] += 1
        day["listening_seconds"] += max(0.0, _number(session.get("timeListening")))
        day["furthest_session_position"] = max(day["furthest_session_position"], current)

    days = []
    furthest = 0.0
    suspicious_days = 0
    for date in sorted(grouped):
        raw = grouped[date]
        raw_position = raw["furthest_session_position"]
        flags = []
        if raw_position + 60 < furthest:
            flags.append("rewind_or_relisten")
        previous = furthest
        furthest = max(furthest, raw_position)
        if duration_seconds:
            furthest = min(furthest, duration_seconds)
        if furthest <= previous + 1:
            flags.append("no_new_progress")
        if flags:
            suspicious_days += 1

        days.append({
            "date": date,
            "session_count": raw["session_count"],
            "listening_minutes": round(raw["listening_seconds"] / 60, 1),
            "new_progress_minutes": round(max(0.0, furthest - previous) / 60, 1),
            "end_position_minutes": round(furthest / 60, 1),
            "progress_percent": round((furthest / duration_seconds) * 100, 1) if duration_seconds else None,
            "flags": flags,
        })

    total_listening = sum(day["listening_seconds"] for day in grouped.values()) / 60
    confidence = "high"
    if skipped or suspicious_days:
        confidence = "review"
    if not days or not duration_seconds:
        confidence = "insufficient"

    return {
        "summary": {
            "session_count": sum(day["session_count"] for day in grouped.values()),
            "day_count": len(days),
            "first_date": days[0]["date"] if days else None,
            "last_date": days[-1]["date"] if days else None,
            "total_listening_minutes": round(total_listening, 1),
            "latest_position_minutes": round(furthest / 60, 1),
            "latest_progress_percent": round((furthest / duration_seconds) * 100, 1) if duration_seconds else None,
            "skipped_session_count": skipped,
            "flagged_day_count": suspicious_days,
            "confidence": confidence,
        },
        "days": days,
    }
