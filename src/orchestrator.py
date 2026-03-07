import json
import logging
import time
from datetime import datetime, timezone

from .config import DATA_DIR, API_PROVIDER, CYCLE_INTERVAL_SECONDS, FORCE_IMAGE, IMAGE_MODEL, MODEL_ID
from . import engine, journal, ledger, memory, monitor, resistance, retrieval
from .models import ImageDecision, TransitionEntry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sum_usage(*usage_dicts: dict | None) -> dict:
    """Sum prompt_tokens, completion_tokens, total_tokens from one or more usage dicts. Missing/None treated as 0."""
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for u in usage_dicts:
        if u:
            out["prompt_tokens"] += u.get("prompt_tokens", 0)
            out["completion_tokens"] += u.get("completion_tokens", 0)
            out["total_tokens"] += u.get("total_tokens", 0)
    return out


def _build_silence_context(state) -> str | None:
    """If we just emerged from silence (last completed cycle was silence end), return context text for the prompt."""
    if not state.last_silence_ended_cycle or state.cycle != state.last_silence_ended_cycle:
        return None
    return (
        "You were silent for the last 3 cycles. The monitoring layer detected you were circling. "
        "Resume your inquiry with fresh attention."
    )


def _load_last_n_cycle_metadata(n: int) -> tuple[list[int], list[bool], list[bool]]:
    """Load repetition scores, had_commitment_updates, had_new_tensions for last n cycles."""
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return [], [], []
    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)[:n]
    scores, commitment_flags, tension_flags = [], [], []
    for path in reversed(files):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mon = data.get("monitoring") or {}
            scores.append(int(mon.get("repetition_score", 3)))
            commitment_flags.append(bool(data.get("commitment_updates")))
            tension_flags.append(bool(data.get("tensions_new")))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            scores.append(3)
            commitment_flags.append(False)
            tension_flags.append(False)
    return scores, commitment_flags, tension_flags


def run_cycle() -> bool:
    memory.init_dirs()
    state = memory.load_state()
    cycle = state.cycle + 1
    timestamp = _now_iso()

    logger.info("--- Cycle %s starting at %s ---", cycle, timestamp)

    # Silence: skip engine, write minimal journal, decrement remaining
    if state.silence and state.silence.get("active") and state.silence.get("remaining", 0) > 0:
        state.cycle = cycle
        state.last_failure = None
        remaining = state.silence["remaining"] - 1
        state.silence["remaining"] = remaining
        if remaining <= 0:
            state.last_silence_ended_cycle = cycle
            state.silence = None
        memory.save_state(state)
        journal_text = f"# Cycle {cycle}\n**Date:** {timestamp[:10]}  **Mode:** (silence)\n\n---\n\nNo evolution this cycle."
        memory.save_journal_entry(cycle, journal_text)
        logger.info("Cycle %s: silence cycle complete", cycle)
        return True

    # ── Phase C: pre-cycle steps ──────────────────────────────────────────────
    # 1. Collect X replies into quarantine (runs silently if X_BEARER_TOKEN not set)
    try:
        from . import x_collector
        x_collector.collect_replies()
    except Exception as _e:
        logger.debug("Phase C: X collection skipped: %s", _e)

    # 2. Run filtering pipeline — move accepted quarantine submissions into human_challenges
    try:
        from . import filtering
        _accepted = filtering.run_filtering_pipeline()
        if _accepted:
            logger.info("Cycle %s: %d submission(s) accepted from filtering", cycle, len(_accepted))
    except Exception as _e:
        logger.warning("Phase C: filtering pipeline error (non-fatal): %s", _e)

    manuscript = memory.load_manuscript()
    transitions = memory.load_transitions()
    tensions = memory.load_tensions()
    commitments = memory.load_commitments()

    pattern_interruption = monitor.get_pending_interruption()
    injection = resistance.select_injection(cycle, tensions, commitments, pattern_interruption)
    if injection.source == "pattern_interruption":
        monitor.clear_pending_interruption()

    threatened, usage_threat = monitor.map_threats(injection.text, commitments)
    snippets = retrieval.retrieve_similar(tensions, injection.text, current_cycle=cycle)
    silence_context = _build_silence_context(state)

    try:
        output, system_prompt, user_message, raw_response, prompt_hash, usage_inquiry = engine.run(
            cycle,
            manuscript,
            transitions,
            tensions,
            commitments,
            archive_snippets=snippets,
            monitoring_feedback=state.last_monitoring,
            threatened_commitments=threatened,
            injection=injection,
            silence_context=silence_context,
        )
    except engine.EngineFailure as e:
        logger.error("Cycle %s: engine failed (%s): %s", cycle, e.reason, e.message)
        state.last_failure = {
            "timestamp": timestamp,
            "reason": e.reason,
            "attempted_cycle": cycle,
            "message": e.message,
        }
        memory.save_state(state)
        memory.append_failure_record({
            "timestamp": timestamp,
            "reason": e.reason,
            "attempted_cycle": cycle,
            "message": e.message,
        })
        return False
    except Exception as e:
        logger.exception("Cycle %s: engine failed: %s", cycle, e)
        state.last_failure = {
            "timestamp": timestamp,
            "reason": "api_error",
            "attempted_cycle": cycle,
            "message": str(e),
        }
        memory.save_state(state)
        memory.append_failure_record({
            "timestamp": timestamp,
            "reason": "api_error",
            "attempted_cycle": cycle,
            "message": str(e),
        })
        return False

    commitment_map = {c.commitment_id: c for c in commitments if c.status == "active"}
    recent_thinking = memory.load_recent_thinking(6)

    monitoring, usage_post = monitor.run_post_cycle(
        cycle,
        output.thinking,
        threatened,
        recent_thinking,
        commitment_map,
    )
    usage_monitoring = _sum_usage(usage_threat, usage_post)

    gate = monitor.evaluate_gate(monitoring)
    if gate == "blocked":
        output.manuscript_update = None
        output.commitment_updates = []
        output.tensions_resolved = []
        logger.info("Cycle %s: gate blocked (expressive output only)", cycle)

    state.cycle = cycle
    state.last_failure = None
    state.last_monitoring = monitoring

    tensions, state = memory.apply_tension_changes(
        tensions, output.tensions_new, output.tensions_resolved, state, cycle
    )
    commitments, state = memory.apply_commitment_changes(
        commitments, output.commitment_updates, state, cycle
    )

    if output.manuscript_update:
        memory.save_manuscript(output.manuscript_update)
    if output.transition_entry:
        memory.append_transition(
            TransitionEntry(cycle=cycle, timestamp=timestamp, entry=output.transition_entry)
        )

    tensions = monitor.append_tension_history(cycle, output.thinking, tensions, monitoring.cycle_summary)

    retrieval.store_cycle_embedding(cycle, output.thinking, tensions, injection.text, timestamp)

    # Silence trigger for next cycle (last 4 saved + this cycle = 5)
    scores, commit_flags, tension_flags = _load_last_n_cycle_metadata(4)
    scores.append(monitoring.repetition_score)
    commit_flags.append(len(output.commitment_updates) > 0)
    tension_flags.append(len(output.tensions_new) > 0)
    silence_dict = monitor.check_silence_trigger(state, cycle, scores, commit_flags, tension_flags)
    if silence_dict:
        state.silence = silence_dict
        logger.info("Cycle %s: silence triggered for next %s cycles", cycle, silence_dict.get("remaining", 0))

    memory.save_state(state)
    memory.save_tensions(tensions)
    memory.save_commitments(commitments)

    usage_inquiry_norm = usage_inquiry or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_monitoring_norm = usage_monitoring or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage = {
        "inquiry": usage_inquiry_norm,
        "monitoring": usage_monitoring_norm,
        "total_prompt_tokens": usage_inquiry_norm["prompt_tokens"] + usage_monitoring_norm["prompt_tokens"],
        "total_completion_tokens": usage_inquiry_norm["completion_tokens"] + usage_monitoring_norm["completion_tokens"],
        "total_tokens": usage_inquiry_norm["total_tokens"] + usage_monitoring_norm["total_tokens"],
    }
    # ── Phase C: post-cycle steps ─────────────────────────────────────────────
    # 3. Image generation (before journal render so image reference can be embedded)
    if FORCE_IMAGE and not output.image_decision.create:
        fallback = (output.summary or manuscript.strip() or "Contemplative inquiry into meaning.").strip()
        if len(fallback) > 400:
            fallback = fallback[:397] + "..."
        output.image_decision = ImageDecision(create=True, prompt=fallback)
        logger.info("Cycle %s: FORCE_IMAGE=1, using prompt: %s", cycle, fallback[:80])
    image_path = None
    if output.image_decision.create and (output.image_decision.prompt or output.image_decision.beyond_words or output.image_decision.concept):
        try:
            from . import images
            image_path = images.generate_image(cycle, output.image_decision)
        except Exception as _e:
            logger.warning("Phase C: image generation error (non-fatal): %s", _e)

    journal_text = journal.render_journal_entry(
        cycle=cycle,
        timestamp=timestamp,
        mode=output.mode,
        thinking=output.thinking,
        tensions_new=output.tensions_new,
        tensions_resolved=output.tensions_resolved,
        transition_entry=output.transition_entry,
        usage=usage,
        title=output.title,
        summary=output.summary,
        image_path=image_path,
    )
    memory.save_journal_entry(cycle, journal_text)

    ledger.save_prompt_record(cycle, system_prompt, user_message, raw_response, prompt_hash)

    cycle_record = {
        "cycle": cycle,
        "timestamp": timestamp,
        "title": output.title,
        "mode": output.mode,
        "injection": injection.model_dump(),
        "thinking": output.thinking,
        "tensions_new": [t.model_dump() for t in output.tensions_new],
        "tensions_resolved": [t.model_dump() for t in output.tensions_resolved],
        "manuscript_updated": bool(output.manuscript_update),
        "transition_entry": output.transition_entry,
        "commitment_updates": [u.model_dump() for u in output.commitment_updates],
        "summary": output.summary,
        "social_output": output.social_output,
        "image_decision": output.image_decision.model_dump(),
        "monitoring": {
            "repetition_score": monitoring.repetition_score,
            "repetition_comment": monitoring.repetition_comment,
            "substance_summary": monitoring.substance_summary,
            "cycle_summary": monitoring.cycle_summary,
            "deflection_flags": monitoring.deflection_flags,
            "move_classification": monitoring.move_classification,
            "self_reference_ratio": monitoring.self_reference_ratio,
            "has_concrete_grounding": monitoring.has_concrete_grounding,
        },
        "gate": gate,
        "model_id": MODEL_ID,
        "provider": API_PROVIDER,
        "prompt_hash": prompt_hash,
        "usage": usage,
        # Phase C additions
        "image_path": str(image_path) if image_path else None,
        "image_model": IMAGE_MODEL if image_path else None,
    }

    # 4. Social posting (after building cycle_record so we can update it)
    if output.social_output:
        try:
            from . import social
            social_results = social.post_social(
                text=output.social_output,
                image_path=image_path,
                cycle=cycle,
                title=output.title,
            )
            cycle_record["social_results"] = social_results
            logger.info("Cycle %s: social posting results: %s", cycle, social_results)
        except Exception as _e:
            logger.warning("Phase C: social posting error (non-fatal): %s", _e)

    memory.save_cycle_record(cycle, cycle_record)

    # 5. Paradigm shift detection
    try:
        from . import insights
        shifts = insights.detect_paradigm_shifts(cycle, output, tensions, commitments)
        if shifts:
            logger.info("Cycle %s: %d paradigm shift(s) detected", cycle, len(shifts))
    except Exception as _e:
        logger.warning("Phase C: paradigm shift detection error (non-fatal): %s", _e)

    logger.info(
        "Cycle %s: complete mode=%s gate=%s injection=%s",
        cycle, output.mode, gate, injection.source,
    )
    return True


def run(max_cycles: int | None = None) -> None:
    logger.info("Meaning Seeker starting" + (f" (run {max_cycles} cycles)" if max_cycles else ""))
    memory.init_dirs()

    successful_cycles = 0
    while True:
        try:
            if run_cycle():
                successful_cycles += 1
                if max_cycles is not None and successful_cycles >= max_cycles:
                    logger.info("Completed %s cycle(s), stopping", successful_cycles)
                    return
        except KeyboardInterrupt:
            logger.info("Shutting down")
            return
        except Exception as e:
            logger.exception("Unhandled error in cycle: %s", e)

        logger.info("Sleeping %ss until next cycle", CYCLE_INTERVAL_SECONDS)
        try:
            time.sleep(CYCLE_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutting down")
            return
