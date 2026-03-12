from pathlib import Path
from typing import Optional

from .models import TensionNew, TensionResolved


def render_journal_entry(
    cycle: int,
    timestamp: str,
    mode: str,
    thinking: str,
    tensions_new: list[TensionNew],
    tensions_resolved: list[TensionResolved],
    transition_entry: str | None,
    usage: dict | None = None,
    title: str | None = None,
    summary: str | None = None,
    image_path: Optional[Path] = None,
    woven_challenge: Optional[dict] = None,
    review_challenges: Optional[list[dict]] = None,
) -> str:
    heading = title.strip() if title and title.strip() else f"Cycle {cycle}"
    header_parts = [f"**Cycle {cycle}**", f"**Date:** {timestamp[:10]}", f"**Mode:** {mode}"]
    if woven_challenge and woven_challenge.get("submission_id"):
        header_parts.append(f"**Woven:** {woven_challenge['submission_id']}")
    lines = [
        f"# {heading}",
        " · ".join(header_parts),
    ]
    if usage:
        inv = usage.get("inquiry") or {}
        mon = usage.get("monitoring") or {}
        tot = usage.get("total_tokens", inv.get("total_tokens", 0) + mon.get("total_tokens", 0))
        lines.append(f"**Tokens:** inquiry {inv.get('total_tokens', 0)} · monitoring {mon.get('total_tokens', 0)} · **total** {tot}")
    if summary and summary.strip():
        lines += ["", f"*{summary.strip()}*"]
    lines += ["", "---", ""]
    if review_challenges:
        lines.append("### Challenges addressed this week\n")
        for c in review_challenges:
            text = (c.get("text") or "").strip()
            if text:
                lines.append(f'- "{text}"')
        lines += ["", ""]
    lines.append(thinking.strip())

    # Phase C: embed generated image if available
    if image_path:
        rel_path = f"/images/{image_path.name}"
        lines += ["", f"![Cycle {cycle} visualization]({rel_path})", ""]

    if tensions_new:
        lines += ["", "---", "", "### New tensions carried forward", ""]
        for t in tensions_new:
            lines.append(f"- {t.description}")

    if tensions_resolved:
        lines += ["", "---", "", "### Tensions resolved this cycle", ""]
        for t in tensions_resolved:
            lines.append(f"- **{t.tension_id}**: {t.resolution_note}")

    if transition_entry:
        lines += ["", "---", "", "### Transition", "", transition_entry.strip()]

    return "\n".join(lines) + "\n"
