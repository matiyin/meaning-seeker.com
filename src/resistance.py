"""
Phase B: Resistance Manager — injection pool with weighted selection across sources.
"""
import json
import logging
import os
import random
import tempfile
from pathlib import Path

from .config import DATA_DIR
from .models import Commitment, InjectionRecord, Tension

logger = logging.getLogger(__name__)

INJECTIONS_DIR = DATA_DIR / "injections"
COUNTERPOSITION_COOLDOWN = 100
CREATIVE_CONSTRAINT_COOLDOWN = 10

# Weights (proposal Section 3.5). Redistributed when a source is unavailable.
WEIGHT_HUMAN = 0.40
WEIGHT_COUNTERPOSITION = 0.25
WEIGHT_CREATIVE = 0.20
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
    """Load creative constraint library. Each item: id, text, last_used_cycle."""
    path = INJECTIONS_DIR / "creative_constraints.json"
    return _load_json_list(path, [])


def load_human_challenges() -> list[dict]:
    """Load human challenges queue. Each item: id, text, source (optional). Consumed on use."""
    path = INJECTIONS_DIR / "human_challenges.json"
    return _load_json_list(path, [])


def consume_human_challenge(index: int) -> dict | None:
    """Remove and return the human challenge at index; save updated queue."""
    path = INJECTIONS_DIR / "human_challenges.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    items = _load_json_list(path, [])
    if index < 0 or index >= len(items):
        return None
    item = items.pop(index)
    _atomic_write_json(path, items)
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


def select_injection(
    cycle: int,
    tensions: list[Tension],
    commitments: list[Commitment],
    pattern_interruption: str | None,
) -> InjectionRecord:
    """
    Build available pool, redistribute weights, weighted random draw.
    Returns one InjectionRecord for this cycle.
    """
    pool: list[tuple[float, str, str, str]] = []  # (weight, source, text, id_or_index)
    total_weight = 0.0

    # Human challenges
    human = load_human_challenges()
    if human:
        idx = random.randint(0, len(human) - 1)
        item = human[idx]
        text = item.get("text", "").strip()
        if text:
            pool.append((WEIGHT_HUMAN, "human_challenge", text, str(idx)))
            total_weight += WEIGHT_HUMAN

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
    original = WEIGHT_HUMAN if source == "human_challenge" else (
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

    # Consume or update cooldown
    if source == "human_challenge":
        consume_human_challenge(int(eid))
    elif source == "counterposition" and eid:
        update_cooldown("counterposition", eid, cycle)
    elif source == "creative_constraint" and eid:
        update_cooldown("creative_constraint", eid, cycle)

    return record


def add_human_challenge(text: str, source: str = "creator") -> None:
    """Append a human challenge to the queue (e.g. from interlocutor)."""
    path = INJECTIONS_DIR / "human_challenges.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    items = _load_json_list(path, [])
    import time
    uid = f"{source}-{int(time.time() * 1000)}"
    items.append({"id": uid, "text": text.strip(), "source": source})
    _atomic_write_json(path, items)
    logger.info("Added human challenge to queue: %s", uid)
