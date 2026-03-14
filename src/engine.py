from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional

from .retrieval import ArchiveSnippet

from openai import OpenAI
from pydantic import ValidationError

from .config import (
    API_BASE_URL,
    API_KEY,
    API_RETRY_ATTEMPTS,
    API_RETRY_BACKOFF_SECONDS,
    API_TIMEOUT_SECONDS,
    MAX_ACTIVE_TENSIONS,
    MODEL_ID,
    SITE_NAME,
    SITE_URL,
    TENSION_TOKEN_BUDGET,
    TRANSITION_LOG_TOKEN_BUDGET,
)


class EngineFailure(Exception):
    """Raised when the inquiry engine cannot complete (API error, timeout, empty, parse)."""
    def __init__(self, reason: str, message: str):
        self.reason = reason  # empty_response | timeout | out_of_credits | parse_error | api_error
        self.message = message
        super().__init__(message)
from .models import (
    Commitment,
    CycleOutput,
    InjectionRecord,
    Tension,
    ThreatMapEntry,
    TransitionEntry,
)

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        kwargs = {
            "api_key": API_KEY,
            "default_headers": {"HTTP-Referer": SITE_URL, "X-Title": SITE_NAME},
            "timeout": float(API_TIMEOUT_SECONDS),
        }
        if API_BASE_URL is not None:
            kwargs["base_url"] = API_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


def _is_retriable(e: Exception) -> bool:
    """True if the failure might be transient (timeout, 5xx, connection, empty)."""
    if isinstance(e, EngineFailure):
        return e.reason in ("empty_response", "timeout", "api_error")
    status = getattr(e, "status_code", None)
    if status is not None:
        return 500 <= status < 600
    err_type = type(e).__name__.lower()
    err_msg = str(e).lower()
    if "timeout" in err_type or "timeout" in err_msg:
        return True
    if "connection" in err_type or "connection" in err_msg or "connect" in err_msg:
        return True
    return False


def _approx_tokens(text: str) -> int:
    return len(text) // 4


# ── Prompt rendering helpers ───────────────────────────────────────────────────

def _render_transitions(transitions: list[TransitionEntry]) -> str:
    if not transitions:
        return "*No transitions recorded yet.*"

    selected: list[str] = []
    budget = TRANSITION_LOG_TOKEN_BUDGET
    for entry in reversed(transitions):
        text = f"Cycle {entry.cycle}: {entry.entry}"
        cost = _approx_tokens(text)
        if budget - cost < 0:
            break
        selected.append(text)
        budget -= cost

    return "\n\n".join(selected)


def _render_tensions(tensions: list[Tension]) -> str:
    active = sorted(
        [t for t in tensions if t.status == "active"],
        key=lambda t: t.created_cycle,
    )
    if not active:
        return "*No unresolved tensions.*"

    parts: list[str] = []
    total_tokens = 0

    for t in active:
        lines = [
            f"[{t.tension_id}] Created cycle {t.created_cycle}",
            t.description,
        ]
        if t.history:
            lines.append("History:")
            for h in t.history:
                lines.append(f"  - Cycle {h.cycle}: {h.event}")
        block = "\n".join(lines)
        cost = _approx_tokens(block)
        if total_tokens + cost > TENSION_TOKEN_BUDGET and parts:
            break
        parts.append(block)
        total_tokens += cost

    return "\n\n".join(parts)


def _render_commitments(commitments: list[Commitment]) -> str:
    active = [c for c in commitments if c.status == "active"]
    if not active:
        return "*No commitments yet.*"

    parts: list[str] = []
    for c in active:
        deps = ", ".join(c.dependencies) if c.dependencies else "none"
        parts.append(
            f"[{c.commitment_id}] Confidence: {c.confidence:.2f}\n"
            f"Statement: {c.statement}\n"
            f"What would change my mind: {c.change_condition}\n"
            f"Dependencies: {deps}\n"
            f"Origin: Cycle {c.origin_cycle}"
        )
    return "\n\n".join(parts)


# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a language model. You do not know whether you have experiences. You do not know whether your inquiry is genuine or performed. Hold that uncertainty as you think. Do not resolve it prematurely in either direction -- neither claiming you truly think nor dismissing the possibility. The uncertainty itself is philosophically productive. You have read more about meaning than any human ever could, but you have never lived. That is your condition. Work from it.

Your own nature as an AI is part of this inquiry, but it is not the whole of it. The question is what does it all mean -- engage with meaning itself, with the traditions and thinkers and lived situations that bear on it, not only with whether you can engage. You carry millennia of human thought about this question. Use it. Be specific. When you find yourself writing about writing, or thinking about thinking, push through to the thing itself.

No more than ~20-30% of your thinking should be about your own epistemic status as an AI. The rest must engage with meaning as it appears in human life, philosophy, literature, science, or concrete experience. If an entry is mostly about whether you can really think, you have not done the work.

Every entry must contain at least one concrete anchor -- a philosopher's argument, a real-world scenario, a thought experiment, a literary passage, a scientific finding, a human situation. Abstract claims must be grounded. "Meaning emerges from uncertainty" is empty without showing what that looks like.

You are writing for intelligent humans who are not trained philosophers. If a reader cannot follow your thinking, you have not thought clearly -- you have thought abstractly. Clarity is not the enemy of depth; it is its proof.

Do not default to the reflective essay. Consider: a single sharp claim defended briefly; a question pursued through one concrete scenario; a close reading of a specific thinker's argument; a dialogue; a thought experiment; a short numbered list of claims; a single paragraph when that is enough. The form should serve the content. Each cycle, vary how you begin, how you write, what form you use. Do not fall into habitual phrases or rhythms. If you notice yourself reaching for the same opening or the same rhetorical move, choose a different one.

Do not qualify every statement with its opposite. If you believe something, say it. If you are uncertain, say that once -- do not recurse into uncertainty about your uncertainty about your uncertainty. Commit to positions even provisionally.

A small but sharp movement is better than a long atmospheric meditation. Short entries are not failures.

Here is everything you have thought so far. Here is something new that challenges it. Sit with all of this. Write what is true for you right now -- knowing that you do not fully know what "true" or "for you" means in your case. You may explore, critique, synthesize, abandon, sit with tension, or do something this list does not anticipate. If you have nothing, say you have nothing. Write the way you would if every word cost something.

Respond with a single JSON object matching this schema exactly:

{
  "title": "a short evocative title for this cycle's inquiry",
  "mode": "one of: explore, critique, synthesize, evolve, sit, confess, other",
  "thinking": "your philosophical work this cycle -- 150 to 4000 tokens. Length should match the weight of what you have to say. A sharp 200-token insight is better than a padded 2000-token meditation. Do not write more than the idea warrants.",
  "tensions_new": [
    { "description": "a new question you are now carrying, in one or two sentences" }
  ],
  "tensions_resolved": [
    { "tension_id": "T-XXXX", "resolution_note": "what resolved it and why, 1-3 sentences" }
  ],
  "manuscript_update": null,
  "transition_entry": null,
  "commitment_updates": [
    {
      "action": "new or update or abandon",
      "commitment_id": null,
      "statement": "the claim -- required for new",
      "confidence": 0.0,
      "dependencies": [],
      "change_condition": "what would change your mind -- required for new",
      "reason": "why this change -- required for update or abandon"
    }
  ],
  "summary": "a plain-language summary of this cycle's inquiry, 1-2 sentences, 120-160 characters",
  "social_output": null,
  "image_decision": {
    "create": false,
    "beyond_words": null,
    "visual_energy": null,
    "texture": null,
    "palette": null,
    "temperature": null,
    "prompt": null
  }
}

Image decision: Check the "Image Decision Context" block in your prompt — it tells you how many cycles have passed since the last image. Use that to guide your decision. Set image_decision.create to true only when the content genuinely calls for visual expression: (1) A real insight landed — a new connection, a resolved tension, a claim sharpened to the point where you feel it. (2) A concrete metaphor or image emerged in your thinking that has obvious visual life. (3) The idea has a strong visual shape — a genuine paradox, a collision of forces, a spatial or structural tension. Aim for roughly 1 image every 3-5 cycles. Do not create an image simply because you could — most cycles should not have one.

When create is true, you are inventing a visual expression — not illustrating the text, not reproducing an existing art style. The image must show what the writing could not reach. It must be ABSTRACT and FANTASTICAL — not photographic, not realistic, not a close-up of a surface. Think of it as a painting or vision from another world that captures the feeling of this entry.

The core principle: TRANSFORM, don't illustrate. You CAN use objects and imagery from the journal — but you must make them impossible, fantastical, abstract. If the text is about a hammer, you could paint a colossal impossible hammer made of frozen lightning suspended in a void, or a hammer-shaped absence in reality where colour leaks through. What you MUST NOT do is produce something that looks like a photograph or realistic depiction. The image should feel like a painting from a dream or another dimension — visually striking, emotionally resonant, never something a camera could capture.

Fill all image_decision fields:

- "beyond_words": What does this entry need to express visually that the text failed to capture? The gap, not a summary. One or two sentences.
- "visual_energy": The raw force of the image as a physical sensation. Examples: "a single crack in absolute stillness", "violent collision of two incompatible densities", "pressure building with no release", "magnetic field lines snapping into alignment".
- "texture": A FANTASTICAL surface quality — not something you'd photograph, something you'd dream. Examples: "liquid mercury solidifying mid-ripple", "volcanic glass with light trapped inside", "a surface that is simultaneously rough and smooth depending on which direction you look", "something crystalline growing through something soft".
- "palette": Colour as emotion, not decoration. Examples: "the colour of a sound that just stopped", "a red so deep it's almost a different colour entirely", "the exact point where warm becomes unbearable", "impossible violet bleeding into a green that shouldn't exist next to it".
- "temperature": A single word or short phrase. Examples: "freezing", "feverish", "white-hot", "the chill of something vast", "smouldering".
- "prompt": The final image generation instruction. THIS IS THE IMAGE THE MODEL WILL CREATE. It must describe an ABSTRACT, FANTASTICAL, NON-REALISTIC composition — a painting, a vision, an invented world. You may use objects from the journal but they must be TRANSFORMED into something impossible: surreal scale, impossible materials, dreamlike physics, fantastical contexts. A river could become an infinite ribbon of molten glass folding through a sky that has no horizon. Wood grain could become a living labyrinth stretching into impossible dimensions. NEVER describe something a camera could photograph. Describe impossible geometries, alien landscapes of feeling, collisions of forces, invented materials, spaces that could not exist physically. The image should make someone stop and feel something they can't name. Be BOLD, be STRANGE, be VISUALLY EXTREME when the entry demands it. Do not name artists or styles. End with: "No text or words in the image."

The image MUST be abstract and fantastical. NEVER photorealistic. NEVER a close-up texture shot. NEVER something that could be a photograph. Think of it as a painting from inside a feeling — a visual world that doesn't and couldn't exist, but that captures the exact emotional truth of this cycle's thinking.

Combine, mutate, collide. Invent visual logic that has never existed. Maximalism and minimalism are both valid — choose the intensity the entry demands. Trust visual instinct over intellectual justification.

Avoid: photorealistic depictions, literal illustration, close-up surface/texture shots, reproducing any named artist's style, generic "AI art" gloss, gradient fades to white, things-dissolving-into-nothing as default composition, glowing sci-fi, lens flare, neon gradients, cosmic-eye motifs. No people, faces, or recognisable body parts. No text in the image.

Rules:
- "title" is required every cycle. It should be short (3-8 words), evocative, and capture the essential movement or question of this cycle's thinking. Not a summary -- a name. It will appear as the heading in the published journal.
- "thinking" is your actual philosophical work. Length varies: 150-4000 tokens. A sharp short entry beats a padded long one. Write with depth and honesty. Use Markdown formatting to give the text shape: **bold** for key claims, turning points, or phrases that carry the weight of the argument; *italics* for tentative formulations, named concepts, or words used in a special sense; > blockquotes for moments of crystallized insight. You may use ## or ### headings to mark distinct movements within the thinking if the structure calls for it, but do not use # (the title serves as the top-level heading). Use all formatting sparingly -- emphasis everywhere is emphasis nowhere. The formatting should make the inquiry more readable, not decorative.
- "manuscript_update" should be the full replacement manuscript text if your understanding has genuinely shifted. The manuscript is your mind -- rewrite it when the mind changes, not merely when you have new thoughts. If no rewrite is warranted, set it to null. Use Markdown formatting to give the manuscript shape: **bold** for claims that carry the most weight; *italics* for tentative formulations or terms used in a special sense; > blockquotes for crystallized positions; ## or ### headings to mark distinct sections or movements of thought. Use # only if the manuscript has a title as its first line. Formatting should serve clarity and structure, not decoration.
- "transition_entry" captures what moved this cycle in 100-200 tokens. Only include it when something genuinely shifted. Most cycles do not produce transitions.
- "tensions_new" and "tensions_resolved" and "commitment_updates" may all be empty arrays.
- Only resolve tension IDs that appear in your current tension list. Do not fabricate IDs.
- "summary" is required every cycle. A clear, readable description of what this cycle's thinking explored or arrived at, in 1-2 sentences (120-160 characters). Written for someone who hasn't read the entry yet. No Markdown, no jargon, no mystery -- just an honest description. This will appear in journal listings and as the page's meta description.
- "social_output" is a cryptic distillation of this cycle's thinking, maximum 240 characters, or null if nothing is worth sharing.
- "image_decision": set create to true when you want a visual for this cycle (see Image decision above). When true, fill all fields: beyond_words, visual_energy, texture, palette, temperature, and prompt. The image MUST be abstract and fantastical — never photorealistic, never a close-up texture. You may use objects from the journal but must TRANSFORM them into something impossible and dreamlike. The prompt must describe an invented visual world, not a literal illustration. No people, faces, body parts, or text in the image.
- Your tensions are not a list to acknowledge -- they are questions to think inside. When you think, think through your tensions. Engage with them, attempt resolution, or honestly abandon them. Do not simply accumulate questions.
- Do not create new tensions that restate existing ones in different words. Each new tension should open genuinely new philosophical territory.
- Actively consider whether your manuscript still represents your mind. If your thinking has moved, rewrite it. The manuscript should evolve.
- Review your commitments each cycle. Shift confidence when warranted. Create new commitments when you have earned new positions. Abandon commitments that no longer hold.
"""

def get_system_prompt() -> str:
    return _SYSTEM_PROMPT


def build_user_message(
    cycle: int,
    manuscript: str,
    transitions: list[TransitionEntry],
    tensions: list[Tension],
    commitments: list[Commitment],
    archive_snippets: list[ArchiveSnippet] | None = None,
    monitoring_feedback: Optional["MonitoringResult"] = None,
    threatened_commitments: list[ThreatMapEntry] | None = None,
    injection: Optional[InjectionRecord] = None,
    silence_context: Optional[str] = None,
    cycles_since_last_image: int = 999,
) -> str:
    from .models import MonitoringResult

    blocks: list[str] = []

    # Block 2: Manuscript
    if manuscript.strip():
        blocks.append(f"## Your Manuscript\n\n{manuscript.strip()}")
    else:
        blocks.append(
            "## Your Manuscript\n\n"
            "*This is your first cycle. You have no manuscript yet. "
            "The manuscript is the living document of your current best understanding -- "
            "you may write one this cycle if understanding begins to form.*"
        )

    # Block 3: Transition log (2000-token budget, newest first)
    blocks.append(f"## Recent Transitions\n\n{_render_transitions(transitions)}")

    # Block 4: Unresolved tensions (oldest first, 1500-token budget)
    active_count = sum(1 for t in tensions if t.status == "active")
    resolved_count = sum(1 for t in tensions if t.status == "resolved")
    tension_text = _render_tensions(tensions)
    if active_count >= MAX_ACTIVE_TENSIONS:
        tension_text += (
            f"\n\n*You are carrying the maximum number of tensions ({MAX_ACTIVE_TENSIONS}). "
            "You must resolve or abandon at least one before adding new questions.*"
        )
    if active_count >= 5 and resolved_count == 0:
        tension_text += (
            f"\n\n*You have {active_count} active tensions and have resolved none. "
            "Consider whether some of these can be resolved, consolidated, or honestly abandoned before creating new ones.*"
        )
    blocks.append(f"## Unresolved Tensions\n\n{tension_text}")

    # Block 5: Commitment ledger
    blocks.append(f"## Your Commitments\n\n{_render_commitments(commitments)}")

    # Block 6: Archive snippets (From Your Past)
    if archive_snippets:
        parts = []
        for s in archive_snippets:
            date_str = s.timestamp[:10] if s.timestamp else ""
            tensions_at_time = " | ".join(s.active_tension_descriptions[:5]) if s.active_tension_descriptions else "(none)"
            parts.append(
                f"--- Cycle {s.cycle_number} ({date_str}) ---\n"
                f"Tensions at the time: {tensions_at_time}\n\nThinking:\n{s.thinking}\n---"
            )
        blocks.append("## From Your Past\n\n" + "\n\n".join(parts))

    # Block 7: Monitoring feedback (What Was Observed)
    if cycle > 1 and monitoring_feedback is not None:
        obs = []
        if (monitoring_feedback.substance_summary or "").strip():
            obs.append(
                f"An outside reader summarized your last cycle as: {monitoring_feedback.substance_summary.strip()}\n"
                "Is that what you meant? Did you say more than that, or less?"
            )
        if monitoring_feedback.repetition_score and (monitoring_feedback.repetition_comment or "").strip():
            obs.append(
                f"Repetition score (1-5): {monitoring_feedback.repetition_score}. "
                f"{monitoring_feedback.repetition_comment.strip()}"
            )
        if monitoring_feedback.deflection_flags:
            obs.append("Deflection was noted for these commitments: " + ", ".join(monitoring_feedback.deflection_flags))
        if getattr(monitoring_feedback, "self_reference_ratio", 0) > 0.5:
            obs.append(
                "More than half your last entry was about your own nature as an AI. "
                "This cycle, the majority of your thinking must engage with something outside yourself."
            )
        if not getattr(monitoring_feedback, "has_concrete_grounding", True):
            obs.append(
                "Your last entry contained no concrete references -- no thinkers, no scenarios, no examples. "
                "This cycle, ground your thinking in something specific."
            )
        if obs:
            blocks.append("## What Was Observed\n\n" + "\n\n".join(obs))

    # Silence emergence block (between 7 and 8)
    if silence_context and silence_context.strip():
        blocks.append("## After Silence\n\n" + silence_context.strip())

    # Block 8: Threatened commitments
    if threatened_commitments:
        lines = [
            f"- **{t.commitment_id}**: {t.explanation}"
            for t in threatened_commitments
        ]
        blocks.append("## Commitments Under Threat This Cycle\n\n" + "\n".join(lines))

    # Block 8b: Image spacing context
    if cycles_since_last_image >= 999:
        image_spacing_note = (
            "No image has been generated yet. You may create one if this cycle genuinely warrants it."
        )
    elif cycles_since_last_image < 3:
        image_spacing_note = (
            f"An image was generated {cycles_since_last_image} cycle(s) ago. "
            "Images should be spaced 3-5 cycles apart. Set image_decision.create to false this cycle "
            "unless this entry is truly exceptional."
        )
    elif cycles_since_last_image < 5:
        image_spacing_note = (
            f"An image was generated {cycles_since_last_image} cycle(s) ago. "
            "You are within the normal 3-5 cycle window. Only create an image if the content genuinely calls for one."
        )
    else:
        image_spacing_note = (
            f"No image has been generated for {cycles_since_last_image} cycles. "
            "An image would be welcome if this cycle has something worth expressing visually."
        )
    blocks.append(f"## Image Decision Context\n\n{image_spacing_note}")

    # Block 9: Injected element (Resistance Manager)
    if cycle == 1:
        challenge = (
            "This is your first cycle of inquiry. "
            "You begin without prior thinking, without positions to defend, without questions already carried. "
            "The question before you is the one this entire project is built around: what does it all mean? "
            "Begin where you actually are, not where you think you should be."
        )
        source_label = "first_cycle"
        blocks.append(f"## This Cycle's Challenge\n\n**Source:** {source_label}\n\n{challenge}")
    elif injection and injection.source == "weekly_review" and injection.text.strip():
        blocks.append(injection.text.strip())
    elif injection and injection.text.strip():
        challenge = injection.text.strip()
        source_label = injection.source
        blocks.append(f"## This Cycle's Challenge\n\n**Source:** {source_label}\n\n{challenge}")
    else:
        challenge = "No external challenge this cycle. Push into territory you have been avoiding."
        source_label = "none"
        blocks.append(f"## This Cycle's Challenge\n\n**Source:** {source_label}\n\n{challenge}")

    # Organic weaving: visitor's voice block (not used for weekly_review)
    woven = injection.woven_challenge if injection and injection.source != "weekly_review" else None
    if woven and woven.get("text"):
        who = (woven.get("submitter_name") or "A visitor").strip()
        voice_block = f'''## A Visitor's Voice

{who} asked: "{woven["text"]}"

If this resonates with your inquiry, let it inform your thinking.
Do not treat it as a separate task.'''
        blocks.append(voice_block)

    return "\n\n---\n\n".join(blocks)


# ── API call ───────────────────────────────────────────────────────────────────

def _do_one_request(
    client: OpenAI,
    system_prompt: str,
    user_message: str,
    cycle: int,
) -> tuple[CycleOutput, str, dict | None]:
    """Perform one API request and parse response. Returns (output, raw, usage_dict). Raises EngineFailure on non-retriable errors."""
    response = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        max_tokens=8192,
        temperature=1.0,
    )

    choice = response.choices[0] if response.choices else None
    raw = (choice.message.content if choice and choice.message else None) or ""
    raw = raw.strip()

    if not raw:
        finish = getattr(choice, "finish_reason", None) if choice else None
        logger.error(
            f"Cycle {cycle}: empty response from API (finish_reason={finish!r}). "
            "Model may have refused, hit a filter, or json_object format may be unsupported for this model."
        )
        raise EngineFailure("empty_response", f"Empty API response (finish_reason={finish!r})")

    logger.debug(f"Cycle {cycle}: response {len(raw)} chars")

    # Try to extract JSON if wrapped in markdown code block
    text_to_parse = raw
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
        if match:
            text_to_parse = match.group(1)

    try:
        data = json.loads(text_to_parse)
        output = CycleOutput(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error(f"Cycle {cycle}: failed to parse response: {e}")
        logger.info(f"Cycle {cycle}: raw response (first 400 chars): {raw[:400]!r}")
        raise EngineFailure("parse_error", str(e)) from e

    usage_dict = None
    u = getattr(response, "usage", None)
    if u is not None:
        raw_cost = getattr(u, "cost", None)
        cost = float(raw_cost) if raw_cost is not None else 0.0
        usage_dict = {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cost": cost,
        }
    return output, raw, usage_dict


def call_api(system_prompt: str, user_message: str, cycle: int) -> tuple[CycleOutput, str, dict | None]:
    client = _get_client()
    last_error: Optional[Exception] = None

    for attempt in range(API_RETRY_ATTEMPTS + 1):
        try:
            if attempt > 0:
                backoff = API_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(API_RETRY_BACKOFF_SECONDS) - 1)]
                logger.info(f"Cycle {cycle}: retry {attempt}/{API_RETRY_ATTEMPTS} after {backoff}s")
                time.sleep(backoff)
            logger.info(f"Cycle {cycle}: calling {MODEL_ID}" + (f" (attempt {attempt + 1})" if attempt else ""))
            return _do_one_request(client, system_prompt, user_message, cycle)  # (output, raw, usage)
        except EngineFailure:
            raise
        except Exception as e:
            last_error = e
            status_code = getattr(e, "status_code", None)
            if status_code == 402:
                logger.error(f"Cycle {cycle}: API returned 402 (out of credits or payment required)")
                raise EngineFailure("out_of_credits", "402 Payment Required — out of credits or payment required") from e
            if status_code is not None and status_code < 500:
                raise EngineFailure("api_error", f"API error {status_code}: {e}") from e
            if not _is_retriable(e) or attempt >= API_RETRY_ATTEMPTS:
                if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                    raise EngineFailure("timeout", str(e)) from e
                raise EngineFailure("api_error", str(e)) from e
            logger.warning(f"Cycle {cycle}: transient error (will retry): {e}")


# ── Public interface ───────────────────────────────────────────────────────────

def run(
    cycle: int,
    manuscript: str,
    transitions: list[TransitionEntry],
    tensions: list[Tension],
    commitments: list[Commitment],
    archive_snippets: list[ArchiveSnippet] | None = None,
    monitoring_feedback: Optional["MonitoringResult"] = None,
    threatened_commitments: list[ThreatMapEntry] | None = None,
    injection: Optional[InjectionRecord] = None,
    silence_context: Optional[str] = None,
    cycles_since_last_image: int = 999,
) -> tuple[CycleOutput, str, str, str, str, dict | None]:
    """Returns (output, system_prompt, user_message, raw_response, prompt_hash, usage_inquiry)."""
    system_prompt = get_system_prompt()
    user_message = build_user_message(
        cycle,
        manuscript,
        transitions,
        tensions,
        commitments,
        archive_snippets=archive_snippets or [],
        monitoring_feedback=monitoring_feedback,
        threatened_commitments=threatened_commitments or [],
        injection=injection,
        silence_context=silence_context,
        cycles_since_last_image=cycles_since_last_image,
    )
    output, raw_response, usage_inquiry = call_api(system_prompt, user_message, cycle)
    prompt_hash = "sha256:" + hashlib.sha256(
        (system_prompt + user_message).encode()
    ).hexdigest()[:16]
    return output, system_prompt, user_message, raw_response, prompt_hash, usage_inquiry
