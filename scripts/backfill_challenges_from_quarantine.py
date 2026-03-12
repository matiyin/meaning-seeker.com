#!/usr/bin/env python3
"""Backfill legacy human_challenges with submitted_at, score, raw_text from quarantine.

Run once after syncing VPS data that has old-format challenges.
Usage: .venv/bin/python scripts/backfill_challenges_from_quarantine.py
"""
import json
import sys
from pathlib import Path

# Add project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_DIR

INJECTIONS = DATA_DIR / "injections"
HC_PATH = INJECTIONS / "human_challenges.json"
QUARANTINE = INJECTIONS / "quarantine"


def _normalize_challenge(entry: dict) -> dict:
    out = dict(entry)
    if "status" not in out:
        out["status"] = "pending"
    for key in ("linked_cycle", "linked_journal", "resolved_at", "lapse_reason", "submitter_name", "raw_text", "score", "submitted_at"):
        if key not in out:
            out[key] = None
    if out.get("raw_text") is None and out.get("text"):
        out["raw_text"] = out["text"]
    return out


def main() -> None:
    if not HC_PATH.exists():
        print("No human_challenges.json found.")
        return

    challenges = json.loads(HC_PATH.read_text(encoding="utf-8"))
    if not isinstance(challenges, list):
        print("human_challenges.json is not a list.")
        return

    # Load accepted quarantine entries (by distilled text overlap)
    quarantine_entries: list[dict] = []
    if QUARANTINE.exists():
        for p in QUARANTINE.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("status") == "accepted":
                    quarantine_entries.append(data)
            except (json.JSONDecodeError, OSError):
                continue

    updated = 0
    for i, orig in enumerate(challenges):
        c = _normalize_challenge(orig)
        if c.get("submitted_at") and c.get("score") is not None:
            challenges[i] = c
            continue  # Already v2-complete

        challenge_text = (c.get("text") or "").strip()
        if not challenge_text:
            challenges[i] = c
            continue

        # Match: quarantine distilled contains visitor quote; raw text is inside challenge_text
        for q in quarantine_entries:
            raw = (q.get("text") or "").strip()
            distilled = (q.get("distilled_challenge") or "").strip()
            if not raw:
                continue
            if raw in challenge_text:
                c["submitted_at"] = q.get("timestamp") or q.get("submitted_at")
                scores = q.get("filter_scores") or {}
                c["score"] = scores.get("relevance")
                c["raw_text"] = raw
                updated += 1
                break

        challenges[i] = c

    # Rewrite with normalized entries
    with open(HC_PATH, "w", encoding="utf-8") as f:
        json.dump(challenges, f, indent=2, ensure_ascii=False)

    print(f"Backfilled {updated} challenge(s) from quarantine. Total: {len(challenges)}.")


if __name__ == "__main__":
    main()
