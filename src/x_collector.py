"""
Phase C: X (Twitter) reply collection.

Pulls recent mentions/replies via the X API and stores them in the quarantine
directory as pending submissions. They will then be processed by the filtering
pipeline on the next orchestrator cycle.

State is tracked in data/x_collector_state.json to avoid re-processing tweets.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, X_BEARER_TOKEN

logger = logging.getLogger(__name__)

COLLECTOR_STATE_PATH = DATA_DIR / "x_collector_state.json"
QUARANTINE_DIR = DATA_DIR / "injections" / "quarantine"

# Minimum character count to consider a reply substantive
MIN_REPLY_LENGTH = 50


def _load_collector_state() -> dict:
    if not COLLECTOR_STATE_PATH.exists():
        return {"last_tweet_id": None, "collected_at": None}
    try:
        return json.loads(COLLECTOR_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_tweet_id": None, "collected_at": None}


def _save_collector_state(state: dict) -> None:
    COLLECTOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COLLECTOR_STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _is_substantive(text: str) -> bool:
    """Quick filter: skip obvious spam/praise/dismissal."""
    stripped = text.strip()
    if len(stripped) < MIN_REPLY_LENGTH:
        return False
    # Skip common non-substantive patterns
    lowered = stripped.lower()
    trivial = [
        "lol", "haha", "nice", "cool", "great", "awesome", "wow",
        "follow me", "check out my", "dm me", "click here",
    ]
    for t in trivial:
        if lowered == t or lowered.startswith(t + " ") or lowered.endswith(" " + t):
            return False
    return True


def collect_replies() -> int:
    """Pull recent mentions from X, store substantive ones in quarantine.

    Returns the count of new submissions added to quarantine.
    """
    if not X_BEARER_TOKEN:
        logger.debug("X_BEARER_TOKEN not set, skipping X reply collection")
        return 0

    try:
        import tweepy
    except ImportError:
        logger.warning("tweepy not installed, skipping X reply collection")
        return 0

    state = _load_collector_state()
    since_id: Optional[str] = state.get("last_tweet_id")

    try:
        client = tweepy.Client(bearer_token=X_BEARER_TOKEN)

        # Get authenticated user ID (needed for mentions lookup)
        # Note: mentions_timeline requires user context auth (OAuth 1a/2a), not just bearer
        # Using search_recent_tweets as fallback for bearer-token-only access
        me = client.get_me()
        if not me or not me.data:
            logger.warning("X collector: could not get authenticated user")
            return 0

        user_id = me.data.id

        # Fetch recent mentions
        kwargs: dict = {
            "id": user_id,
            "max_results": 20,
            "tweet_fields": ["id", "text", "created_at", "author_id"],
        }
        if since_id:
            kwargs["since_id"] = since_id

        response = client.get_users_mentions(**kwargs)
        if not response or not response.data:
            logger.info("X collector: no new mentions")
            return 0

        count = 0
        newest_id: Optional[str] = None

        for tweet in response.data:
            tweet_id = str(tweet.id)
            if newest_id is None:
                newest_id = tweet_id

            text = tweet.text or ""
            # Strip @mention from the start of reply
            text = text.lstrip().lstrip("@")
            # Remove our own handle if it starts the reply
            import re
            text = re.sub(r"^@\w+\s*", "", text).strip()

            if not _is_substantive(text):
                logger.debug("X collector: skipping non-substantive tweet %s", tweet_id)
                continue

            # Hash the tweet ID as pseudo-IP for consistency
            ip_hash = hashlib.sha256(f"x_tweet_{tweet_id}".encode()).hexdigest()

            submission = {
                "submission_id": f"sub-x-{tweet_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "text": text,
                "ip_hash": ip_hash,
                "source": "x_reply",
                "filter_scores": None,
                "status": "pending",
                "rejection_reason": None,
                "x_tweet_id": tweet_id,
            }

            QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
            path = QUARANTINE_DIR / f"{submission['submission_id']}.json"

            if path.exists():
                logger.debug("X collector: tweet %s already in quarantine", tweet_id)
                continue

            path.write_text(
                json.dumps(submission, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            count += 1
            logger.info("X collector: stored tweet %s", tweet_id)

        # Update state
        if newest_id:
            state["last_tweet_id"] = newest_id
            state["collected_at"] = datetime.now(timezone.utc).isoformat()
            _save_collector_state(state)

        logger.info("X collector: collected %d new replies", count)
        return count

    except Exception as e:
        logger.error("X collector: failed: %s", e)
        return 0
