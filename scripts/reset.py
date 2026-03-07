#!/usr/bin/env python3
"""
Reset Meaning Seeker to cycle 0. Deletes all state, archive, and embeddings.
Use this to start fresh after prompt changes or to re-run from the beginning.
"""
import argparse
import shutil
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_DIR


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset Meaning Seeker to cycle 0. Deletes all state, archive, and embeddings."
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting",
    )
    args = parser.parse_args()

    data_dir = Path(DATA_DIR).resolve()
    if not data_dir.exists():
        print(f"Data directory does not exist: {data_dir}")
        print("Nothing to reset.")
        sys.exit(0)

    if args.dry_run:
        print(f"Would delete: {data_dir}")
        if data_dir.exists():
            for p in sorted(data_dir.rglob("*")):
                rel = p.relative_to(data_dir)
                print(f"  - {rel}")
        sys.exit(0)

    if not args.force:
        print(f"This will permanently delete: {data_dir}")
        print("All cycles, journal entries, manuscript, tensions, and commitments will be lost.")
        try:
            reply = input("Type 'yes' to confirm: ").strip().lower()
        except EOFError:
            reply = ""
        if reply != "yes":
            print("Aborted.")
            sys.exit(1)

    shutil.rmtree(data_dir)
    print(f"Deleted {data_dir}")

    # Reinitialize
    from src import memory

    memory.init_dirs()
    print("Reinitialized. Next cycle will be 1.")


if __name__ == "__main__":
    main()
