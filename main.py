#!/usr/bin/env python3
import argparse
import logging
import sys

from src import orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meaning Seeker -- autonomous philosophical AI"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit (useful for testing)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        metavar="N",
        default=None,
        help="Run N cycles and exit (handy for testing). Overrides continuous mode.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if args.once:
        orchestrator.run_cycle()
    elif args.cycles is not None:
        orchestrator.run(max_cycles=args.cycles)
    else:
        orchestrator.run()


if __name__ == "__main__":
    main()
