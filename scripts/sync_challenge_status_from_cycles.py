#!/usr/bin/env python3
"""Sync challenge status from cycle records into human_challenges.json.

Use when a challenge was woven in a cycle but human_challenges still shows it as pending
(e.g. due to a race or missed update). Scans archive/cycles for injection.woven_challenge
and sets those challenges to status=woven with linked_cycle/linked_journal.

Usage: .venv/bin/python scripts/sync_challenge_status_from_cycles.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_DIR

INJECTIONS = DATA_DIR / "injections"
HC_PATH = INJECTIONS / "human_challenges.json"
CYCLES_DIR = DATA_DIR / "archive" / "cycles"


def main() -> None:
    if not HC_PATH.exists():
        print("No human_challenges.json found.")
        return

    challenges = json.loads(HC_PATH.read_text(encoding="utf-8"))
    if not isinstance(challenges, list):
        print("human_challenges.json is not a list.")
        return

    by_id = {c.get("id"): c for c in challenges}
    if len(by_id) != len(challenges):
        print("Warning: duplicate ids in human_challenges.json")

    if not CYCLES_DIR.exists():
        print("No archive/cycles dir found.")
        return

    updated = 0
    for path in sorted(CYCLES_DIR.glob("*.json"), key=lambda p: int(p.stem), reverse=True):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        injection = rec.get("injection") or {}
        woven = injection.get("woven_challenge") or {}
        sid = (woven.get("submission_id") or "").strip()
        if not sid:
            continue
        cycle = rec.get("cycle")
        if cycle is None:
            try:
                cycle = int(path.stem)
            except ValueError:
                continue
        c = by_id.get(sid)
        if c is None:
            continue
        if c.get("status") in ("woven", "reviewed"):
            continue
        c["status"] = "woven"
        c["linked_cycle"] = cycle
        c["linked_journal"] = str(cycle)
        c["resolved_at"] = rec.get("timestamp") or datetime.now(timezone.utc).isoformat()
        updated += 1
        print(f"  {sid} -> woven (cycle {cycle})")

    if not updated:
        print("No pending challenges needed status sync.")
        return

    HC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HC_PATH, "w", encoding="utf-8") as f:
        json.dump(challenges, f, indent=2, ensure_ascii=False)
    print(f"Synced {updated} challenge(s).")


if __name__ == "__main__":
    main()
