"""
Health checks for GlitchTip uptime monitoring and operational visibility.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CYCLE_DAILY_AT_UTC_PARSED, DATA_DIR


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_cycle_info() -> tuple[int | None, datetime | None]:
    """Return (cycle_number, timestamp) for the most recent completed cycle."""
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return None, None

    latest_cycle: int | None = None
    latest_ts: datetime | None = None
    for path in cycles_dir.glob("*.json"):
        try:
            cycle = int(path.stem)
        except ValueError:
            continue
        ts = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = _parse_iso(data.get("timestamp"))
        except (json.JSONDecodeError, OSError):
            pass
        if latest_cycle is None or cycle > latest_cycle:
            latest_cycle = cycle
            latest_ts = ts
    return latest_cycle, latest_ts


def _max_cycle_age() -> timedelta:
    """How old the last cycle may be before /health/cycle returns unhealthy."""
    if CYCLE_DAILY_AT_UTC_PARSED:
        return timedelta(hours=36)
    return timedelta(hours=6)


def web_health() -> dict:
    return {"status": "ok", "service": "web"}


def cycle_health() -> tuple[dict, int]:
    """
    Check whether inquiry cycles are completing on schedule.
    Returns (payload, http_status) — 503 when stale or failing.
    """
    now = datetime.now(timezone.utc)
    max_age = _max_cycle_age()

    state_path = DATA_DIR / "state.json"
    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    last_cycle, last_ts = _last_cycle_info()
    last_failure = state.get("last_failure")
    age_seconds: int | None = None
    stale = False

    if last_ts is not None:
        age = now - last_ts.astimezone(timezone.utc)
        age_seconds = int(age.total_seconds())
        stale = age > max_age
    elif last_cycle is not None:
        stale = True

    status = "ok"
    if stale or last_failure:
        status = "unhealthy"

    payload = {
        "status": status,
        "service": "cycle",
        "last_cycle": last_cycle,
        "last_cycle_at": last_ts.isoformat() if last_ts else None,
        "age_seconds": age_seconds,
        "max_age_seconds": int(max_age.total_seconds()),
        "state_cycle": state.get("cycle"),
        "last_failure": last_failure,
        "daily_schedule_utc": (
            f"{CYCLE_DAILY_AT_UTC_PARSED[0]:02d}:{CYCLE_DAILY_AT_UTC_PARSED[1]:02d}"
            if CYCLE_DAILY_AT_UTC_PARSED
            else None
        ),
    }
    return payload, 200 if status == "ok" else 503
