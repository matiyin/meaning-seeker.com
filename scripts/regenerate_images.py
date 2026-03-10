#!/usr/bin/env python3
"""
Re-run image art direction + generation for all existing cycles using the current
engine prompt and image pipeline. Useful for A/B testing prompt changes.

For each cycle that has journal thinking, this script:
1. Sends the cycle's thinking to the inquiry model asking ONLY for an image_decision
2. Generates the image using the current images.py pipeline
3. Saves results to data/image-tests/<run_id>/

Usage:
  .venv/bin/python scripts/regenerate_images.py              # all cycles
  .venv/bin/python scripts/regenerate_images.py --cycles 5,10,13
  .venv/bin/python scripts/regenerate_images.py --last 4      # last 4 cycles
  .venv/bin/python scripts/regenerate_images.py --dry-run     # show prompts only, no generation
  .venv/bin/python scripts/regenerate_images.py --replace    # overwrite production images and metadata
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.art_direction import ART_DIRECTION_PROMPT
from src.config import API_BASE_URL, API_KEY, DATA_DIR, IMAGE_MODEL, MODEL_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def load_cycles(cycles_dir: Path) -> list[dict]:
    results = []
    for path in sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("thinking"):
                results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def get_art_direction(thinking: str, title: str, cycle: int) -> dict | None:
    from openai import OpenAI

    client = OpenAI(api_key=API_KEY, base_url=API_BASE_URL or OPENROUTER_BASE)

    user_msg = f"## Journal Entry: {title}\n\n{thinking}"

    response = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": ART_DIRECTION_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
        temperature=1.0,
    )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return None

    import re
    text_to_parse = raw
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
        if match:
            text_to_parse = match.group(1)

    try:
        return json.loads(text_to_parse)
    except json.JSONDecodeError as e:
        logger.warning("Cycle %s: failed to parse art direction: %s", cycle, e)
        return None


def generate_image(prompt: str, output_path: Path) -> bool:
    import httpx
    import base64
    import re

    base = OPENROUTER_BASE.rstrip("/")
    content = prompt
    if "bytedance-seed" in IMAGE_MODEL or "seedream" in IMAGE_MODEL.lower():
        max_chars = 2000
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(" ", 1)[0] + "."
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
        logger.error("Image API %s — %s", response.status_code, body)
        response.raise_for_status()
    data = response.json()

    try:
        image_entry = data["choices"][0]["message"]["images"][0]
        data_url: str = image_entry["image_url"]["url"]
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("Unexpected image response: %s", e)
        return False

    match = re.match(r"data:image/(\w+);base64,(.+)", data_url, re.DOTALL)
    if not match:
        logger.warning("Image URL is not a base64 data URI")
        return False

    ext = match.group(1).lower()
    image_bytes = base64.b64decode(match.group(2))

    final_path = output_path.with_suffix(f".{ext}")
    final_path.write_bytes(image_bytes)
    logger.info("Saved: %s (%d KB)", final_path.name, len(image_bytes) // 1024)
    return True


def build_final_prompt(decision: dict) -> str:
    """Mirror the logic of images._build_generation_prompt for the new fields."""
    parts: list[str] = []

    prompt = (decision.get("prompt") or "").strip()
    if prompt:
        parts.append(prompt)

    sensory: list[str] = []
    if decision.get("visual_energy"):
        sensory.append(f"Visual energy: {decision['visual_energy']}")
    if decision.get("texture"):
        sensory.append(f"Surface texture: {decision['texture']}")
    if decision.get("palette"):
        sensory.append(f"Colour: {decision['palette']}")
    if decision.get("temperature"):
        sensory.append(f"Temperature: {decision['temperature']}")
    if sensory:
        parts.append(". ".join(sensory) + ".")

    parts.append("Do not reproduce any named artist's style.")
    parts.append("No text or words anywhere in the image.")
    return " ".join(parts)


def _update_cycle_record(cycle: int, image_decision: dict) -> None:
    """Update cycle JSON with new image_decision."""
    path = DATA_DIR / "archive" / "cycles" / f"{cycle:06d}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["image_decision"] = image_decision
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Updated cycle record: %s", path.name)


def _update_journal_image(cycle: int, image_filename: str) -> None:
    """Replace or add image reference in journal MD."""
    import re

    path = DATA_DIR / "archive" / "journal" / f"{cycle:06d}.md"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    img_line = f"![Cycle {cycle} visualization](/images/{image_filename})"
    pattern = r"\n*!\[[^\]]*\]\(/images/[^)]+\)\n*"
    if re.search(pattern, content):
        content = re.sub(pattern, f"\n\n{img_line}\n\n", content, count=1)
    else:
        # No image yet — insert before "### New tensions" or similar
        match = re.search(r"\n---\n\n### ", content)
        if match:
            content = content[: match.start()] + f"\n\n{img_line}\n\n" + content[match.start() :]
        else:
            content = content.rstrip() + "\n\n" + img_line + "\n"
    path.write_text(content, encoding="utf-8")
    logger.info("Updated journal: %s", path.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-generate images for existing cycles")
    parser.add_argument("--cycles", type=str, help="Comma-separated cycle numbers (e.g. 5,10,13)")
    parser.add_argument("--last", type=int, help="Only process the last N cycles")
    parser.add_argument("--dry-run", action="store_true", help="Show art direction + prompts only, skip image generation")
    parser.add_argument("--replace", action="store_true", help="Overwrite production images in data/images/ and update cycle records + journal MD")
    parser.add_argument("--run-id", type=str, help="Custom run ID (default: timestamp)")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: No API key set. Set OPENROUTER_API_KEY or OPENAI_API_KEY in .env")
        sys.exit(1)

    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        print(f"No cycles directory: {cycles_dir}")
        sys.exit(1)

    all_cycles = load_cycles(cycles_dir)
    if not all_cycles:
        print("No cycles with thinking found")
        sys.exit(1)

    if args.cycles:
        selected = set(int(c.strip()) for c in args.cycles.split(","))
        cycles = [c for c in all_cycles if c.get("cycle") in selected]
    elif args.last:
        cycles = all_cycles[-args.last:]
    else:
        cycles = all_cycles

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    replace_mode = args.replace and not args.dry_run
    meta_dir = DATA_DIR / "image-tests" / run_id
    image_dir = DATA_DIR / "images" if replace_mode else meta_dir
    meta_dir.mkdir(parents=True, exist_ok=True)
    if replace_mode:
        (DATA_DIR / "images").mkdir(parents=True, exist_ok=True)

    run_meta = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inquiry_model": MODEL_ID,
        "image_model": IMAGE_MODEL,
        "dry_run": args.dry_run,
        "replace": replace_mode,
        "cycles_count": len(cycles),
        "cycle_numbers": [c.get("cycle") for c in cycles],
    }
    (meta_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    print(f"\nRun: {run_id}")
    print(f"Output: {meta_dir}")
    if replace_mode:
        print(f"REPLACE MODE: images → {image_dir}")
    print(f"Inquiry model: {MODEL_ID}")
    print(f"Image model: {IMAGE_MODEL}")
    print(f"Cycles: {len(cycles)} ({', '.join(str(c.get('cycle')) for c in cycles)})")
    print(f"Dry run: {args.dry_run}")
    print()

    for cycle_data in cycles:
        cycle_num = cycle_data.get("cycle", "?")
        title = cycle_data.get("title", "")
        thinking = cycle_data.get("thinking", "")

        print(f"── Cycle {cycle_num}: {title} ──")

        logger.info("Cycle %s: requesting art direction from %s", cycle_num, MODEL_ID)
        decision = get_art_direction(thinking, title, cycle_num)

        if not decision:
            print(f"  SKIP: no art direction returned\n")
            continue

        print(f"  beyond_words: {decision.get('beyond_words', '(none)')}")
        print(f"  visual_energy: {decision.get('visual_energy', '(none)')}")
        print(f"  texture: {decision.get('texture', '(none)')}")
        print(f"  palette: {decision.get('palette', '(none)')}")
        print(f"  temperature: {decision.get('temperature', '(none)')}")

        final_prompt = build_final_prompt(decision)
        print(f"  prompt ({len(final_prompt)} chars): {final_prompt[:200]}...")

        cycle_result = {
            "cycle": cycle_num,
            "title": title,
            "art_direction": decision,
            "final_prompt": final_prompt,
            "image_generated": False,
        }

        if not args.dry_run:
            image_path = image_dir / f"{cycle_num:06d}-img-001"
            if replace_mode:
                for old in image_dir.glob(f"{cycle_num:06d}-img-001.*"):
                    old.unlink()
                    logger.info("Removed old image: %s", old.name)
            try:
                ok = generate_image(final_prompt, image_path)
                cycle_result["image_generated"] = ok
                if ok:
                    print(f"  IMAGE: saved")
                    if replace_mode:
                        _update_cycle_record(cycle_num, decision)
                        saved_path = next(image_dir.glob(f"{cycle_num:06d}-img-001.*"), None)
                        if saved_path:
                            _update_journal_image(cycle_num, saved_path.name)
                else:
                    print(f"  IMAGE: generation returned no image")
            except Exception as e:
                print(f"  IMAGE: failed — {e}")
                cycle_result["error"] = str(e)

            time.sleep(2)

        result_path = meta_dir / f"{cycle_num:06d}-decision.json"
        result_path.write_text(json.dumps(cycle_result, indent=2), encoding="utf-8")
        print()

    print(f"Done. Results in: {meta_dir}")


if __name__ == "__main__":
    main()
