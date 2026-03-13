#!/usr/bin/env python3
"""Sync challenge status from cycle records into human_challenges.json.

Use when a challenge was woven in a cycle but human_challenges still shows it as pending
(e.g. due to a race or missed update). Scans archive/cycles for injection.woven_challenge
and sets those challenges to status=woven with linked_cycle/linked_journal.

Usage:
  .venv/bin/python scripts/sync_challenge_status_from_cycles.py
  .venv/bin/python scripts/sync_challenge_status_from_cycles.py --diagnose
  .venv/bin/python scripts/sync_challenge_status_from_cycles.py --fix-id visitor-1773300604330 [--cycle 31]
  .venv/bin/python scripts/sync_challenge_status_from_cycles.py --recover-orphans
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
    diagnose = "--diagnose" in sys.argv
    fix_id = None
    fix_cycle = None
    recover_orphans = "--recover-orphans" in sys.argv
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--diagnose":
            pass
        elif arg == "--recover-orphans":
            pass
        elif arg.startswith("--fix-id="):
            fix_id = arg.split("=", 1)[1].strip()
        elif arg == "--fix-id":
            if i + 1 < len(args):
                fix_id = args[i + 1].strip()
                i += 1
        elif arg.startswith("--cycle="):
            try:
                fix_cycle = int(arg.split("=", 1)[1].strip())
            except ValueError:
                fix_cycle = None
        elif arg == "--cycle" and i + 1 < len(args):
            try:
                fix_cycle = int(args[i + 1].strip())
                i += 1
            except ValueError:
                fix_cycle = None
        i += 1

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

    if diagnose:
        print("=== human_challenges.json ===")
        for c in challenges:
            print(f"  [{c.get('status','?')}] {c.get('id')} | score={c.get('score')} | linked_cycle={c.get('linked_cycle')}")

    if not CYCLES_DIR.exists():
        print("No archive/cycles dir found.")
        return

    if diagnose:
        print("\n=== Cycle records with woven_challenge ===")

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
        if diagnose:
            in_hc = sid in by_id
            current_status = by_id.get(sid, {}).get("status", "NOT IN FILE")
            print(f"  Cycle {cycle}: woven={sid} | in_hc={in_hc} | status={current_status}")
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

    # --fix-id: force-mark a specific challenge as woven even if no cycle record found
    if fix_id:
        c = by_id.get(fix_id)
        if c is None:
            print(f"\n--fix-id: {fix_id!r} not found in human_challenges.json")
        elif c.get("status") in ("woven", "reviewed"):
            print(f"\n--fix-id: {fix_id!r} already has status={c['status']!r}, nothing to do")
            print("  If the page still shows it as waiting, this is a display/cache issue.")
        else:
            print(f"\n--fix-id: force-marking {fix_id!r} as woven")
            c["status"] = "woven"
            c["resolved_at"] = c.get("resolved_at") or datetime.now(timezone.utc).isoformat()
            if fix_cycle is not None:
                c["linked_cycle"] = fix_cycle
                c["linked_journal"] = str(fix_cycle)
            updated += 1

    # --recover-orphans: add minimal entries for woven_challenge ids that are in cycle records but not in HC
    if recover_orphans and CYCLES_DIR.exists():
        seen_orphan_ids = set()
        for path in sorted(CYCLES_DIR.glob("*.json"), key=lambda p: int(p.stem)):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            injection = rec.get("injection") or {}
            woven = injection.get("woven_challenge") or {}
            sid = (woven.get("submission_id") or "").strip()
            if not sid or sid in by_id or sid in seen_orphan_ids:
                continue
            cycle = rec.get("cycle")
            if cycle is None:
                try:
                    cycle = int(path.stem)
                except ValueError:
                    continue
            seen_orphan_ids.add(sid)
            text = (woven.get("text") or "").strip() or "(no text)"
            if len(text) > 500:
                text = text[:497] + "..."
            entry = {
                "id": sid,
                "text": text,
                "raw_text": text,
                "submitted_at": rec.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "score": None,
                "status": "woven",
                "source": "visitor",
                "linked_cycle": cycle,
                "linked_journal": str(cycle),
                "resolved_at": rec.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "lapse_reason": None,
                "submitter_name": woven.get("submitter_name"),
            }
            challenges.append(entry)
            by_id[sid] = entry
            updated += 1
            print(f"  Recovered {sid} -> woven (cycle {cycle})")

    if not updated:
        print("No challenges needed status sync.")
        if not diagnose:
            print("Run with --diagnose to inspect the full state.")
        return

    HC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HC_PATH, "w", encoding="utf-8") as f:
        json.dump(challenges, f, indent=2, ensure_ascii=False)
    print(f"Synced {updated} challenge(s).")


if __name__ == "__main__":
    main()
