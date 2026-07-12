#!/usr/bin/env python3
"""
Extract named people/works from journal cycles and resolve them into the link dictionary.

Usage (from repo root):
  .venv/bin/python scripts/extract_links.py --dry-run
  .venv/bin/python scripts/extract_links.py --cycle 128
  .venv/bin/python scripts/extract_links.py --last 5
  .venv/bin/python scripts/extract_links.py --all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR
from src import link_resolver
from src.models import LinkCandidate


def _load_cycle_records(*, cycle: int | None, last: int, all_cycles: bool) -> list[tuple[int, dict]]:
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return []

    if cycle is not None:
        path = cycles_dir / f"{cycle:06d}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [(int(data.get("cycle", cycle)), data)]
        except (json.JSONDecodeError, OSError, ValueError):
            return []

    paths = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)
    if not all_cycles and last > 0:
        paths = paths[:last]

    records: list[tuple[int, dict]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append((int(data.get("cycle", path.stem)), data))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    records.sort(key=lambda item: item[0])
    return records


def _process_cycle(cycle_num: int, record: dict, *, dry_run: bool) -> int:
    thinking = (record.get("thinking") or "").strip()
    if not thinking:
        print(f"Cycle {cycle_num}: skipped (no thinking text)")
        return 0

    print(f"\nCycle {cycle_num}: extracting link candidates…")
    candidates = link_resolver.extract_candidates_from_text(thinking)
    if not candidates:
        print(f"  no candidates found")
        return 0

    print(f"  found {len(candidates)} candidate(s)")
    report = link_resolver.resolve_and_merge_candidates(candidates, dry_run=dry_run)
    resolved = 0
    for item in report:
        status = item.get("status", "?")
        canonical = item.get("canonical_name", "")
        entry = item.get("entry")
        if entry:
            resolved += 1
            source = entry.get("source_name", "")
            print(f"  [{status}] {canonical} -> {source}")
        else:
            print(f"  [{status}] {canonical} (unresolved)")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract and resolve journal entity links")
    parser.add_argument("--cycle", type=int, help="Specific cycle number")
    parser.add_argument("--last", type=int, default=1, help="Process the N most recent cycles (default: 1)")
    parser.add_argument("--all", action="store_true", help="Process every archived cycle")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and resolve without writing data/link_dictionary.json",
    )
    args = parser.parse_args()

    if args.cycle is not None and (args.all or args.last != 1):
        print("Use only one of --cycle, --last, or --all.")
        sys.exit(1)

    records = _load_cycle_records(cycle=args.cycle, last=args.last, all_cycles=args.all)
    if not records:
        cycles_dir = DATA_DIR / "archive" / "cycles"
        if not cycles_dir.exists():
            print("No cycles archive found:", cycles_dir)
        elif args.cycle is not None:
            print(f"Cycle {args.cycle} not found or unreadable.")
        else:
            print(f"No cycles found in {cycles_dir}")
        sys.exit(1)

    mode = "dry-run" if args.dry_run else "write"
    print(f"Processing {len(records)} cycle(s) [{mode}]")

    total_resolved = 0
    for cycle_num, record in records:
        total_resolved += _process_cycle(cycle_num, record, dry_run=args.dry_run)

    print(f"\nDone. Resolved {total_resolved} entit(ies) across {len(records)} cycle(s).")
    if args.dry_run:
        print("(dry-run: dictionary not written)")


if __name__ == "__main__":
    main()
