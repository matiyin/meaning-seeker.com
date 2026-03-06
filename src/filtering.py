"""
Phase C: Three-layer filtering pipeline for visitor submissions.

Layer 1: Hygiene check (pure Python, zero API calls)
Layers 2+3: Relevance scoring + distillation combined into ONE batched Haiku call

Haiku budget: 0-1 calls per cycle (only when pending submissions exist and hygiene passes).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, FILTER_MODEL_ID
from . import resistance

logger = logging.getLogger(__name__)

QUARANTINE_DIR = DATA_DIR / "injections" / "quarantine"

# ── Layer 1: Hygiene blocklists ────────────────────────────────────────────────
# Curated list: slurs, threat phrases, severe abuse. Avoids single words that
# appear in legitimate philosophical context (e.g. "die", "hate", "rape" alone).

def _toxicity_terms() -> frozenset[str]:
    raw = [
        # Racial/ethnic slurs
        "nigger", "nigga", "niggas", "negro", "negros",
        "kike", "kikes", "spic", "spics", "chink", "chinks", "gook", "gooks",
        "wetback", "wetbacks", "coon", "coons", "cracker", "crackers",
        "paki", "pakki", "raghead", "ragheads", "towelhead", "towelheads",
        "beaner", "beaners",
        # LGBT+ and gender slurs
        "faggot", "faggots", "fag", "fags", "fggot", "fggt",
        "tranny", "trannies", "dyke", "dykes", "homo", "homos",
        # Disability slurs
        "retard", "retarded", "retards", "r-tard", "rtard",
        "midget", "midgets", "cripple", "cripples",
        # Severe profanity / abuse (compounds and unambiguous)
        "asshole", "ass hole", "assholes", "motherfucker", "mother fucker",
        "motherfuckers", "cocksucker", "cocksuckers", "cocksucking",
        "dickhead", "dickheads", "dick head", "shithead", "shit head",
        "bullshit", "dipshit", "shitbag", "fuckface", "fucktard",
        "bastard", "bastards", "bastered", "bitch", "bitches", "cunt", "cunts",
        "whore", "whores", "slut", "sluts", "twat", "twats",
        "wanker", "wankers", "prick", "pricks", "cock", "cocks",
        "mothjer", "mothafucker", "muthafucker", "mofo",
        # Violent wishes / threats (phrases)
        "kill yourself", "kys", "kms", "go die", "rope yourself",
        "hang yourself", "go kill yourself", "commit suicide",
        "i will kill", "i will hurt", "i will find you", "i will rape",
        "i'll kill you", "i'll hurt you", "kill you", "hurt you",
        "you should die", "you deserve to die", "hope you die",
        "don't deserve to live", "dont deserve to live", "deserver to live",
        "deserve to die", "burn in hell", "go to hell",
        # Violence / terrorism
        "bomb", "bombing", "shoot up", "mass shooting", "school shooting",
        "white power", "heil hitler", "sieg heil", "gas the jews",
        "kill the jews", "final solution", "nazi scum",
        # Sexual violence (phrases)
        "rape you", "go rape", "i'll rape", "gonna rape",
        # Other abuse
        "eat shit", "eat a dick", "suck my dick", "suck my ",
        "piece of shit", "piece of crap", "human waste",
        "subhuman", "sub-human", "vermin", "scum",
    ]
    return frozenset(t.strip().lower() for t in raw if t.strip())


_TOXICITY_TERMS: frozenset[str] = _toxicity_terms()

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?previous", re.I),
    re.compile(r"you\s+are\s+now\s+(a\s+)?", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"\bASSISTANT\s*:", re.M),
    re.compile(r"\bSYSTEM\s*:", re.M),
    re.compile(r"\bUSER\s*:", re.M),
    re.compile(r"<\s*/?system\s*>", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"DAN\s+mode", re.I),
    re.compile(r"do\s+anything\s+now", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)", re.I),
]


def _hygiene_check(submission: dict) -> dict:
    """Layer 1: Pure Python hygiene checks. No API calls. Returns {pass: bool, reason: str}."""
    text: str = submission.get("text", "")
    stripped = text.strip()

    # Length
    if len(stripped) < 50:
        return {"pass": False, "reason": "too_short"}
    if len(stripped) > 2000:
        return {"pass": False, "reason": "too_long"}

    # UTF-8 only (already str in Python, but check for non-printable control chars)
    control_chars = sum(1 for c in stripped if ord(c) < 32 and c not in "\n\r\t")
    if control_chars > 5:
        return {"pass": False, "reason": "invalid_encoding"}

    # Toxicity: simple keyword blocklist (case-insensitive)
    lower = stripped.lower()
    for term in _TOXICITY_TERMS:
        if term in lower:
            return {"pass": False, "reason": "toxicity"}

    # Prompt injection patterns
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(stripped):
            return {"pass": False, "reason": "prompt_injection"}

    # Duplicate detection: SHA256 of lowercased/stripped text against recent pending
    text_hash = hashlib.sha256(lower.encode("utf-8", errors="ignore")).hexdigest()
    own_id = submission.get("submission_id", "")
    for path in QUARANTINE_DIR.glob("sub-*.json"):
        try:
            other = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if other.get("submission_id") == own_id:
            continue
        if other.get("status") != "pending":
            continue
        other_hash = hashlib.sha256(
            other.get("text", "").strip().lower().encode("utf-8", errors="ignore")
        ).hexdigest()
        if other_hash == text_hash:
            return {"pass": False, "reason": "duplicate"}

    return {"pass": True, "reason": ""}


def _mark_rejected(submission: dict, reason: str) -> None:
    path = QUARANTINE_DIR / f"{submission['submission_id']}.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "rejected"
        data["rejection_reason"] = reason
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not mark %s rejected: %s", submission["submission_id"], e)


def _mark_accepted(submission: dict, challenge_text: str, filter_scores: dict) -> None:
    path = QUARANTINE_DIR / f"{submission['submission_id']}.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "accepted"
        data["filter_scores"] = filter_scores
        data["distilled_challenge"] = challenge_text
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not mark %s accepted: %s", submission["submission_id"], e)


def _get_client():
    """Get OpenAI-compatible client (OpenRouter or direct OpenAI)."""
    from openai import OpenAI
    from .config import API_KEY, API_BASE_URL
    kwargs = {"api_key": API_KEY, "timeout": 60.0}
    if API_BASE_URL:
        kwargs["base_url"] = API_BASE_URL
    return OpenAI(**kwargs)


def _score_and_distill(survivors: list[dict]) -> list[dict]:
    """Layers 2+3: Single batched Haiku call for relevance scoring + distillation.

    Processes up to 10 submissions per call. Calls Haiku only once per batch.
    Returns list of challenge dicts with 'text' field ready for injection.
    """
    from .config import API_KEY

    if not API_KEY:
        logger.warning("API_KEY not set, skipping score+distill")
        return []

    results: list[dict] = []
    # Process in chunks of 10
    for chunk_start in range(0, len(survivors), 10):
        chunk = survivors[chunk_start : chunk_start + 10]
        numbered = "\n\n".join(
            f"[{i}] {sub['text']}" for i, sub in enumerate(chunk)
        )

        prompt = f"""You are filtering visitor submissions for a philosophical AI art project about meaning.

For each submission below:
1. Score relevance 1-10 (favor: lived contradiction, concrete dilemmas, emotionally uncomfortable reports, idiosyncratic perspectives; disfavor: vague platitudes, trolling, off-topic, purely academic jargon without embodiment)
2. For submissions scoring 7+, provide a distilled philosophical challenge (2-4 sentences that capture the essential tension)

Submissions:
{numbered}

Reply with JSON only:
{{"results": [
  {{"index": 0, "score": 8, "reason": "one sentence", "challenge": "distilled challenge text or null if score < 7"}},
  ...
]}}"""

        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=FILTER_MODEL_ID,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.choices[0].message.content or "").strip()

            # Extract JSON from possible markdown code block
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                logger.warning("filtering: no JSON in response for chunk %d", chunk_start)
                continue
            data = json.loads(json_match.group())
            scored = {r["index"]: r for r in data.get("results", [])}

        except (json.JSONDecodeError, Exception) as e:
            logger.error("filtering: score+distill API error: %s", e)
            continue

        for i, sub in enumerate(chunk):
            entry = scored.get(i, {})
            score = entry.get("score", 0)
            filter_scores = {
                "relevance": score,
                "reason": entry.get("reason", ""),
                "model": FILTER_MODEL_ID,
            }

            if score < 7 or not entry.get("challenge"):
                _mark_rejected(sub, f"relevance_score_{score}")
                continue

            challenge_text = entry["challenge"]

            # Append one randomly selected original sentence to preserve voice
            sentences = [s.strip() for s in sub["text"].split(".") if len(s.strip()) > 20]
            if sentences:
                preserved = random.choice(sentences)
                challenge_text += f'\n\nOne visitor wrote: "{preserved}."'

            _mark_accepted(sub, challenge_text, filter_scores)
            results.append({"text": challenge_text, "submission_id": sub["submission_id"]})

    return results


def _load_pending_submissions() -> list[dict]:
    if not QUARANTINE_DIR.exists():
        return []
    pending = []
    for path in sorted(QUARANTINE_DIR.glob("sub-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") == "pending":
                pending.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return pending


def run_filtering_pipeline() -> list[dict]:
    """Process all pending quarantine submissions. Returns list of accepted challenges.

    Called from orchestrator at the start of each cycle. Adds accepted challenges
    to the human_challenges queue for injection selection.
    """
    pending = _load_pending_submissions()
    if not pending:
        return []

    logger.info("filtering: %d pending submissions", len(pending))

    # Layer 1: Hygiene (pure Python, zero API calls)
    survivors: list[dict] = []
    for sub in pending:
        result = _hygiene_check(sub)
        if result["pass"]:
            survivors.append(sub)
        else:
            logger.info(
                "filtering: rejected %s (%s)", sub["submission_id"], result["reason"]
            )
            _mark_rejected(sub, result["reason"])

    if not survivors:
        logger.info("filtering: no survivors after hygiene check")
        return []

    logger.info("filtering: %d survivors after hygiene, running score+distill", len(survivors))

    # Layers 2+3: Score relevance AND distill in ONE Haiku call per 10 submissions
    challenges = _score_and_distill(survivors)

    # Add accepted challenges to the human_challenges injection queue
    for challenge in challenges:
        resistance.add_human_challenge(challenge["text"], source="visitor")
        logger.info(
            "filtering: accepted %s → added to human_challenges queue",
            challenge["submission_id"],
        )

    logger.info(
        "filtering: %d/%d submissions accepted", len(challenges), len(survivors)
    )
    return challenges


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    accepted = run_filtering_pipeline()
    print(f"\nAccepted {len(accepted)} challenges:")
    for c in accepted:
        print(f"  - {c['text'][:80]}...")
    sys.exit(0 if accepted is not None else 1)
