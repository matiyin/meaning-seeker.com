"""
Phase C: Image generation via OpenRouter.

Uses the same OPENROUTER_API_KEY as the main inquiry model — no separate key needed.
OpenRouter's image generation endpoint mirrors the chat completions API with
modalities=["image", "text"], returning base64-encoded images in the response.

Called from the orchestrator after each cycle when image_decision.create is True.
Saves images to data/images/{cycle:06d}-img-001.<ext>.
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import API_BASE_URL, API_KEY, DATA_DIR, IMAGE_MODEL

if TYPE_CHECKING:
    from .models import ImageDecision

logger = logging.getLogger(__name__)

# OpenRouter image generation endpoint (same as chat completions)
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


# ByteDance Seedream has 4K context; long prompts can cause 400
_SEEDREAM_PROMPT_MAX_CHARS = 2000


def _image_request_payload(final_prompt: str) -> dict:
    """Build chat/completions payload for image generation.
    Google image models (e.g. gemini-2.5-flash-image, gemini-3-pro-image-preview) use
    modalities ['image','text'] and image_config.aspect_ratio. All others (Flux, Seedream, etc.)
    use modalities ['image'] only and no image_config.
    Seedream has a small context window; prompt is truncated for bytedance-seed.
    """
    content = final_prompt
    if "bytedance-seed" in IMAGE_MODEL or "seedream" in IMAGE_MODEL.lower():
        if len(content) > _SEEDREAM_PROMPT_MAX_CHARS:
            content = content[:_SEEDREAM_PROMPT_MAX_CHARS].rsplit(" ", 1)[0] + "."
            logger.debug("Truncated prompt to %d chars for Seedream", len(content))
    payload = {
        "model": IMAGE_MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    if IMAGE_MODEL.startswith("google/"):
        payload["modalities"] = ["image", "text"]
        payload["image_config"] = {"aspect_ratio": "1:1"}
    else:
        payload["modalities"] = ["image"]
    return payload


def _build_generation_prompt(image_decision: "ImageDecision") -> str:
    """Build the final generation prompt from structured visual-expression metadata.

    The prompt field is the primary instruction. The surrounding fields (visual_energy,
    texture, palette, temperature) add sensory context that the image model can use.
    Falls back gracefully to the raw prompt for old cycle records or FORCE_IMAGE.
    """
    d = image_decision

    # Rich path: new visual-expression fields are present
    if d.visual_energy or d.texture or d.temperature:
        parts: list[str] = []

        parts.append("Create an ABSTRACT, FANTASTICAL, NON-PHOTOREALISTIC painting or vision.")

        if d.prompt:
            parts.append(d.prompt.strip())

        sensory: list[str] = []
        if d.visual_energy:
            sensory.append(f"Visual energy: {d.visual_energy}")
        if d.texture:
            sensory.append(f"Surface texture: {d.texture}")
        if d.palette:
            sensory.append(f"Colour: {d.palette}")
        if d.temperature:
            sensory.append(f"Temperature: {d.temperature}")
        if sensory:
            parts.append(". ".join(sensory) + ".")

        parts.append("Style: abstract, fantastical, painterly, non-realistic. Not a photograph. Not a texture close-up. An invented visual world.")
        parts.append("Do not reproduce any named artist's style.")
        parts.append("No text or words anywhere in the image.")
        return " ".join(parts)

    # Legacy path: old-style fields (style/medium/artist_reference)
    if d.style or d.medium or d.artist_reference:
        parts = []
        if d.concept:
            parts.append(d.concept.rstrip(".") + ".")
        if d.prompt:
            parts.append(d.prompt.strip())
        parts.append("No text or words anywhere in the image.")
        return " ".join(parts)

    # Minimal fallback: raw prompt only
    raw = (d.prompt or "").strip()
    if raw:
        return f"{raw} No text or words anywhere in the image."

    return "A contemplative philosophical visualization. No text or words in the image."


def generate_image(cycle: int, image_decision: "ImageDecision") -> Optional[Path]:
    """Generate an art-directed image via OpenRouter.

    Accepts a full ImageDecision object so it can compose a rich prompt
    from the structured art-direction metadata.
    Returns the saved file path, or None if generation is skipped/fails.
    """
    if not API_KEY:
        logger.warning("Cycle %s: API_KEY not set, skipping image generation", cycle)
        return None

    final_prompt = _build_generation_prompt(image_decision)
    logger.debug("Cycle %s: image prompt: %s", cycle, final_prompt[:200])

    try:
        import httpx

        # Always use OpenRouter for image generation — it handles provider routing
        base = OPENROUTER_BASE.rstrip("/")

        payload = _image_request_payload(final_prompt)
        response = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120.0,
        )
        if response.status_code >= 400:
            body = response.text
            try:
                err = response.json()
                body = err.get("error", {}).get("message", body) or str(err)
            except Exception:
                pass
            logger.error("Cycle %s: image API %s — %s", cycle, response.status_code, body)
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
