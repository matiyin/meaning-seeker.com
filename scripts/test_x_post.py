#!/usr/bin/env python3
"""
Test X (Twitter) posting without running a full cycle.

Usage (from repo root):
  python scripts/test_x_post.py              # Post one real test tweet
  python scripts/test_x_post.py --dry-run    # Only verify credentials (no post)

Requires .env with X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow importing from src when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import X_ACCESS_SECRET, X_ACCESS_TOKEN, X_API_KEY, X_API_SECRET
from src import social


def dry_run() -> bool:
    """Verify X credentials by calling get_me(). Returns True if OK."""
    if not (X_API_KEY and X_ACCESS_TOKEN):
        print("Missing X credentials (X_API_KEY, X_ACCESS_TOKEN, etc.). Check .env")
        return False
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_SECRET,
        )
        me = client.get_me()
        if me.data:
            print(f"Credentials OK. Account: @{me.data.username} (id={me.data.id})")
            return True
        print("Credentials responded but no user data.")
        return False
    except Exception as e:
        print(f"Credentials check failed: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Test X posting")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only verify credentials; do not post",
    )
    parser.add_argument(
        "--message",
        default="Test post from Meaning Seeker — please ignore. (testing X integration)",
        help="Custom message to post (default: test message)",
    )
    args = parser.parse_args()

    if args.dry_run:
        ok = dry_run()
        sys.exit(0 if ok else 1)
    else:
        if not (X_API_KEY and X_ACCESS_TOKEN):
            print("Missing X credentials. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET in .env")
            sys.exit(1)
        text = args.message[:280]
        print(f"Posting to X ({len(text)} chars): {text[:60]}...")
        results = social.post_social(text, image_path=None, cycle=0)
        if "blocked" in results:
            print("Post was blocked by moderation:", results["blocked"])
            sys.exit(1)
        if "x" in results:
            r = results["x"]
            if r.get("ok"):
                print("Posted successfully. Tweet ID:", r.get("tweet_id"))
            else:
                print("X post failed:", r.get("error", r))
                sys.exit(1)
        else:
            print("No X result in response:", results)
            sys.exit(1)


if __name__ == "__main__":
    main()
