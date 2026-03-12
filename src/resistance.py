"""
Phase B: Resistance Manager — injection pool with weighted selection across sources.
Challenge System v2: organic weaving, weekly review, status lifecycle.
"""
import json
import logging
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import BASE_DIR, DATA_DIR, MAX_REVIEW_CHALLENGES, ORGANIC_WEAVE_THRESHOLD
from .models import Commitment, InjectionRecord, StateFile, Tension

logger = logging.getLogger(__name__)

INJECTIONS_DIR = DATA_DIR / "injections"
HUMAN_CHALLENGES_PATH = INJECTIONS_DIR / "human_challenges.json"
REJECTED_CHALLENGES_PATH = INJECTIONS_DIR / "rejected_challenges.json"
COUNTERPOSITION_COOLDOWN = 100
CREATIVE_CONSTRAINT_COOLDOWN = 10

# Weights (v2: human challenges via weaving/review, not weighted draw)
WEIGHT_COUNTERPOSITION = 0.35
WEIGHT_CREATIVE = 0.25
WEIGHT_PATTERN = 0.15


def _atomic_write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_json_list(path: Path, default: list) -> list:
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else default
    except (json.JSONDecodeError, OSError):
        return default


def load_counterpositions() -> list[dict]:
    """Load counterposition library. Each item: id, text, last_used_cycle."""
    path = INJECTIONS_DIR / "counterpositions.json"
    return _load_json_list(path, [])


def load_creative_constraints() -> list[dict]:
    """Load creative constraint library. Each item: id, text, last_used_cycle. Falls back to seed if runtime file missing or empty."""
    path = INJECTIONS_DIR / "creative_constraints.json"
    items = _load_json_list(path, [])
    if not items:
        seed_path = BASE_DIR / "seed" / "creative_constraints.json"
        items = _load_json_list(seed_path, [])
    return items


def _normalize_challenge_entry(entry: dict) -> dict:
    """Backfill Challenge System v2 fields on legacy entries."""
    out = dict(entry)
    if "status" not in out:
        out["status"] = "pending"
    for key in ("linked_cycle", "linked_journal", "resolved_at", "lapse_reason", "submitter_name", "raw_text", "score"):
        if key not in out:
            out[key] = None
    if "raw_text" in out and out["raw_text"] is None and "text" in out:
        out["raw_text"] = out["text"]
    return out


def load_human_challenges() -> list[dict]:
    """Load human challenges. Each item has id, text, status, score, etc. Append-and-update store (no consumption)."""
    items = _load_json_list(HUMAN_CHALLENGES_PATH, [])
    return [_normalize_challenge_entry(e) for e in items]


def update_challenge_status(
    challenge_id: str,
    status: str,
    *,
    linked_cycle: int | None = None,
    linked_journal: str | None = None,
    lapse_reason: str | None = None,
) -> bool:
    """Update a challenge's status. Returns True if updated, False if not found."""
    items = load_human_challenges()
    resolved_at = datetime.now(timezone.utc).isoformat()
    for item in items:
        if item.get("id") == challenge_id:
            item["status"] = status
            item["resolved_at"] = resolved_at
            if linked_cycle is not None:
                item["linked_cycle"] = linked_cycle
            if linked_journal is not None:
                item["linked_journal"] = linked_journal
            if lapse_reason is not None:
                item["lapse_reason"] = lapse_reason
            _atomic_write_json(HUMAN_CHALLENGES_PATH, items)
            logger.info("Updated challenge %s to status=%s", challenge_id, status)
            return True
    logger.warning("update_challenge_status: challenge_id %r not found in human_challenges", challenge_id)
    return False


def consume_human_challenge(index: int) -> dict | None:
    """Remove and return the human challenge at index; save updated queue. Deprecated: use update_challenge_status in v2."""
    HUMAN_CHALLENGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    items = _load_json_list(HUMAN_CHALLENGES_PATH, [])
    if index < 0 or index >= len(items):
        return None
    item = items.pop(index)
    _atomic_write_json(HUMAN_CHALLENGES_PATH, items)
    return item


def update_cooldown(source: str, entry_id: str, cycle: int) -> None:
    """Update last_used_cycle for a counterposition or creative constraint."""
    if source == "counterposition":
        path = INJECTIONS_DIR / "counterpositions.json"
        items = load_counterpositions()
    elif source == "creative_constraint":
        path = INJECTIONS_DIR / "creative_constraints.json"
        items = load_creative_constraints()
    else:
        return
    for item in items:
        if item.get("id") == entry_id:
            item["last_used_cycle"] = cycle
            break
    _atomic_write_json(path, items)


def _containment_ratio(a: str, b: str) -> float:
    """Fraction of a's words (len>2) found in b. Returns 0-1."""
    aw = set(w.lower() for w in a.split() if len(w) > 2)
    bw = set(w.lower() for w in b.split() if len(w) > 2)
    if not aw:
        return 0.0
    return len(aw & bw) / len(aw)


def find_organic_weave_candidate(
    injection_text: str,
    tensions: list[Tension],
) -> dict | None:
    """Find best pending challenge that matches the cycle topic. Returns challenge dict or None."""
    pending = [c for c in load_human_challenges() if c.get("status") == "pending"]
    if not pending:
        return None

    tension_text = " ".join(
        t.description for t in tensions if hasattr(t, "description")
    )
    combined_topic = f"{injection_text} {tension_text}".strip()

    best: dict | None = None
    best_score = 0.0

    for c in pending:
        challenge_text = (c.get("text") or "").strip()
        if not challenge_text:
            continue
        score = _containment_ratio(challenge_text, combined_topic)
        quality = c.get("score") or 0
        combined = score * (1 + 0.1 * quality)  # tiebreak by quality
        if score >= ORGANIC_WEAVE_THRESHOLD and (best is None or combined > best_score):
            best = c
            best_score = combined

    return best


def select_injection(
    cycle: int,
    tensions: list[Tension],
    commitments: list[Commitment],
    pattern_interruption: str | None,
) -> InjectionRecord:
    """
    Build available pool (no human_challenge in draw), weighted random draw.
    After draw, optionally find organic weave candidate from pending challenges.
    """
    pool: list[tuple[float, str, str, str]] = []  # (weight, source, text, id_or_index)
    total_weight = 0.0

    # Counterpositions (cooldown: once per 100 cycles)
    counter = load_counterpositions()
    available_cp = [c for c in counter if (cycle - c.get("last_used_cycle", 0)) >= COUNTERPOSITION_COOLDOWN]
    if available_cp:
        c = random.choice(available_cp)
        text = c.get("text", "").strip()
        if text:
            pool.append((WEIGHT_COUNTERPOSITION, "counterposition", text, c.get("id", "")))
            total_weight += WEIGHT_COUNTERPOSITION

    # Creative constraints (cooldown: 10 cycles)
    creative = load_creative_constraints()
    available_cc = [c for c in creative if (cycle - c.get("last_used_cycle", 0)) >= CREATIVE_CONSTRAINT_COOLDOWN]
    if available_cc:
        c = random.choice(available_cc)
        text = c.get("text", "").strip()
        if text:
            pool.append((WEIGHT_CREATIVE, "creative_constraint", text, c.get("id", "")))
            total_weight += WEIGHT_CREATIVE

    # Pattern interruption (only if pending)
    if pattern_interruption and pattern_interruption.strip():
        pool.append((WEIGHT_PATTERN, "pattern_interruption", pattern_interruption.strip(), ""))
        total_weight += WEIGHT_PATTERN

    if not pool:
        # Fallback: no external challenge (like Phase A)
        fallback = (
            "No external challenge this cycle. Push into territory you have been avoiding."
        )
        return InjectionRecord(
            source="none",
            text=fallback,
            original_weight=0.0,
            effective_weight=1.0,
        )

    # Redistribute weights to sum to 1.0
    if total_weight <= 0:
        total_weight = 1.0
    r = 1.0 / total_weight
    weighted: list[tuple[float, str, str, str]] = [(w * r, src, txt, eid) for w, src, txt, eid in pool]

    # Weighted random draw
    rnd = random.random()
    acc = 0.0
    chosen = None
    for w, source, text, eid in weighted:
        acc += w
        if rnd <= acc:
            chosen = (w, source, text, eid)
            break
    if chosen is None:
        chosen = weighted[-1]

    w, source, text, eid = chosen
    original = (
        WEIGHT_COUNTERPOSITION if source == "counterposition" else (
            WEIGHT_CREATIVE if source == "creative_constraint" else WEIGHT_PATTERN
        )
    )
    record = InjectionRecord(
        source=source,
        text=text,
        original_weight=original,
        effective_weight=w,
    )

    # Organic weaving: check for relevant pending challenge
    woven = find_organic_weave_candidate(text, tensions)
    if woven:
        woven_id = woven.get("id", "")
        rel = _containment_ratio(
            (woven.get("text") or "").strip(),
            f"{text} {' '.join(t.description for t in tensions if hasattr(t, 'description'))}",
        )
        record.woven_challenge = {
            "submission_id": woven_id,
            "text": (woven.get("text") or "").strip(),
            "relevance_score": rel,
            "submitter_name": woven.get("submitter_name"),
        }
        # Mark as woven immediately at selection time so the status is correct even if
        # the cycle later fails before the orchestrator's post-cycle cleanup runs.
        # linked_journal is set to str(cycle) as a placeholder; the orchestrator confirms
        # it post-cycle (same value, so re-calling update_challenge_status is a no-op).
        if woven_id:
            update_challenge_status(
                woven_id,
                "woven",
                linked_cycle=cycle,
                linked_journal=str(cycle),
            )

    # Update cooldown
    if source == "counterposition" and eid:
        update_cooldown("counterposition", eid, cycle)
    elif source == "creative_constraint" and eid:
        update_cooldown("creative_constraint", eid, cycle)

    return record


def append_challenge(entry: dict) -> None:
    """Append a challenge to human_challenges.json with full v2 schema."""
    HUMAN_CHALLENGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    items = load_human_challenges()
    items.append(_normalize_challenge_entry(entry))
    _atomic_write_json(HUMAN_CHALLENGES_PATH, items)
    logger.info("Appended challenge: %s", entry.get("id", "?"))


def build_weekly_review_injection(cycle: int, state: StateFile) -> InjectionRecord:
    """Build injection for Sunday weekly review. Top pending + woven from past week."""
    all_challenges = load_human_challenges()
    pending = sorted(
        [c for c in all_challenges if c.get("status") == "pending"],
        key=lambda c: (c.get("score") or 0),
        reverse=True,
    )
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    woven_recent = [
        c for c in all_challenges
        if c.get("status") == "woven"
        and (c.get("resolved_at") or "") >= week_ago
    ]
    woven_recent.sort(key=lambda c: (c.get("score") or 0), reverse=True)

    addressed: list[str] = []
    mentioned: list[str] = []
    lines: list[str] = []
    mentioned_lines: list[str] = []

    for c in pending[:MAX_REVIEW_CHALLENGES]:
        sid = c.get("id", "")
        if not sid:
            continue
        addressed.append(sid)
        score = c.get("score") or 0
        text = (c.get("text") or "").strip()
        if text:
            lines.append(f'{len(lines) + 1}. "{text}" (score: {score})')

    for c in woven_recent:
        sid = c.get("id", "")
        if not sid or sid in addressed:
            continue
        if len(addressed) + len(mentioned) < MAX_REVIEW_CHALLENGES:
            addressed.append(sid)
            score = c.get("score") or 0
            text = (c.get("text") or "").strip()
            cycle_num = c.get("linked_cycle") or "?"
            if text:
                lines.append(f'{len(lines) + 1}. "{text}" (score: {score}, woven Cycle {cycle_num})')
        else:
            mentioned.append(sid)
            text = (c.get("text") or "").strip()
            cycle_num = c.get("linked_cycle") or "?"
            if text:
                mentioned_lines.append(f'- "{text}" (Cycle {cycle_num})')

    prompt_parts = [
        "## Weekly Review\n\n"
        "This cycle, you are responding to the voices of visitors who\n"
        "challenged your inquiry this week.\n\n## Challenges to Address\n"
    ]
    if lines:
        prompt_parts.append("\n".join(lines))
    else:
        prompt_parts.append("(No open challenges this week.)")
    if mentioned_lines:
        prompt_parts.append("\n\n## Already Encountered This Week\n\n")
        prompt_parts.append("\n".join(mentioned_lines))

    return InjectionRecord(
        source="weekly_review",
        text="\n".join(prompt_parts),
        original_weight=1.0,
        effective_weight=1.0,
        challenges_addressed=addressed,
        challenges_mentioned=mentioned,
    )


def run_weekly_review_lapse(state: StateFile) -> int:
    """Lapse pending challenges older than previous review. Returns count lapsed."""
    prev = state.last_review_at
    if not prev:
        return 0
    items = load_human_challenges()
    lapsed = 0
    for c in items:
        if c.get("status") != "pending":
            continue
        submitted = c.get("submitted_at") or ""
        if submitted and submitted < prev:
            c["status"] = "lapsed"
            c["resolved_at"] = datetime.now(timezone.utc).isoformat()
            c["lapse_reason"] = "Not selected within review window"
            lapsed += 1
    if lapsed:
        _atomic_write_json(HUMAN_CHALLENGES_PATH, items)
        logger.info("Lapsed %d pending challenges (older than previous review)", lapsed)
    return lapsed


def add_human_challenge(text: str, source: str = "creator") -> None:
    """Append a human challenge (e.g. from interlocutor). Uses v2 schema with status=pending."""
    import time
    uid = f"{source}-{int(time.time() * 1000)}"
    entry = {
        "id": uid,
        "text": text.strip(),
        "raw_text": text.strip(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "score": None,
        "status": "pending",
        "source": source,
        "linked_cycle": None,
        "linked_journal": None,
        "resolved_at": None,
        "lapse_reason": None,
        "submitter_name": None,
    }
    append_challenge(entry)
