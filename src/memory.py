import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR
from .models import (
    Commitment,
    CommitmentUpdate,
    StateFile,
    Tension,
    TensionNew,
    TensionResolved,
    TransitionEntry,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# ── Initialisation ─────────────────────────────────────────────────────────────

def init_dirs() -> None:
    dirs = [
        DATA_DIR,
        DATA_DIR / "archive" / "cycles",
        DATA_DIR / "archive" / "journal",
        DATA_DIR / "prompts",
        DATA_DIR / "injections",
        DATA_DIR / "chroma",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    state_path = DATA_DIR / "state.json"
    if not state_path.exists():
        _atomic_write_json(state_path, StateFile().model_dump())

    manuscript_path = DATA_DIR / "manuscript.md"
    if not manuscript_path.exists():
        manuscript_path.write_text("", encoding="utf-8")

    for fname in ("transitions.json", "tensions.json", "commitments.json"):
        p = DATA_DIR / fname
        if not p.exists():
            _atomic_write_json(p, [])

    move_log_path = DATA_DIR / "move_log.json"
    if not move_log_path.exists():
        _atomic_write_json(move_log_path, [])


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> StateFile:
    raw = json.loads((DATA_DIR / "state.json").read_text(encoding="utf-8"))
    return StateFile(**raw)


def save_state(state: StateFile) -> None:
    _atomic_write_json(DATA_DIR / "state.json", state.model_dump())


# ── Manuscript ─────────────────────────────────────────────────────────────────

def load_manuscript() -> str:
    return (DATA_DIR / "manuscript.md").read_text(encoding="utf-8")


def save_manuscript(text: str) -> None:
    (DATA_DIR / "manuscript.md").write_text(text, encoding="utf-8")


# ── Transition log ─────────────────────────────────────────────────────────────

def load_transitions() -> list[TransitionEntry]:
    raw = json.loads((DATA_DIR / "transitions.json").read_text(encoding="utf-8"))
    return [TransitionEntry(**r) for r in raw]


def append_transition(entry: TransitionEntry) -> None:
    entries = load_transitions()
    entries.append(entry)
    _atomic_write_json(
        DATA_DIR / "transitions.json",
        [e.model_dump() for e in entries],
    )


# ── Tensions ───────────────────────────────────────────────────────────────────

def load_tensions() -> list[Tension]:
    raw = json.loads((DATA_DIR / "tensions.json").read_text(encoding="utf-8"))
    return [Tension(**r) for r in raw]


def save_tensions(tensions: list[Tension]) -> None:
    _atomic_write_json(
        DATA_DIR / "tensions.json",
        [t.model_dump() for t in tensions],
    )


def apply_tension_changes(
    tensions: list[Tension],
    new_items: list[TensionNew],
    resolved_items: list[TensionResolved],
    state: StateFile,
    cycle: int,
) -> tuple[list[Tension], StateFile]:
    active_count = sum(1 for t in tensions if t.status == "active")

    for item in new_items:
        if active_count >= 15:
            logger.warning(
                f"Cycle {cycle}: max tensions reached, skipping new tension: {item.description[:60]}"
            )
            break
        tid = f"T-{state.next_tension_id:04d}"
        state.next_tension_id += 1
        tensions.append(
            Tension(
                tension_id=tid,
                created_cycle=cycle,
                created_at=_now_iso(),
                description=item.description,
            )
        )
        active_count += 1
        logger.debug(f"Cycle {cycle}: new tension {tid}")

    resolve_map = {r.tension_id: r for r in resolved_items}
    for t in tensions:
        if t.tension_id in resolve_map and t.status == "active":
            r = resolve_map[t.tension_id]
            t.status = "resolved"
            t.resolution = {"cycle": cycle, "note": r.resolution_note}
            logger.debug(f"Cycle {cycle}: resolved tension {t.tension_id}")

    return tensions, state


# ── Commitments ────────────────────────────────────────────────────────────────

def load_commitments() -> list[Commitment]:
    raw = json.loads((DATA_DIR / "commitments.json").read_text(encoding="utf-8"))
    return [Commitment(**r) for r in raw]


def save_commitments(commitments: list[Commitment]) -> None:
    _atomic_write_json(
        DATA_DIR / "commitments.json",
        [c.model_dump() for c in commitments],
    )


def apply_commitment_changes(
    commitments: list[Commitment],
    updates: list[CommitmentUpdate],
    state: StateFile,
    cycle: int,
) -> tuple[list[Commitment], StateFile]:
    commitment_map = {c.commitment_id: c for c in commitments}

    for update in updates:
        if update.action == "new":
            cid = f"C-{state.next_commitment_id:04d}"
            state.next_commitment_id += 1
            new_commitment = Commitment(
                commitment_id=cid,
                statement=update.statement or "",
                confidence=update.confidence if update.confidence is not None else 0.5,
                dependencies=update.dependencies or [],
                change_condition=update.change_condition or "",
                origin_cycle=cycle,
                created_at=_now_iso(),
            )
            commitments.append(new_commitment)
            commitment_map[cid] = new_commitment
            logger.debug(f"Cycle {cycle}: new commitment {cid}")

        elif update.action == "update":
            if update.commitment_id not in commitment_map:
                logger.warning(
                    f"Cycle {cycle}: update for unknown commitment {update.commitment_id}"
                )
                continue
            c = commitment_map[update.commitment_id]
            if update.statement:
                c.statement = update.statement
            if update.confidence is not None:
                c.confidence = update.confidence
            logger.debug(f"Cycle {cycle}: updated commitment {update.commitment_id}")

        elif update.action == "abandon":
            if update.commitment_id not in commitment_map:
                logger.warning(
                    f"Cycle {cycle}: abandon for unknown commitment {update.commitment_id}"
                )
                continue
            c = commitment_map[update.commitment_id]
            c.status = "abandoned"
            c.abandoned_at_cycle = cycle
            c.abandon_reason = update.reason
            logger.debug(f"Cycle {cycle}: abandoned commitment {update.commitment_id}")

    return commitments, state


# ── Archive ────────────────────────────────────────────────────────────────────

def save_cycle_record(cycle: int, record: dict) -> None:
    path = DATA_DIR / "archive" / "cycles" / f"{cycle:06d}.json"
    _atomic_write_json(path, record)


def save_journal_entry(cycle: int, content: str) -> None:
    path = DATA_DIR / "archive" / "journal" / f"{cycle:06d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_move_log() -> list[str]:
    """Phase B: Pattern Monitor move log (last N classifications)."""
    path = DATA_DIR / "move_log.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_move_log(moves: list[str]) -> None:
    """Phase B: Persist move log."""
    _atomic_write_json(DATA_DIR / "move_log.json", moves)


def load_recent_thinking(n: int) -> list[str]:
    """Phase B: Load thinking text from last n cycle records (newest first). Used for repetition check."""
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return []
    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)
    out = []
    for path in files[:n]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            thinking = (data.get("thinking") or "").strip()
            if thinking:
                out.append(thinking)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return out


def append_failure_record(record: dict) -> None:
    """Append a failure record to data/archive/failures.json for audit."""
    path = DATA_DIR / "archive" / "failures.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append(record)
    _atomic_write_json(path, existing)
