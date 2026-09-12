#!/usr/bin/env python3
"""Standalone crowd-intel job — the only thing in this project that calls Gemini.

Kept out of the data refresh on purpose: `python -m app.refresh` and the Refresh
button in the UI are free to run as often as you like, while this costs one
Gemini call every time. Cron owns the schedule:

    0 6 * * *  cd /home/<user>/fpl-helper && /usr/bin/python3 -m app.crowd_refresh --quiet

Run manually with:  python -m app.crowd_refresh
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .logging_setup import setup_logging
from .refresh import run_crowd_intel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gather crowd intel (YouTube + news -> Gemini) and rebuild on it.",
    )
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument(
        "--no-rebuild", action="store_true",
        help="store the adjustments but leave projections for the next refresh",
    )
    parser.add_argument("--quiet", action="store_true", help="warnings and errors only")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(level=logging.WARNING if args.quiet else logging.INFO)
    results = run_crowd_intel(cfg, rebuild=not args.no_rebuild)

    if results.get("skipped"):
        print(f"Skipped: {results.get('reason')}")
        return 1
    failed = results.get("failed_stages") or []
    print(f"Crowd intel finished in {results.get('seconds')}s")
    for name, value in results.items():
        if isinstance(value, dict) and "ok" in value:
            mark = "ok  " if value["ok"] else "FAIL"
            detail = value.get("error") or value.get("result")
            print(f"  [{mark}] {name:<18} {str(detail)[:110]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
