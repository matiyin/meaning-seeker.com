#!/usr/bin/env python3
"""
Reindex archive cycles into Chroma using Ollama (nomic-embed-text).

Use this when Ollama was not running during past cycles: it reads each
data/archive/cycles/NNNNNN.json, computes an embedding via Ollama, and
stores it in the Chroma "archive" collection. Future cycles will then
retrieve similar past thinking as archive_snippets.

Requires: Ollama running with nomic-embed-text pulled.

Usage:
  .venv/bin/python3 scripts/reindex_embeddings.py           # index all cycles
  .venv/bin/python3 scripts/reindex_embeddings.py --force   # re-index (replace existing)
  .venv/bin/python3 scripts/reindex_embeddings.py --dry-run  # list cycles only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Project root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DATA_DIR
from src import retrieval


def _tension_like(description: str, tension_id: str = "") -> SimpleNamespace:
    """Minimal object compatible with store_cycle_embedding (description, status, tension_id)."""
    return SimpleNamespace(
        description=(description or "")[:200],
        status="active",
        tension_id=tension_id,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Reindex archive cycles into Chroma via Ollama.")
    ap.add_argument("--force", action="store_true", help="Delete existing doc before add (reindex).")
    ap.add_argument("--dry-run", action="store_true", help="Only list cycles that would be indexed.")
    args = ap.parse_args()

    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        print("No archive/cycles directory at", cycles_dir)
        sys.exit(1)

    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem))
    if not files:
        print("No cycle JSON files in", cycles_dir)
        sys.exit(0)

    print(f"Found {len(files)} cycle(s) in {cycles_dir}")
    if args.dry_run:
        for p in files:
            print(" ", p.name)
        return

    ok = 0
    skip = 0
    fail = 0
    for path in files:
        cycle = int(path.stem)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            print(f"  Cycle {cycle}: skip (read error: {e})")
            fail += 1
            continue

        thinking = (raw.get("thinking") or "").strip()
        if not thinking:
            print(f"  Cycle {cycle}: skip (no thinking)")
            skip += 1
            continue

        timestamp = raw.get("timestamp") or ""
        inj = raw.get("injection") or {}
        challenge = (inj.get("text") or "").strip() if isinstance(inj, dict) else ""

        # Build minimal tension-like list from tensions_new for this cycle
        tensions_new = raw.get("tensions_new") or []
        if not isinstance(tensions_new, list):
            tensions_new = []
        tensions = [
            _tension_like(
                (t.get("description") or "") if isinstance(t, dict) else "",
                f"backfill-{cycle}-{i}",
            )
            for i, t in enumerate(tensions_new[:20])
        ]

        if args.force:
            retrieval.delete_cycle_embedding(cycle)

        if retrieval.store_cycle_embedding(cycle, thinking, tensions, challenge, timestamp or None):
            ok += 1
            print(f"  Cycle {cycle}: indexed")
        else:
            fail += 1
            print(f"  Cycle {cycle}: failed (embedding or Chroma add)")

    print(f"Done: {ok} indexed, {skip} skipped (no thinking), {fail} errors.")


if __name__ == "__main__":
    main()
