"""
Phase C: Paradigm shift detection and insights data assembly.

Called from the orchestrator after each cycle. Detects three types of paradigm shifts:
1. Manuscript rewritten after 10+ cycles without one
2. Long-held commitment abandoned (held for 20+ cycles)
3. Long-lived tension resolved (carried for 30+ cycles)

Persists shifts to data/paradigm_shifts.json (append-only array).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .models import Commitment, CycleOutput, Tension

logger = logging.getLogger(__name__)

PARADIGM_SHIFTS_PATH = DATA_DIR / "paradigm_shifts.json"

# Thresholds for paradigm shift detection
MANUSCRIPT_REWRITE_GAP = 10   # cycles without a manuscript update → rewrite is significant
COMMITMENT_ABANDON_MIN = 20   # cycles a commitment must be held before abandonment is a shift
TENSION_RESOLVE_MIN = 30      # cycles a tension must be held before resolution is a shift


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_paradigm_shifts() -> list[dict]:
    if not PARADIGM_SHIFTS_PATH.exists():
        return []
    try:
        return json.loads(PARADIGM_SHIFTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _append_paradigm_shifts(new_shifts: list[dict]) -> None:
    if not new_shifts:
        return
    shifts = _load_paradigm_shifts()
    shifts.extend(new_shifts)
    PARADIGM_SHIFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARADIGM_SHIFTS_PATH.write_text(
        json.dumps(shifts, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _last_manuscript_update_cycle(current_cycle: int) -> Optional[int]:
    """Find the most recent cycle before current_cycle where manuscript_update was non-None."""
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return None
    # Walk backwards from current_cycle - 1
    for c in range(current_cycle - 1, max(0, current_cycle - 200), -1):
        path = cycles_dir / f"{c:06d}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("manuscript_update"):
                return c
        except (json.JSONDecodeError, OSError):
            continue
    return None


def detect_paradigm_shifts(
    cycle: int,
    output: CycleOutput,
    tensions: list,
    commitments: list,
) -> list[dict]:
    """Detect if this cycle produced a paradigm shift.

    Args:
        cycle: Current cycle number.
        output: CycleOutput from this cycle.
        tensions: Current tensions list (may be raw dicts or Tension objects).
        commitments: Current commitments list (may be raw dicts or Commitment objects).

    Returns list of shift records (also persisted to disk).
    """
    shifts: list[dict] = []

    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # 1. Manuscript rewrite after 10+ cycles without one
    if output.manuscript_update:
        last = _last_manuscript_update_cycle(cycle)
        gap = (cycle - last) if last is not None else cycle
        if gap >= MANUSCRIPT_REWRITE_GAP:
            shifts.append({
                "cycle": cycle,
                "timestamp": _now_iso(),
                "event_type": "manuscript_rewrite",
                "description": (
                    f"Manuscript rewritten after {gap} cycles of stability"
                    if last is not None
                    else "First manuscript rewrite"
                ),
            })
            logger.info("Cycle %s: paradigm shift — manuscript rewrite (gap=%d)", cycle, gap)

    # 2. Long-held commitment abandoned (held for 20+ cycles)
    for update in output.commitment_updates:
        if _get(update, "action") == "abandon" and _get(update, "commitment_id"):
            cid = _get(update, "commitment_id")
            commitment = next(
                (c for c in commitments if _get(c, "commitment_id") == cid), None
            )
            if commitment:
                origin = _get(commitment, "origin_cycle", cycle)
                age = cycle - origin
                if age >= COMMITMENT_ABANDON_MIN:
                    statement = _get(commitment, "statement", "")[:80]
                    shifts.append({
                        "cycle": cycle,
                        "timestamp": _now_iso(),
                        "event_type": "commitment_abandoned",
                        "description": (
                            f"Abandoned commitment '{statement}' after {age} cycles"
                        ),
                    })
                    logger.info(
                        "Cycle %s: paradigm shift — commitment abandoned (age=%d): %s",
                        cycle, age, cid,
                    )

    # 3. Long-lived tension resolved (carried for 30+ cycles)
    for resolved in output.tensions_resolved:
        rid = _get(resolved, "tension_id")
        tension = next(
            (t for t in tensions if _get(t, "tension_id") == rid), None
        )
        if tension:
            created = _get(tension, "created_cycle", cycle)
            age = cycle - created
            if age >= TENSION_RESOLVE_MIN:
                desc = _get(tension, "description", "")[:80]
                shifts.append({
                    "cycle": cycle,
                    "timestamp": _now_iso(),
                    "event_type": "long_tension_resolved",
                    "description": (
                        f"Resolved '{desc}' after {age} cycles"
                    ),
                })
                logger.info(
                    "Cycle %s: paradigm shift — tension resolved (age=%d): %s",
                    cycle, age, rid,
                )

    if shifts:
        _append_paradigm_shifts(shifts)

    return shifts
