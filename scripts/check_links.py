#!/usr/bin/env python3
"""
Validate URLs in the journal link dictionary and optionally apply curated seed fixes.

Usage (from repo root):
  .venv/bin/python scripts/check_links.py
  .venv/bin/python scripts/check_links.py --apply-seed
  .venv/bin/python scripts/check_links.py --only mourners-kaddish,wittgenstein
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import link_resolver


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate journal entity link URLs")
    parser.add_argument(
        "--apply-seed",
        action="store_true",
        help="Write curated seed URLs/summaries into data/link_dictionary.json before checking",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated dictionary keys to check (default: all)",
    )
    args = parser.parse_args()

    if args.apply_seed:
        updated = link_resolver.apply_seed_to_runtime_dictionary()
        print(f"Applied seed overrides to {updated} dictionary entr(ies).")

    dictionary = link_resolver.load_dictionary()
    keys = None
    if args.only:
        keys = {k.strip() for k in args.only.split(",") if k.strip()}

    results = link_resolver.validate_dictionary(dictionary)
    if keys is not None:
        results = [r for r in results if r["key"] in keys]

    ok_count = 0
    fail_count = 0
    for item in results:
        status = "OK" if item["ok"] else "FAIL"
        code = item["status_code"] if item["status_code"] is not None else "-"
        name = item["canonical_name"]
        url = item["url"]
        if item["ok"]:
            ok_count += 1
            print(f"[{status}] {item['key']} ({name}) — HTTP {code}")
            if item["final_url"] and item["final_url"] != url:
                print(f"        redirects to {item['final_url']}")
        else:
            fail_count += 1
            err = item["error"] or "unknown error"
            print(f"[{status}] {item['key']} ({name}) — {err}")
            print(f"        {url}")

    print(f"\nChecked {len(results)} URL(s): {ok_count} OK, {fail_count} failed.")
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
