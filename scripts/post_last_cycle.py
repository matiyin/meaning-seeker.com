#!/usr/bin/env python3
"""
Post cycle(s) with social_output to configured social platforms.

Usage (from repo root):
  .venv/bin/python scripts/post_last_cycle.py
  .venv/bin/python scripts/post_last_cycle.py --last 3
  .venv/bin/python scripts/post_last_cycle.py --cycle 42
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR
from src import social


def _resolve_image_path(image_path_raw: str) -> Path | None:
    if not image_path_raw:
        return None
    if image_path_raw.startswith("data/"):
        image_path = DATA_DIR.parent / image_path_raw
    else:
        image_path = DATA_DIR / image_path_raw
    if image_path.exists():
        return image_path
    fallback = DATA_DIR / "images" / Path(image_path_raw).name
    return fallback if fallback.exists() else None


def _load_cycle_records(*, cycle: int | None, last: int) -> list[tuple[dict, Path]]:
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return []

    if cycle is not None:
        path = cycles_dir / f"{cycle:06d}.json"
        if not path.exists():
            return []
        try:
            return [(json.loads(path.read_text(encoding="utf-8")), path)]
        except (json.JSONDecodeError, OSError):
            return []

    paths = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)
    if last > 0:
        paths = paths[:last]

    records: list[tuple[dict, Path]] = []
    for path in paths:
        try:
            records.append((json.loads(path.read_text(encoding="utf-8")), path))
        except (json.JSONDecodeError, OSError):
            continue
    # Post oldest-first so feeds read chronologically.
    records.sort(key=lambda item: int(item[0].get("cycle", item[1].stem)))
    return records


def _post_record(record: dict, path: Path) -> bool:
    cycle = int(record.get("cycle", path.stem))
    text = (record.get("social_output") or "").strip()
    if not text:
        print(f"Cycle {cycle}: skipped (no social_output)")
        return True

    image_path = _resolve_image_path(record.get("image_path") or "")
    title = record.get("title") or ""

    if image_path:
        print(f"Cycle {cycle}: posting {len(text)} chars + image {image_path.name}")
    else:
        print(f"Cycle {cycle}: posting {len(text)} chars (no image)")

    results = social.post_social(
        text, image_path=image_path, cycle=cycle, title=title
    )
    if "blocked" in results:
        print(f"  blocked by moderation: {results['blocked']}")
        return False

    ok_any = False
    for platform, r in results.items():
        if isinstance(r, dict) and r.get("ok"):
            ok_any = True
            print(f"  {platform}: OK", r.get("tweet_id") or r.get("uri") or r.get("post_id"))
        elif isinstance(r, dict) and not r.get("ok"):
            print(f"  {platform}: failed", r.get("error", r))
    if not ok_any:
        print("  no platform succeeded")
    return ok_any


def main() -> None:
    parser = argparse.ArgumentParser(description="Post cycle(s) to social platforms")
    parser.add_argument("--cycle", type=int, help="Specific cycle number")
    parser.add_argument(
        "--last",
        type=int,
        default=1,
        help="Post the N most recent cycles (default: 1)",
    )
    args = parser.parse_args()

    if args.cycle is not None and args.last != 1:
        print("Use either --cycle or --last, not both.")
        sys.exit(1)

    records = _load_cycle_records(cycle=args.cycle, last=args.last)
    if not records:
        cycles_dir = DATA_DIR / "archive" / "cycles"
        if not cycles_dir.exists():
            print("No cycles archive found:", cycles_dir)
        elif args.cycle is not None:
            print(f"Cycle {args.cycle} not found or unreadable.")
        else:
            print(f"No cycles found in {cycles_dir}")
        sys.exit(1)

    any_ok = False
    for record, path in records:
        if _post_record(record, path):
            any_ok = True

    if not any_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
