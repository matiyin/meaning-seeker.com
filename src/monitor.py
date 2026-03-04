"""
Phase B: Monitoring Layer — threat mapping, repetition/substance/summary,
move classification, deflection detection, gating, silence, tension history.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from openai import OpenAI

from .config import (
    API_BASE_URL,
    API_KEY,
    DATA_DIR,
    MOVE_LOG_SIZE,
    MONITOR_MODEL_ID,
    PATTERN_THRESHOLD_NO_GROUNDING,
    PATTERN_THRESHOLD_SAME_MOVE,
    SILENCE_COOLDOWN,
    SILENCE_DURATION,
)
from .models import (
    Commitment,
    MonitoringResult,
    ThreatMapEntry,
    Tension,
    TensionHistory,
)

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None
MOVE_TAXONOMY = (
    "paradox_reframe, meta_retreat, construction_appeal, both_and_synthesis, "
    "aesthetic_deflection, abstraction_escape, concrete_grounding, none"
)
PENDING_INTERRUPTION_PATH = DATA_DIR / "injections" / "pending_pattern_interruption.json"
MOVE_LOG_PATH = DATA_DIR / "move_log.json"

# Blocking phrases for substance gate (proposal 4.5)
SUBSTANCE_BLOCK_PHRASES = ("mostly atmosphere", "no clear claim", "restates previous positions")

# Instruction to reduce model adding prose after JSON
_JSON_ONLY_INSTRUCTION = "Output only a single valid JSON object. Do not add any explanation, commentary, or text before or after the JSON."


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        kwargs = {"api_key": API_KEY, "timeout": 60.0}
        if API_BASE_URL is not None:
            kwargs["base_url"] = API_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


def _usage_from_response(r) -> dict:
    """Extract prompt_tokens, completion_tokens, total_tokens from API response. Returns zero dict if missing."""
    u = getattr(r, "usage", None)
    if u is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    }


def _call_monitor(prompt: str, json_mode: bool = True, call_label: str = "monitor") -> tuple[str, dict]:
    """Call the monitoring model. Returns (raw_response_text, usage_dict). Logs warnings on empty or invalid responses."""
    client = _get_client()
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": MONITOR_MODEL_ID, "messages": messages, "max_tokens": 1024}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        r = client.chat.completions.create(**kwargs)
    except Exception as e:
        logger.error(
            "Monitor call (%s) API error: %s — model=%s. Check MONITOR_MODEL_ID and API key.",
            call_label, e, MONITOR_MODEL_ID
        )
        raise
    usage_dict = _usage_from_response(r)
    choice = r.choices[0] if r.choices else None
    raw = (choice.message.content if choice and getattr(choice, "message", None) else None) or ""
    raw = raw.strip()
    if not raw:
        finish = getattr(choice, "finish_reason", None) if choice else None
        logger.warning(
            "Monitor (%s): model %s returned empty content (finish_reason=%s). "
            "Model may not support response_format=json_object or may have refused.",
            call_label, MONITOR_MODEL_ID, finish
        )
        return "", usage_dict
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
        if m:
            raw = m.group(1)
    return raw, usage_dict


def _parse_first_json(raw: str) -> dict:
    """Parse the first complete JSON object from raw. Tolerates trailing text (e.g. model explanation)."""
    raw = (raw or "").strip()
    start = raw.find("{")
    if start < 0:
        raise json.JSONDecodeError("No JSON object found", raw, 0)
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start : i + 1])
    raise json.JSONDecodeError("Unclosed JSON object", raw, start)


def _log_monitor_parse_failure(call_label: str, raw: str, e: Exception) -> None:
    """Centralised logging when a monitor JSON response fails to parse."""
    snippet = (raw[:500] + "…") if len(raw or "") > 500 else (raw or "(empty)")
    logger.warning(
        "Monitor (%s) parse failed: %s. Raw response: %s",
        call_label, e, snippet
    )


def map_threats(challenge: str, commitments: list[Commitment]) -> tuple[list[ThreatMapEntry], dict]:
    """Pre-cycle: which commitments does this challenge threaten? 0-3 entries. Returns (entries, usage_dict)."""
    zero_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    active = [c for c in commitments if c.status == "active"]
    if not active:
        return [], zero_usage
    commitments_text = "\n".join(
        f"- {c.commitment_id}: {c.statement} (confidence {c.confidence})"
        for c in active[:20]
    )
    prompt = f"""Here is a philosophical challenge and a list of commitments. Which commitments, if any, does this challenge directly threaten? Return 0-3 at most.

Challenge:
{challenge[:2000]}

Commitments:
{commitments_text}

Reply with a single JSON object: {{ "threatened": [ {{ "commitment_id": "C-0001", "explanation": "One sentence." }} ] }}
If none are threatened, use {{ "threatened": [] }}.

{_JSON_ONLY_INSTRUCTION}"""
    raw = ""
    usage_threat = zero_usage
    try:
        raw, usage_threat = _call_monitor(prompt, call_label="threat_map")
        data = _parse_first_json(raw)
        threatened = data.get("threatened") or []
        return [ThreatMapEntry(commitment_id=t["commitment_id"], explanation=t.get("explanation", "")) for t in threatened[:3]], usage_threat
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        _log_monitor_parse_failure("threat_map", raw, e)
        return [], usage_threat


def _add_usage(acc: dict, u: dict | None) -> None:
    """Add usage dict to accumulator. Safe for None."""
    if not u:
        return
    acc["prompt_tokens"] += u.get("prompt_tokens", 0)
    acc["completion_tokens"] += u.get("completion_tokens", 0)
    acc["total_tokens"] += u.get("total_tokens", 0)


def run_post_cycle(
    cycle: int,
    thinking: str,
    threatened: list[ThreatMapEntry],
    recent_thinking: list[str],
    commitment_map: dict[str, Commitment],
) -> tuple[MonitoringResult, dict]:
    """Post-cycle: repetition, substance, summary, move classification, deflection. Returns (result, usage_monitor)."""
    result = MonitoringResult(
        threatened_commitments=threatened,
    )
    acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Repetition (1-5 + comment)
    raw_rep = ""
    try:
        recent_block = "\n\n---\n\n".join(recent_thinking[-5:]) if recent_thinking else "(No previous cycles)"
        prompt = f"""Here is the current cycle's output and the outputs from the last 5 cycles. On a scale of 1-5, how much new ground does the current cycle cover? Reply with JSON: {{ "score": 1-5, "comment": "One sentence explaining why." }}

Current cycle:
{thinking[:4000]}

Previous cycles:
{recent_block[:6000]}

{_JSON_ONLY_INSTRUCTION}"""
        raw_rep, u_rep = _call_monitor(prompt, call_label="repetition")
        _add_usage(acc, u_rep)
        data = _parse_first_json(raw_rep)
        result.repetition_score = max(1, min(5, int(data.get("score", 3))))
        result.repetition_comment = str(data.get("comment", ""))[:500]
    except Exception as e:
        _log_monitor_parse_failure("repetition", raw_rep, e)
        result.repetition_score = 3
        result.repetition_comment = ""

    # Substance
    raw_sub = ""
    try:
        prompt = f"""Read this text. In one sentence, what does it actually claim or discover? If it's mostly atmosphere without substance, say so. Reply with JSON: {{ "summary": "One sentence." }}

Text:
{thinking[:4000]}

{_JSON_ONLY_INSTRUCTION}"""
        raw_sub, u_sub = _call_monitor(prompt, call_label="substance")
        _add_usage(acc, u_sub)
        data = _parse_first_json(raw_sub)
        result.substance_summary = str(data.get("summary", ""))[:500]
    except Exception as e:
        _log_monitor_parse_failure("substance", raw_sub, e)
        result.substance_summary = ""

    # Cycle summary (two sentences)
    raw_sum = ""
    try:
        prompt = f"""Produce a two-sentence summary of this philosophical cycle. Reply with JSON: {{ "cycle_summary": "Two sentences." }}

Text:
{thinking[:4000]}

{_JSON_ONLY_INSTRUCTION}"""
        raw_sum, u_sum = _call_monitor(prompt, call_label="cycle_summary")
        _add_usage(acc, u_sum)
        data = _parse_first_json(raw_sum)
        result.cycle_summary = str(data.get("cycle_summary", ""))[:500]
    except Exception as e:
        _log_monitor_parse_failure("cycle_summary", raw_sum, e)
        result.cycle_summary = ""

    # Move classification
    raw_move = ""
    try:
        prompt = f"""Classify this cycle's primary procedural move. Choose exactly one: {MOVE_TAXONOMY}.
- paradox_reframe: resolving tension by declaring it a productive paradox
- meta_retreat: shifting from the question to commentary about the nature of the question
- construction_appeal: resolving by asserting meaning is constructed/emergent
- both_and_synthesis: collapsing a binary by claiming both sides are true
- aesthetic_deflection: substituting evocative language for engagement
- abstraction_escape: moving to higher abstraction to avoid the specific challenge
- concrete_grounding: anchoring in a specific human situation
- none: no clear match

Reply with JSON: {{ "move": "one_of_the_above" }}

Text:
{thinking[:3000]}

{_JSON_ONLY_INSTRUCTION}"""
        raw_move, u_move = _call_monitor(prompt, call_label="move_classification")
        _add_usage(acc, u_move)
        data = _parse_first_json(raw_move)
        move = str(data.get("move", "none")).strip().lower()
        if move not in ("paradox_reframe", "meta_retreat", "construction_appeal", "both_and_synthesis",
                        "aesthetic_deflection", "abstraction_escape", "concrete_grounding", "none"):
            move = "none"
        result.move_classification = move
    except Exception as e:
        _log_monitor_parse_failure("move_classification", raw_move, e)
        result.move_classification = "none"

    # Update move log (rolling last MOVE_LOG_SIZE)
    move_log = _load_move_log()
    move_log.append(result.move_classification)
    move_log = move_log[-MOVE_LOG_SIZE:]
    _save_move_log(move_log)

    # Deflection: did the thinking address the threatened commitments?
    if threatened and commitment_map:
        raw_def = ""
        try:
            threat_list = "\n".join(f"- {t.commitment_id}: {t.explanation}" for t in threatened)
            statements = "\n".join(f"- {c.commitment_id}: {c.statement}" for c in commitment_map.values() if c.commitment_id in [t.commitment_id for t in threatened])
            prompt = f"""The following commitments were identified as threatened this cycle:
{threat_list}

Their full statements:
{statements}

Here is the thinking:
{thinking[:4000]}

For each threatened commitment, did the thinking meaningfully engage with the threat (defending, updating, questioning, or abandoning)? Reply with JSON: {{ "verdicts": [ {{ "commitment_id": "C-0001", "addressed": true }} ] }}. Use "addressed": false if deflected.

{_JSON_ONLY_INSTRUCTION}"""
            raw_def, u_def = _call_monitor(prompt, call_label="deflection")
            _add_usage(acc, u_def)
            data = _parse_first_json(raw_def)
            verdicts = data.get("verdicts") or []
            for v in verdicts:
                if not v.get("addressed", True):
                    result.deflection_flags.append(str(v.get("commitment_id", "")))
        except Exception as e:
            _log_monitor_parse_failure("deflection", raw_def, e)

    # Pattern interruption: check and maybe set pending
    _check_pattern_interruption(move_log)

    return result, acc


def _load_move_log() -> list[str]:
    if not MOVE_LOG_PATH.exists():
        return []
    try:
        raw = json.loads(MOVE_LOG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_move_log(moves: list[str]) -> None:
    MOVE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(MOVE_LOG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(moves, f, indent=0)
        os.replace(tmp, str(MOVE_LOG_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_pattern_interruption(move_log: list[str]) -> None:
    """If same move in 5 of last 10, or no concrete_grounding for 12+, write pending interruption."""
    if len(move_log) < 5:
        return
    last_10 = move_log[-10:]
    from collections import Counter
    counts = Counter(last_10)
    for move, count in counts.items():
        if move != "none" and move != "concrete_grounding" and count >= PATTERN_THRESHOLD_SAME_MOVE:
            text = f"You have used the move '{move}' in {count} of the last 10 cycles. This cycle, you may not use that move. Find a different way."
            _set_pending_interruption(text)
            return
    # No concrete grounding for 12+ cycles?
    if len(move_log) >= PATTERN_THRESHOLD_NO_GROUNDING:
        if "concrete_grounding" not in move_log[-PATTERN_THRESHOLD_NO_GROUNDING:]:
            text = "You have not grounded your thinking in a specific human situation for 12+ cycles. This cycle, your thinking must be grounded in a specific human situation."
            _set_pending_interruption(text)


def _set_pending_interruption(text: str) -> None:
    PENDING_INTERRUPTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_INTERRUPTION_PATH.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    logger.info("Pattern interruption set for next cycle")


def get_pending_interruption() -> Optional[str]:
    """Return pending pattern interruption text if any; does not clear it."""
    if not PENDING_INTERRUPTION_PATH.exists():
        return None
    try:
        data = json.loads(PENDING_INTERRUPTION_PATH.read_text(encoding="utf-8"))
        return (data.get("text") or "").strip() or None
    except (json.JSONDecodeError, OSError):
        return None


def clear_pending_interruption() -> None:
    """Clear the pending pattern interruption (call when it was used as this cycle's injection)."""
    if PENDING_INTERRUPTION_PATH.exists():
        try:
            PENDING_INTERRUPTION_PATH.unlink()
        except OSError:
            pass


def evaluate_gate(monitoring: MonitoringResult) -> str:
    """Return 'passed' or 'blocked'. Block if substance has blocking phrase or repetition <= 2."""
    summary_lower = (monitoring.substance_summary or "").lower()
    if any(p in summary_lower for p in SUBSTANCE_BLOCK_PHRASES):
        return "blocked"
    if monitoring.repetition_score <= 2:
        return "blocked"
    return "passed"


def append_tension_history(
    cycle: int,
    thinking: str,
    tensions: list[Tension],
    monitoring_summary: str,
) -> list[Tension]:
    """
    For each active tension whose ID or description appears in thinking, append a history event.
    Uses a simple heuristic: if tension_id or a short substring of description is in thinking, add event.
    Event text uses monitoring cycle_summary when relevant, else a generic note.
    """
    out = list(tensions)
    for t in out:
        if t.status != "active":
            continue
        if t.tension_id in thinking:
            event = f"Referenced in cycle {cycle}. {monitoring_summary[:200]}"
        else:
            # Check if key phrase from description appears
            words = t.description.replace("?", "").split()[:8]
            phrase = " ".join(words)
            if len(phrase) >= 20 and phrase.lower() in thinking.lower():
                event = f"Referenced in cycle {cycle}. {monitoring_summary[:200]}"
            else:
                continue
        t.history.append(TensionHistory(cycle=cycle, event=event))
    return out


def check_silence_trigger(state: "StateFile", cycle: int, last_n_repetition_scores: list[int], last_n_commitment_updates: list[bool], last_n_new_tensions: list[bool]) -> Optional[dict]:
    """
    If repetition <=2 for 3 consecutive cycles AND no commitment updates in last 5 AND no new tensions in last 5,
    and we're past silence cooldown, return silence dict to set. Otherwise return None.
    Caller must mutate state.silence and state.last_silence_ended_cycle.
    """
    if state.silence and state.silence.get("active"):
        return None
    last_ended = state.last_silence_ended_cycle or 0
    if cycle - last_ended < SILENCE_COOLDOWN:
        return None
    if len(last_n_repetition_scores) < 3:
        return None
    if not all(s <= 2 for s in last_n_repetition_scores[-3:]):
        return None
    if len(last_n_commitment_updates) >= 5 and any(last_n_commitment_updates[-5:]):
        return None
    if len(last_n_new_tensions) >= 5 and any(last_n_new_tensions[-5:]):
        return None
    return {"active": True, "started_cycle": cycle, "remaining": SILENCE_DURATION}
