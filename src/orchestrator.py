import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .config import (
    CYCLE_DAILY_AT_UTC_PARSED,
    CYCLE_INTERVAL_SECONDS,
    DATA_DIR,
    API_PROVIDER,
    FORCE_IMAGE,
    FORCE_WEEKLY_REVIEW,
    IMAGE_MODEL,
    MODEL_ID,
)
from . import engine, journal, ledger, memory, monitor, resistance, retrieval
from .models import ImageDecision, TransitionEntry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_until_next_daily_run() -> int | None:
    """If CYCLE_DAILY_AT_UTC is set, return seconds until next occurrence. Else None."""
    if not CYCLE_DAILY_AT_UTC_PARSED:
        return None
    hour, minute = CYCLE_DAILY_AT_UTC_PARSED
    now = datetime.now(timezone.utc)
    today_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    next_run = today_at if now < today_at else today_at + timedelta(days=1)
    return max(0, int((next_run - now).total_seconds()))


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


def _cycles_since_last_image() -> int:
    """Return how many cycles have passed since the last cycle that generated an image.
    Returns a large sentinel (999) if no image has ever been generated.
    """
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return 999
    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("image_decision") or {}).get("create"):
                last_image_cycle = int(path.stem)
                # current cycle hasn't been saved yet; state.cycle is the last completed
                # We read cycle numbers from filenames; the current cycle = max + 1
                all_cycles = [int(p.stem) for p in files]
                current_cycle = max(all_cycles) + 1
                return current_cycle - last_image_cycle
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            continue
    return 999


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

    # Touch state so web shows "Running" during long cycles (state mtime < 15 min)
    (DATA_DIR / "state.json").touch()

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

    # Sunday weekly review or normal injection
    is_sunday = datetime.now(timezone.utc).weekday() == 6
    use_weekly_review = bool(FORCE_WEEKLY_REVIEW)
    if not use_weekly_review and CYCLE_DAILY_AT_UTC_PARSED and is_sunday:
        use_weekly_review = True
    elif not use_weekly_review and not CYCLE_DAILY_AT_UTC_PARSED and cycle % 7 == 0:
        use_weekly_review = True

    if use_weekly_review:
        if FORCE_WEEKLY_REVIEW:
            logger.info("Cycle %s: FORCE_WEEKLY_REVIEW=1, running weekly review", cycle)
        injection = resistance.build_weekly_review_injection(cycle, state)
    else:
        pattern_interruption = monitor.get_pending_interruption()
        injection = resistance.select_injection(cycle, tensions, commitments, pattern_interruption)
        if injection.source == "pattern_interruption":
            monitor.clear_pending_interruption()

    threatened, usage_threat = monitor.map_threats(injection.text, commitments)
    snippets = retrieval.retrieve_similar(tensions, injection.text, current_cycle=cycle)
    silence_context = _build_silence_context(state)
    cycles_since_image = _cycles_since_last_image()

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
            cycles_since_last_image=cycles_since_image,
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

    # Weekly review: additional monitoring for challenge engagement and defensiveness
    if injection.source == "weekly_review":
        hc_texts = []
        if injection.challenges_addressed:
            hc_map_pre = {c["id"]: c for c in resistance.load_human_challenges()}
            hc_texts = [
                (hc_map_pre.get(cid) or {}).get("text", "").strip()
                for cid in injection.challenges_addressed
                if (hc_map_pre.get(cid) or {}).get("text", "").strip()
            ]
        commitment_update_dicts = [u.model_dump() for u in output.commitment_updates]
        review_engagement, review_defensive, usage_review = monitor.run_weekly_review_monitoring(
            output.thinking, hc_texts, commitment_update_dicts,
        )
        monitoring.review_challenge_engagement = review_engagement
        monitoring.review_defensive = review_defensive
        usage_monitoring = _sum_usage(usage_threat, usage_post, usage_review)
    else:
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

    journal_mode = "weekly_review" if injection.source == "weekly_review" else output.mode
    woven_challenge = injection.woven_challenge if injection.woven_challenge else None
    review_challenges = None
    if injection.source == "weekly_review" and (
        (injection.challenges_addressed and len(injection.challenges_addressed) > 0)
        or (injection.challenges_mentioned and len(injection.challenges_mentioned) > 0)
    ):
        hc_map = {c["id"]: c for c in resistance.load_human_challenges()}
        ids = (injection.challenges_addressed or []) + (injection.challenges_mentioned or [])
        review_challenges = [
            {"text": (hc_map.get(i) or {}).get("text", ""), "cycle": (hc_map.get(i) or {}).get("linked_cycle")}
            for i in ids
        ]

    journal_text = journal.render_journal_entry(
        cycle=cycle,
        timestamp=timestamp,
        mode=journal_mode,
        thinking=output.thinking,
        tensions_new=output.tensions_new,
        tensions_resolved=output.tensions_resolved,
        transition_entry=output.transition_entry,
        usage=usage,
        title=output.title,
        summary=output.summary,
        image_path=image_path,
        woven_challenge=woven_challenge,
        review_challenges=review_challenges,
    )
    memory.save_journal_entry(cycle, journal_text)

    # Challenge System v2: post-cycle status updates
    # Woven challenges are marked at selection time in resistance.select_injection.
    # Weekly review challenges are addressed here because they need the journal entry to exist first.
    if injection.source == "weekly_review":
        for cid in (injection.challenges_addressed or []) + (injection.challenges_mentioned or []):
            resistance.update_challenge_status(
                cid,
                "reviewed",
                linked_cycle=cycle,
                linked_journal=str(cycle),
            )
        resistance.run_weekly_review_lapse(state)
        state.last_review_at = timestamp
        memory.save_state(state)

    ledger.save_prompt_record(cycle, system_prompt, user_message, raw_response, prompt_hash)

    cycle_record = {
        "cycle": cycle,
        "timestamp": timestamp,
        "title": output.title,
        "mode": journal_mode,
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
            "review_challenge_engagement": monitoring.review_challenge_engagement,
            "review_defensive": monitoring.review_defensive,
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

    def sleep_until_next() -> None:
        sleep_sec = _seconds_until_next_daily_run()
        if sleep_sec is not None:
            hour, minute = CYCLE_DAILY_AT_UTC_PARSED
            logger.info(
                "Sleeping until next cycle at %02d:%02d UTC (%ds)",
                hour,
                minute,
                sleep_sec,
            )
        else:
            logger.info("Sleeping %ss until next cycle", CYCLE_INTERVAL_SECONDS)
            sleep_sec = CYCLE_INTERVAL_SECONDS
        time.sleep(sleep_sec)

    # When using daily-at, sleep until next scheduled time before first run
    if CYCLE_DAILY_AT_UTC_PARSED:
        try:
            sleep_until_next()
        except KeyboardInterrupt:
            logger.info("Shutting down")
            return

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

        try:
            sleep_until_next()
        except KeyboardInterrupt:
            logger.info("Shutting down")
            return
