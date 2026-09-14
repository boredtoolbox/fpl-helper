#!/usr/bin/env python3
"""Start the FPL Helper web app.

    python3 run.py                 # http://0.0.0.0:8000
    python3 run.py --port 8080
    python3 run.py --no-scheduler   # force the in-process refresh off

By default config.yaml sets `refresh_hour: null`, so the app schedules nothing
and cron runs `app.refresh` / `app.crowd_refresh` instead.
"""
from __future__ import annotations

import argparse
import socket

from app import create_app
from app.config import ConfigError


def local_address() -> str:
    """Best-guess LAN address, so the printed URL is one you can actually open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FPL Helper web app.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument(
        "--no-scheduler", action="store_true",
        help="skip the daily background refresh",
    )
    args = parser.parse_args()

    try:
        app = create_app(args.config, start_scheduler=not args.no_scheduler)
    except ConfigError as exc:
        # A broken config.yaml is a typo, not a bug — say what to fix, not where
        # in the YAML parser it surfaced.
        print(f"\n{exc}\n")
        return 1
    cfg = app.config["FPL_CONFIG"]

    print()
    print(f"  FPL Helper  ->  http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"               ->  http://{local_address()}:{args.port}   (other devices)")
    print(f"  teams: {', '.join(str(t) for t in cfg.team_ids) or 'none configured'}")
    if args.no_scheduler:
        print("  daily refresh: disabled (--no-scheduler)")
    elif cfg.refresh_hour is None:
        print("  daily refresh: cron only (refresh_hour is not set)")
    else:
        print(f"  daily refresh: {cfg.refresh_hour:02d}:00 local")
    print("  Ctrl-C to stop.")
    print()

    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
