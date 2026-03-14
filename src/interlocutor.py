"""
Phase B/C: Interlocutor Console — CLI for creator to inject challenges and add observations.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import DATA_DIR
from . import resistance

INJECTIONS_DIR = DATA_DIR / "injections"
HUMAN_CHALLENGES_PATH = INJECTIONS_DIR / "human_challenges.json"
CREATOR_OBSERVATIONS_PATH = DATA_DIR / "creator_observations.json"


def _load_json_list(path: Path, default: list) -> list:
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else default
    except (json.JSONDecodeError, OSError):
        return default


def _save_json_list(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meaning Seeker Interlocutor — inject a challenge or add a creator observation."
    )
    parser.add_argument(
        "challenge",
        nargs="*",
        help="Challenge text. If not provided, read from stdin.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List current tensions and recent commitments (from data/), then exit.",
    )
    parser.add_argument(
        "--show-queue",
        action="store_true",
        help="Show current human challenges queue length and exit.",
    )
    # Phase C: creator observations for the "What I'm Not Thinking About" section
    parser.add_argument(
        "--observe",
        action="store_true",
        help=(
            "Add a creator observation to data/creator_observations.json "
            "(shown on the Insights page under 'What I\\'m Not Thinking About')."
        ),
    )
    parser.add_argument(
        "--show-observations",
        action="store_true",
        help="Show current creator observations and exit.",
    )
    parser.add_argument(
        "--rejected",
        action="store_true",
        help="List rejected challenges (excludes already-promoted ones) and exit.",
    )
    parser.add_argument(
        "--promote",
        metavar="ID",
        help="Promote a rejected challenge by ID into the pending queue.",
    )
    args = parser.parse_args()

    if args.list:
        tensions_path = DATA_DIR / "tensions.json"
        commitments_path = DATA_DIR / "commitments.json"
        if tensions_path.exists():
            data = json.loads(tensions_path.read_text(encoding="utf-8"))
            active = [t for t in data if isinstance(t, dict) and t.get("status") == "active"]
            print("Current tensions (%d active):" % len(active))
            for t in active[:10]:
                print("  -", (t.get("description") or "")[:80])
        if commitments_path.exists():
            data = json.loads(commitments_path.read_text(encoding="utf-8"))
            active = [c for c in data if isinstance(c, dict) and c.get("status") == "active"]
            print("Current commitments (%d active):" % len(active))
            for c in active[:5]:
                print("  -", (c.get("statement") or "")[:80])
        return

    if args.show_queue:
        queue = _load_json_list(HUMAN_CHALLENGES_PATH, [])
        print("Human challenges in queue:", len(queue))
        return

    if args.show_observations:
        obs = _load_json_list(CREATOR_OBSERVATIONS_PATH, [])
        print("Creator observations (%d):" % len(obs))
        for o in obs:
            print("  [%s] %s" % (o.get("date", "?"), (o.get("text") or "")[:100]))
        return

    if args.rejected:
        rejected = resistance.load_rejected_challenges()
        reviewable = [r for r in rejected if r.get("status") != "promoted"]
        print("Rejected challenges (%d, %d already promoted):" % (len(reviewable), len(rejected) - len(reviewable)))
        for r in reviewable:
            score = r.get("score")
            score_str = str(score) if score is not None else "–"
            reason = r.get("rejection_reason", "?")
            text_preview = (r.get("raw_text") or "")[:100]
            print("  [%s] score=%s reason=%s" % (r.get("id", "?"), score_str, reason))
            print("        %s" % text_preview)
        return

    if args.promote:
        result = resistance.promote_rejected_challenge(args.promote)
        if result:
            print("Promoted challenge %s into pending queue." % args.promote)
            print("  Text: %s" % (result.get("text") or "")[:120])
        else:
            print("Could not promote %s. Check the ID exists and hasn't been promoted already." % args.promote, file=sys.stderr)
            sys.exit(1)
        return

    text = " ".join(args.challenge).strip() if args.challenge else sys.stdin.read().strip()
    if not text:
        print(
            "No text provided. Use: python -m src.interlocutor 'Your text here'",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.observe:
        # Phase C: append to creator_observations.json
        obs = _load_json_list(CREATOR_OBSERVATIONS_PATH, [])
        obs.append({
            "date": date.today().isoformat(),
            "text": text,
        })
        _save_json_list(CREATOR_OBSERVATIONS_PATH, obs)
        print("Observation added to creator_observations.json.")
        print("It will appear on the Insights page under 'What I\\'m Not Thinking About'.")
    else:
        resistance.add_human_challenge(text, source="creator")
        print("Challenge added to queue. It will be selected by weighted draw in a future cycle.")


if __name__ == "__main__":
    main()
