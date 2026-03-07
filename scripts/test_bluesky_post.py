#!/usr/bin/env python3
"""
Test Bluesky posting without running a full cycle.

Usage (from repo root):
  .venv/bin/python scripts/test_bluesky_post.py              # Post one real test post
  .venv/bin/python scripts/test_bluesky_post.py --dry-run   # Only verify credentials (no post)

Requires .env with BLUESKY_HANDLE and BLUESKY_PASSWORD (app password).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BLUESKY_HANDLE, BLUESKY_PASSWORD
from src import social


def dry_run() -> bool:
    """Verify Bluesky credentials by logging in and fetching profile. Returns True if OK."""
    if not (BLUESKY_HANDLE and BLUESKY_PASSWORD):
        print("Missing Bluesky credentials (BLUESKY_HANDLE, BLUESKY_PASSWORD). Check .env")
        return False
    try:
        from atproto import Client
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        profile = client.get_profile(client.me.did)
        if profile:
            print(f"Credentials OK. Account: @{profile.handle} (did={profile.did})")
            return True
        print("Credentials responded but no profile data.")
        return False
    except Exception as e:
        print(f"Credentials check failed: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Bluesky posting")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only verify credentials; do not post",
    )
    parser.add_argument(
        "--message",
        default="Test post from Meaning Seeker — please ignore. (testing Bluesky integration)",
        help="Custom message to post (default: test message)",
    )
    args = parser.parse_args()

    if args.dry_run:
        ok = dry_run()
        sys.exit(0 if ok else 1)
    else:
        if not (BLUESKY_HANDLE and BLUESKY_PASSWORD):
            print("Missing Bluesky credentials. Set BLUESKY_HANDLE and BLUESKY_PASSWORD (app password) in .env")
            sys.exit(1)
        text = args.message[:300]
        print(f"Posting to Bluesky ({len(text)} chars): {text[:60]}...")
        results = social.post_social(text, image_path=None, cycle=0)
        if "blocked" in results:
            print("Post was blocked by moderation:", results["blocked"])
            sys.exit(1)
        if "bluesky" in results:
            r = results["bluesky"]
            if r.get("ok"):
                print("Posted successfully. URI:", r.get("uri"))
            else:
                print("Bluesky post failed:", r.get("error", r))
                sys.exit(1)
        else:
            print("No Bluesky result in response:", results)
            sys.exit(1)


if __name__ == "__main__":
    main()
