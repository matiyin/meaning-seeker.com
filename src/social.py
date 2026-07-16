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
    SITE_URL,
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

# X counts every HTTP(S) URL as a fixed 23 characters (t.co), regardless of length.
_X_URL_WEIGHTED_LEN = 23


def _format_post(
    text: str,
    cycle: int,
    limit: int,
    *,
    url_weighted_len: int | None = None,
) -> str:
    """Wrap the quote in curly quotes and append a journal link + tagline.

    The journal URL is always kept when available. If the full post does not fit,
    drop the tagline first, then truncate the quote — never drop the link.

    Args:
        url_weighted_len: If set (e.g. 23 for X), count the journal URL as this
            many characters for the length budget instead of its raw length.
    """
    journal_url = f"{SITE_URL.rstrip('/')}/journal/{cycle}" if cycle and SITE_URL else ""
    tagline = "An AI doing philosophy in public — one cycle at a time."

    def _build(quote: str, include_url: bool, include_tagline: bool) -> str:
        parts = [f"\u201c{quote}\u201d"]
        if include_url and journal_url:
            parts.append(journal_url)
        if include_tagline:
            parts.append(tagline)
        return "\n\n".join(parts)

    def _weighted_len(candidate: str, include_url: bool) -> int:
        n = len(candidate)
        if include_url and journal_url and url_weighted_len is not None:
            n = n - len(journal_url) + url_weighted_len
        return n

    # Prefer: quote + url + tagline → quote + url → truncated quote + url
    # (URL is never dropped when present.)
    for include_tagline in (True, False):
        include_url = bool(journal_url)
        candidate = _build(text, include_url=include_url, include_tagline=include_tagline)
        if _weighted_len(candidate, include_url) <= limit:
            return candidate

    # Truncate quote so URL (and curly quotes / separators) still fit.
    include_url = bool(journal_url)
    empty = _build("", include_url=include_url, include_tagline=False)
    empty_weight = _weighted_len(empty, include_url)
    # Leave room for an ellipsis if we truncate mid-word.
    max_quote = max(0, limit - empty_weight - 1)
    quote = text[:max_quote].rstrip()
    if len(quote) < len(text):
        quote = quote.rsplit(" ", 1)[0].rstrip() if " " in quote else quote
        quote = quote.rstrip(".,;:—-") + "…"
        # Re-fit if ellipsis pushed us over (rare).
        while quote and _weighted_len(_build(quote, include_url, False), include_url) > limit:
            quote = quote[:-2].rstrip() + "…" if len(quote) > 1 else ""
    return _build(quote, include_url=include_url, include_tagline=False)


def _bluesky_link_facets(text: str, journal_url: str):
    """Build Bluesky facets so the journal URL is a clickable link. Returns list or None."""
    if not journal_url or journal_url not in text:
        return None
    from atproto import models
    pos = text.index(journal_url)
    byte_start = len(text[:pos].encode("utf-8"))
    byte_end = len((text[:pos] + journal_url).encode("utf-8"))
    return [
        models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(byte_start=byte_start, byte_end=byte_end),
            features=[models.AppBskyRichtextFacet.Link(uri=journal_url)],
        )
    ]


def _post_x(text: str, cycle: int = 0, image_path: Optional[Path] = None) -> dict:
    try:
        import tweepy
        auth = tweepy.OAuth1UserHandler(
            X_API_KEY,
            X_API_SECRET,
            X_ACCESS_TOKEN,
            X_ACCESS_SECRET,
        )
        client = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_SECRET,
        )
        formatted = _format_post(
            text, cycle=cycle, limit=280, url_weighted_len=_X_URL_WEIGHTED_LEN
        )

        media_ids = None
        if image_path and image_path.exists():
            media = tweepy.API(auth).media_upload(filename=str(image_path))
            media_ids = [media.media_id_string]
            logger.info("X: uploaded media %s", media.media_id_string)

        response = client.create_tweet(text=formatted, media_ids=media_ids)
        tweet_id = response.data["id"] if response.data else None
        logger.info("X: posted tweet %s", tweet_id)
        return {"ok": True, "tweet_id": tweet_id}
    except Exception as e:
        logger.error("X: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


# Bluesky image blob limit (API allows ~976KB)
BLUESKY_IMAGE_MAX_BYTES = 950 * 1024


def _resize_image_for_bluesky(image_bytes: bytes) -> bytes:
    """Resize/compress image to fit Bluesky blob limit. Returns JPEG bytes."""
    from io import BytesIO
    from PIL import Image
    target = BLUESKY_IMAGE_MAX_BYTES
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    out = BytesIO()
    for quality in (85, 70, 55, 40):
        out.seek(0)
        out.truncate(0)
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= target:
            return out.getvalue()
    # Still too large: resize by half and try again
    w, h = img.size
    img = img.resize((w // 2, h // 2), Image.Resampling.LANCZOS)
    for quality in (75, 60, 45, 30):
        out.seek(0)
        out.truncate(0)
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= target:
            return out.getvalue()
    # last resort: smallest size
    out.seek(0)
    out.truncate(0)
    img.save(out, "JPEG", quality=25, optimize=True)
    return out.getvalue()


def _post_bluesky(
    text: str,
    image_path: Optional[Path] = None,
    cycle: int = 0,
    image_alt: Optional[str] = None,
) -> dict:
    try:
        from atproto import Client
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        formatted = _format_post(text, cycle=cycle, limit=300)
        journal_url = f"{SITE_URL.rstrip('/')}/journal/{cycle}" if cycle and SITE_URL else ""
        facets = _bluesky_link_facets(formatted, journal_url)
        if image_path and image_path.exists():
            image_bytes = image_path.read_bytes()
            if len(image_bytes) > BLUESKY_IMAGE_MAX_BYTES:
                image_bytes = _resize_image_for_bluesky(image_bytes)
            alt = image_alt or "Meaning Seeker cycle image"
            post = client.send_image(
                text=formatted,
                image=image_bytes,
                image_alt=alt,
                facets=facets,
            )
        else:
            post = client.send_post(text=formatted, facets=facets)
        logger.info("Bluesky: posted %s", post.uri)
        return {"ok": True, "uri": post.uri}
    except Exception as e:
        logger.error("Bluesky: posting failed: %s", e)
        return {"ok": False, "error": str(e)}


def _post_threads(text: str, cycle: int = 0) -> dict:
    """Post to Threads using Meta's Threads API via httpx."""
    try:
        import httpx
        base = "https://graph.threads.net/v1.0"
        formatted = _format_post(text, cycle=cycle, limit=500)
        # Step 1: Create media container
        r1 = httpx.post(
            f"{base}/{THREADS_USER_ID}/threads",
            params={
                "media_type": "TEXT",
                "text": formatted,
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
    title: Optional[str] = None,
) -> dict:
    """Post to all configured platforms. Returns dict of {platform: result}.

    Args:
        text: The philosophical text to post.
        image_path: Optional path to a generated image (used for Bluesky, Instagram, X).
        cycle: Cycle number for logging.
        title: Cycle title (same as gallery caption under image); used for image alt text.
    """
    results: dict = {}
    # Same as gallery: caption under image is title or "Cycle N"
    image_alt = (title or (f"Cycle {cycle}" if cycle else "Meaning Seeker cycle image"))

    # Moderation check first
    if SOCIAL_MODERATION_ENABLED:
        safe, reason = _moderate(text)
        if not safe:
            logger.warning(
                "Cycle %s: social output blocked by moderation: %s", cycle, reason
            )
            return {"blocked": reason}

    if X_API_KEY and X_ACCESS_TOKEN:
        results["x"] = _post_x(text, cycle=cycle, image_path=image_path)

    if BLUESKY_HANDLE and BLUESKY_PASSWORD:
        results["bluesky"] = _post_bluesky(
            text, image_path=image_path, cycle=cycle, image_alt=image_alt
        )

    if THREADS_ACCESS_TOKEN and THREADS_USER_ID:
        results["threads"] = _post_threads(text, cycle=cycle)

    if INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID and image_path:
        results["instagram"] = _post_instagram(text, image_path)

    if not results:
        logger.info("Cycle %s: no social platforms configured", cycle)

    return results
