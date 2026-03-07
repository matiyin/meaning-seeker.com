#!/usr/bin/env python3
"""
Post the most recent cycle that has an image to configured social platforms.
Uses that cycle's social_output text and image (Bluesky/Threads get text; Instagram gets text + image).

Usage (from repo root):
  .venv/bin/python scripts/post_last_cycle_with_image.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR
from src import social


def main() -> None:
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        print("No cycles archive found:", cycles_dir)
        sys.exit(1)
    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)
    record = None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        image_path_raw = data.get("image_path")
        social_output = data.get("social_output")
        if not image_path_raw or not social_output:
            continue
        # Resolve image path: stored as "data/images/NNNNNN-img-001.png" or relative to DATA_DIR
        if image_path_raw.startswith("data/"):
            image_path = DATA_DIR.parent / image_path_raw
        else:
            image_path = DATA_DIR / image_path_raw
        if not image_path.exists():
            image_path = DATA_DIR / "images" / Path(image_path_raw).name
        if not image_path.exists():
            print("Image file not found:", image_path_raw)
            continue
        record = data
        cycle = record.get("cycle", path.stem)
        text = social_output.strip()
        title = record.get("title") or ""
        print(f"Cycle {cycle}: posting {len(text)} chars + image {image_path.name}")
        results = social.post_social(
            text, image_path=image_path, cycle=int(cycle), title=title
        )
        if "blocked" in results:
            print("Blocked by moderation:", results["blocked"])
            sys.exit(1)
        for platform, r in results.items():
            if isinstance(r, dict) and r.get("ok"):
                print(f"  {platform}: OK", r.get("tweet_id") or r.get("uri") or r.get("post_id"))
            elif isinstance(r, dict) and not r.get("ok"):
                print(f"  {platform}: failed", r.get("error", r))
        if not any(isinstance(r, dict) and r.get("ok") for r in results.values()):
            sys.exit(1)
        return
    print("No cycle with both image_path and social_output found.")
    sys.exit(1)


if __name__ == "__main__":
    main()
