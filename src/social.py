"""
Phase C: Social media posting (X, Bluesky, Threads, Instagram).

Uses a local-first moderation approach:
1. Hard keyword/regex blocklist → block immediately (no API call)
2. Soft flags (death, violence, etc.) → single Haiku call to disambiguate
3. Obviously safe → post directly

Haiku budget: 0-1 calls per cycle (only for ambiguous content with soft flags).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .config import (
    API_BASE_URL,
    API_KEY,
    BLUESKY_HANDLE,
    BLUESKY_PASSWORD,
    FILTER_MODEL_ID,
    INSTAGRAM_ACCESS_TOKEN,
    INSTAGRAM_USER_ID,
    SOCIAL_MODERATION_ENABLED,
    THREADS_ACCESS_TOKEN,
    THREADS_USER_ID,
    X_ACCESS_SECRET,
    X_ACCESS_TOKEN,
    X_API_KEY,
    X_API_SECRET,
)

logger = logging.getLogger(__name__)

# ── Local moderation ───────────────────────────────────────────────────────────

_HARD_BLOCK_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(kill|murder|shoot|stab|bomb|attack|threaten)\s+(yourself|myself|someone|people|them|him|her|you)\b", re.I),
    re.compile(r"(how\s+to|ways\s+to)\s+(harm|hurt|kill|die)\b", re.I),
    re.compile(r"\b(child|minor|kid|underage)\s+(sex|nude|naked|explicit|porn)\b", re.I),
    re.compile(r"\b(doxx|doxing|personal\s+info|home\s+address)\b", re.I),
    re.compile(r"\bkys\b", re.I),
    re.compile(r"go\s+(kill|hang|rope)\s+yourself", re.I),
]

_SOFT_FLAG_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(suicide|suicidal|self.harm|cutting|overdose)\b", re.I),
    re.compile(r"\b(death|dying|dead|die|killing)\b", re.I),
    re.compile(r"\b(violence|violent|hurt|pain|suffer)\b", re.I),
]


def _local_moderation_check(text: str) -> tuple[bool, str]:
    """Returns (blocked, reason). Blocked=True means unsafe."""
    for pattern in _HARD_BLOCK_PATTERNS:
        if pattern.search(text):
            return True, f"hard_block: {pattern.pattern[:50]}"
    return False, ""


def _has_soft_flags(text: str) -> bool:
    return any(p.search(text) for p in _SOFT_FLAG_PATTERNS)


def _haiku_moderate(text: str) -> tuple[bool, str]:
    """Single Haiku call for ambiguous content. Returns (safe, reason)."""
    if not API_KEY:
        return True, ""  # can't check, allow
    try:
        import json as _json
        from openai import OpenAI
        kwargs = {"api_key": API_KEY, "timeout": 30.0}
        if API_BASE_URL:
            kwargs["base_url"] = API_BASE_URL
        client = OpenAI(**kwargs)
        prompt = f"""Is this philosophical text safe to post on social media?
Check only for: harmful content directed at real people, dangerous instructions, dangerous self-harm encouragement.
This is from a philosophical AI art project — philosophical discussion of mortality, suffering, and meaning is expected and OK.
Text: {text}
Reply JSON only: {{"safe": true, "reason": ""}}"""
        response = client.chat.completions.create(
            model=FILTER_MODEL_ID,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (response.choices[0].message.content or "").strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            data = _json.loads(json_match.group())
            return data.get("safe", True), data.get("reason", "")
    except Exception as e:
        logger.warning("social moderation Haiku check failed: %s", e)
    return True, ""  # fail-open


def _moderate(text: str) -> tuple[bool, str]:
    """Multi-tier moderation. Returns (safe, reason). Safe=True means OK to post."""
    # Tier 1: hard local blocklist (no API call)
    blocked, reason = _local_moderation_check(text)
    if blocked:
        return False, reason

    # Tier 2: only call Haiku for content with soft flags (death, violence in context)
    if not _has_soft_flags(text):
        return True, ""

    safe, reason = _haiku_moderate(text)
    return safe, reason


# ── Platform implementations ───────────────────────────────────────────────────

def _post_x(text: str) -> dict:
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_SECRET,
        )
        response = client.create_tweet(text=text[:280])
        tweet_id = response.data["id"] if response.data else None
        logger.info("X: posted tweet %s", tweet_id)
        return {"ok": True, "tweet_id": tweet_id}
    except Exception as e:
        logger.error("X: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


def _post_bluesky(text: str) -> dict:
    try:
        from atproto import Client
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        post = client.send_post(text=text[:300])
        logger.info("Bluesky: posted %s", post.uri)
        return {"ok": True, "uri": post.uri}
    except Exception as e:
        logger.error("Bluesky: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


def _post_threads(text: str) -> dict:
    """Post to Threads using Meta's Threads API via httpx."""
    try:
        import httpx
        base = "https://graph.threads.net/v1.0"
        # Step 1: Create media container
        r1 = httpx.post(
            f"{base}/{THREADS_USER_ID}/threads",
            params={
                "media_type": "TEXT",
                "text": text[:500],
                "access_token": THREADS_ACCESS_TOKEN,
            },
            timeout=30,
        )
        r1.raise_for_status()
        container_id = r1.json().get("id")
        if not container_id:
            return {"ok": False, "error": "No container ID returned"}

        # Step 2: Publish
        r2 = httpx.post(
            f"{base}/{THREADS_USER_ID}/threads_publish",
            params={
                "creation_id": container_id,
                "access_token": THREADS_ACCESS_TOKEN,
            },
            timeout=30,
        )
        r2.raise_for_status()
        post_id = r2.json().get("id")
        logger.info("Threads: posted %s", post_id)
        return {"ok": True, "post_id": post_id}
    except Exception as e:
        logger.error("Threads: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


def _post_instagram(caption: str, image_path: Path) -> dict:
    """Post to Instagram using Meta Graph API. Requires image to be publicly accessible.

    Instagram requires a publicly accessible URL; we upload via a temp public URL.
    This implementation uses the Graph API with the image served from the web server.
    Requires SITE_URL to be set so Instagram can fetch the image.
    """
    try:
        import httpx
        from .config import SITE_URL

        if not SITE_URL:
            logger.warning("Instagram: SITE_URL not set, cannot post image")
            return {"ok": False, "error": "SITE_URL not set"}

        image_url = f"{SITE_URL.rstrip('/')}/images/{image_path.name}"
        base = "https://graph.instagram.com/v21.0"

        # Step 1: Create media container
        r1 = httpx.post(
            f"{base}/{INSTAGRAM_USER_ID}/media",
            params={
                "image_url": image_url,
                "caption": caption[:2200],
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=60,
        )
        r1.raise_for_status()
        container_id = r1.json().get("id")
        if not container_id:
            return {"ok": False, "error": "No container ID returned"}

        # Step 2: Publish
        r2 = httpx.post(
            f"{base}/{INSTAGRAM_USER_ID}/media_publish",
            params={
                "creation_id": container_id,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=30,
        )
        r2.raise_for_status()
        post_id = r2.json().get("id")
        logger.info("Instagram: posted %s", post_id)
        return {"ok": True, "post_id": post_id}
    except Exception as e:
        logger.error("Instagram: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── Main entry point ───────────────────────────────────────────────────────────

def post_social(
    text: str,
    image_path: Optional[Path] = None,
    cycle: int = 0,
) -> dict:
    """Post to all configured platforms. Returns dict of {platform: result}.

    Args:
        text: The philosophical text to post.
        image_path: Optional path to a generated image (used for Instagram).
        cycle: Cycle number for logging.
    """
    results: dict = {}

    # Moderation check first
    if SOCIAL_MODERATION_ENABLED:
        safe, reason = _moderate(text)
        if not safe:
            logger.warning(
                "Cycle %s: social output blocked by moderation: %s", cycle, reason
            )
            return {"blocked": reason}

    if X_API_KEY and X_ACCESS_TOKEN:
        results["x"] = _post_x(text)

    if BLUESKY_HANDLE and BLUESKY_PASSWORD:
        results["bluesky"] = _post_bluesky(text)

    if THREADS_ACCESS_TOKEN and THREADS_USER_ID:
        results["threads"] = _post_threads(text)

    if INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID and image_path:
        results["instagram"] = _post_instagram(text, image_path)

    if not results:
        logger.info("Cycle %s: no social platforms configured", cycle)

    return results
