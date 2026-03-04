"""
Phase C: Image generation via OpenRouter (FLUX models).

Uses the same OPENROUTER_API_KEY as the main inquiry model — no separate key needed.
OpenRouter's image generation endpoint mirrors the chat completions API with
modalities=["image"], returning base64-encoded images in the response.

Called from the orchestrator after each cycle when image_decision.create is True.
Saves images to data/images/{cycle:06d}-img-001.png.
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Optional

from .config import API_BASE_URL, API_KEY, DATA_DIR, IMAGE_MODEL

logger = logging.getLogger(__name__)

# OpenRouter image generation endpoint (same as chat completions)
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def generate_image(cycle: int, prompt: str) -> Optional[Path]:
    """Generate a philosophical visualization via OpenRouter FLUX.

    Returns the saved file path, or None if generation is skipped/fails.
    """
    if not API_KEY:
        logger.warning("Cycle %s: API_KEY not set, skipping image generation", cycle)
        return None

    enhanced_prompt = (
        f"Abstract philosophical visualization. {prompt}. "
        "Contemplative, minimal, evocative. No text or words in the image. "
        "Deep color, rich texture, meditative quality."
    )

    try:
        import httpx

        # Use OpenRouter base URL if configured, fall back to default OpenRouter endpoint
        base = (API_BASE_URL or OPENROUTER_BASE).rstrip("/")

        response = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": IMAGE_MODEL,
                "messages": [{"role": "user", "content": enhanced_prompt}],
                "modalities": ["image"],
                "stream": False,
            },
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()

        # Extract image from response
        # OpenRouter returns: choices[0].message.images[0].image_url.url (data URI)
        try:
            image_entry = data["choices"][0]["message"]["images"][0]
            data_url: str = image_entry["image_url"]["url"]
        except (KeyError, IndexError, TypeError) as e:
            logger.warning("Cycle %s: unexpected image response structure: %s | %s", cycle, e, data)
            return None

        # Parse data URI: "data:image/jpeg;base64,..."
        match = re.match(r"data:image/(\w+);base64,(.+)", data_url, re.DOTALL)
        if not match:
            logger.warning("Cycle %s: image URL is not a base64 data URI", cycle)
            return None

        ext = match.group(1).lower()
        image_bytes = base64.b64decode(match.group(2))

        images_dir = DATA_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{cycle:06d}-img-001.{ext}"
        filepath = images_dir / filename
        filepath.write_bytes(image_bytes)

        logger.info("Cycle %s: image saved to %s", cycle, filepath)
        return filepath

    except Exception as e:
        logger.error("Cycle %s: image generation failed: %s", cycle, e)
        return None
