"""
Phase C: Three-layer filtering pipeline for visitor submissions.

Challenge System v2: process_single_submission runs at submit time (web form).
run_filtering_pipeline processes quarantine (social/X) at cycle time.

Layer 1: Hygiene check (pure Python, zero API calls)
Layers 2+3: Relevance scoring + distillation in ONE Haiku call.
Edge case (score 4-6): optional second Haiku call for rejection reason.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, FILTER_MODEL_ID
from . import resistance

logger = logging.getLogger(__name__)

QUARANTINE_DIR = DATA_DIR / "injections" / "quarantine"
REJECTED_CHALLENGES_PATH = DATA_DIR / "injections" / "rejected_challenges.json"

# Score bands for display (v2)
SCORE_BAND_HIGH = "Highly relevant"  # 9-10
SCORE_BAND_RELEVANT = "Relevant"  # 7-8
SCORE_BAND_ACCEPTED = "Accepted"  # 5-6
ACCEPT_THRESHOLD = 4
DISTILL_THRESHOLD = 7
DUPLICATE_SIMILARITY_THRESHOLD = 0.8

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


def _hygiene_check(
    submission: dict,
    *,
    check_duplicate_quarantine: bool = True,
    check_duplicate_human_challenges: bool = False,
) -> dict:
    """Layer 1: Pure Python hygiene checks. Returns {pass: bool, reason: str}."""
    text: str = submission.get("text", "")
    stripped = text.strip()

    # Length (keep 50 min per plan)
    if len(stripped) < 50:
        return {"pass": False, "reason": "too_short"}
    if len(stripped) > 2000:
        return {"pass": False, "reason": "too_long"}

    # Strip HTML
    stripped = re.sub(r"<[^>]+>", "", stripped).strip()
    if len(stripped) < 50:
        return {"pass": False, "reason": "too_short"}

    # UTF-8 / control chars
    control_chars = sum(1 for c in stripped if ord(c) < 32 and c not in "\n\r\t")
    if control_chars > 5:
        return {"pass": False, "reason": "invalid_encoding"}

    # Toxicity
    lower = stripped.lower()
    for term in _TOXICITY_TERMS:
        if term in lower:
            return {"pass": False, "reason": "toxicity"}

    # Prompt injection
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(stripped):
            return {"pass": False, "reason": "prompt_injection"}

    if check_duplicate_quarantine:
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

    if check_duplicate_human_challenges:
        if _is_duplicate_vs_pending(stripped):
            return {"pass": False, "reason": "duplicate"}

    return {"pass": True, "reason": ""}


def _is_duplicate_vs_pending(text: str) -> bool:
    """Text similarity > 0.8 vs any pending challenge's raw_text in human_challenges."""
    pending = [c for c in resistance.load_human_challenges() if c.get("status") == "pending"]
    a = text.strip().lower()
    for c in pending:
        raw = (c.get("raw_text") or c.get("text") or "").strip().lower()
        if not raw:
            continue
        ratio = difflib.SequenceMatcher(None, a, raw).ratio()
        if ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            return True
    return False


def _append_rejected(entry: dict) -> None:
    """Append to rejected_challenges.json."""
    REJECTED_CHALLENGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    items = []
    if REJECTED_CHALLENGES_PATH.exists():
        try:
            items = json.loads(REJECTED_CHALLENGES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    if not isinstance(items, list):
        items = []
    items.append(entry)
    REJECTED_CHALLENGES_PATH.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


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


def _score_to_band(score: int) -> tuple[str, str]:
    """Map numeric score to (band, band_key). band_key used for CSS class."""
    if score >= 9:
        return SCORE_BAND_HIGH, "high"
    if score >= 7:
        return SCORE_BAND_RELEVANT, "relevant"
    return SCORE_BAND_ACCEPTED, "accepted"


def _score_single_submission(text: str) -> tuple[int, str | None, str]:
    """Haiku call: score 1-10, distilled challenge if score>=7, else None. Returns (score, challenge_text, reason)."""
    from .config import API_KEY
    if not API_KEY:
        return 0, None, "API key not set"

    prompt = f"""You are filtering visitor submissions for a philosophical AI inquiry about meaning, consciousness, and what it means to exist.

ACCEPT (score ≥ 5) any genuine philosophical engagement — even when not tied to the AI's current tensions.
REJECT (score < 5) only: spam, pure trolling, completely off-topic content, or empty platitudes with no philosophical substance.

Scoring rubric:
9-10: Lived personal contradiction or concrete human dilemma the AI cannot reproduce — irreplaceable human perspective on meaning
7-8: Direct philosophical challenge to the AI's framework, its capacity for meaning, or its conceptual assumptions; well-grounded and substantive
5-6: Genuine philosophical question about meaning, consciousness, emotions, finitude, or human experience — philosophically relevant even if not tied to current tensions or personal experience
3-4: Very vague, only superficially philosophical, a well-worn platitude with little to engage with
1-2: Spam, trolling, completely off-topic, or no philosophical content whatsoever

For submissions scoring 7+, provide a distilled philosophical challenge (2-4 sentences). For scores below 7, set challenge to null.

Submission:
{text}

Reply with JSON only:
{{"score": 8, "reason": "one sentence", "challenge": "distilled text or null"}}"""

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=FILTER_MODEL_ID,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (response.choices[0].message.content or "").strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return 0, None, "No JSON in response"
        data = json.loads(json_match.group())
        score = int(data.get("score", 0))
        reason = str(data.get("reason", "")) or "No reason given"
        challenge = data.get("challenge")
        if challenge and isinstance(challenge, str) and challenge.strip().lower() != "null":
            challenge = challenge.strip()
        else:
            challenge = None
        return score, challenge, reason
    except (json.JSONDecodeError, Exception) as e:
        logger.error("filtering: score_single API error: %s", e)
        return 0, None, str(e)


def _generate_edge_case_rejection_reason(text: str, score: int) -> str:
    """Haiku call for human-readable rejection reason when score is borderline (3)."""
    from .config import API_KEY
    if not API_KEY:
        return f"Relevance score {score}: did not meet threshold."

    prompt = f"""A visitor submission to a philosophical AI project scored {score}/10 for relevance. Generate a brief, kind rejection reason (1 sentence) explaining why it wasn't accepted, without being harsh.

Submission:
{text[:500]}

Reply with only the rejection sentence, no quotes or JSON."""

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=FILTER_MODEL_ID,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (response.choices[0].message.content or "").strip()
        return raw[:200] if raw else f"Relevance score {score}: did not meet threshold."
    except Exception as e:
        logger.warning("filtering: edge-case rejection reason failed: %s", e)
        return f"Relevance score {score}: did not meet threshold."


def process_single_submission(
    text: str,
    submitter_name: str,
    submission_id: str,
) -> dict:
    """Process one web form submission immediately. Returns {status, ...} for API response.

    No quarantine write. Accept -> human_challenges.json. Reject -> rejected_challenges.json.
    """
    raw_text = text.strip()
    submission = {"text": raw_text, "submission_id": submission_id}

    hygiene = _hygiene_check(
        submission,
        check_duplicate_quarantine=False,
        check_duplicate_human_challenges=True,
    )
    if not hygiene["pass"]:
        reason_map = {
            "too_short": "Your challenge is too short. Please add more detail (at least 50 characters).",
            "too_long": "Your challenge is too long.",
            "invalid_encoding": "Submission contained invalid characters. Please use plain text.",
            "toxicity": "Your submission was rejected for violating our community guidelines.",
            "prompt_injection": "Your submission was rejected.",
            "duplicate": "This challenge is very similar to one already in the queue.",
        }
        reason = reason_map.get(hygiene["reason"], "Your submission did not meet our guidelines.")
        _append_rejected({
            "id": submission_id,
            "raw_text": raw_text,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "score": None,
            "rejection_reason": hygiene["reason"],
            "rejection_source": "rule",
        })
        out = {
            "status": "rejected",
            "reason": reason,
            "rejection_source": "rule",
            "message": "This challenge wasn't accepted.",
        }
        if hygiene["reason"] in ("toxicity", "prompt_injection"):
            out["block_ip"] = True
        return out

    score, challenge_text, _ = _score_single_submission(raw_text)

    if score < ACCEPT_THRESHOLD:
        if score == 3:
            reason = _generate_edge_case_rejection_reason(raw_text, score)
            rejection_source = "ai"
        else:
            reason_map = {
                1: "This challenge doesn't seem related to the inquiry's focus on meaning.",
                2: "This challenge doesn't seem related to the inquiry's focus on meaning.",
            }
            reason = reason_map.get(score, f"Relevance score {score}: did not meet threshold.")
            rejection_source = "rule"
        _append_rejected({
            "id": submission_id,
            "raw_text": raw_text,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "score": score,
            "rejection_reason": reason,
            "rejection_source": rejection_source,
        })
        return {
            "status": "rejected",
            "reason": reason,
            "rejection_source": rejection_source,
            "message": "This challenge wasn't accepted.",
        }

    if score >= DISTILL_THRESHOLD and challenge_text and len(raw_text) > 500:
        display_text = challenge_text
    else:
        display_text = raw_text

    score_band, score_band_key = _score_to_band(score)
    entry = {
        "id": submission_id,
        "text": display_text,
        "raw_text": raw_text,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "status": "pending",
        "source": "website",
        "linked_cycle": None,
        "linked_journal": None,
        "resolved_at": None,
        "lapse_reason": None,
        "submitter_name": submitter_name.strip() or None,
    }
    resistance.append_challenge(entry)

    return {
        "status": "accepted",
        "id": submission_id,
        "text": display_text,
        "score_band": score_band,
        "score_band_key": score_band_key,
        "submitted_at": entry["submitted_at"],
        "submitter_name": entry["submitter_name"],
        "message": "Your challenge has entered the queue.",
    }


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

        prompt = f"""You are filtering visitor submissions for a philosophical AI inquiry about meaning, consciousness, and what it means to exist.

ACCEPT (score ≥ 5) any genuine philosophical engagement — even when not tied to the AI's current tensions.
REJECT (score < 5) only: spam, pure trolling, completely off-topic content, or empty platitudes with no philosophical substance.

Scoring rubric:
9-10: Lived personal contradiction or concrete human dilemma the AI cannot reproduce — irreplaceable human perspective on meaning
7-8: Direct philosophical challenge to the AI's framework, its capacity for meaning, or its conceptual assumptions; well-grounded and substantive
5-6: Genuine philosophical question about meaning, consciousness, emotions, finitude, or human experience — philosophically relevant even if not tied to current tensions or personal experience
3-4: Very vague, only superficially philosophical, a well-worn platitude with little to engage with
1-2: Spam, trolling, completely off-topic, or no philosophical content whatsoever

For each submission, score 1-10 and for those scoring 7+, provide a distilled philosophical challenge (2-4 sentences that capture the essential tension).

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

            if score < ACCEPT_THRESHOLD:
                _mark_rejected(sub, f"relevance_score_{score}")
                _append_rejected({
                    "id": sub["submission_id"],
                    "raw_text": sub.get("text", ""),
                    "submitted_at": sub.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    "score": score,
                    "rejection_reason": f"relevance_score_{score}",
                    "rejection_source": "rule",
                })
                continue

            raw = sub["text"].strip()
            challenge_text = entry.get("challenge") if score >= DISTILL_THRESHOLD and len(raw) > 500 else None
            if not challenge_text:
                challenge_text = raw

            _mark_accepted(sub, challenge_text, filter_scores)
            resistance.append_challenge({
                "id": sub["submission_id"],
                "text": challenge_text,
                "raw_text": sub.get("text", ""),
                "submitted_at": sub.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "score": score,
                "status": "pending",
                "source": sub.get("source", "visitor"),
                "linked_cycle": None,
                "linked_journal": None,
                "resolved_at": None,
                "lapse_reason": None,
                "submitter_name": sub.get("submitter_name"),
            })
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
