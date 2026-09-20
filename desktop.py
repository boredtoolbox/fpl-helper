#!/usr/bin/env python3
"""Entry point for the packaged desktop build.

`run.py` is the server entry point: it binds every interface, prints a LAN
address and expects cron or systemd to own the schedule. That is right for a
Pi in a cupboard and wrong for a laptop, so the packaged build comes in here
instead.

The differences are all about where the app is running, not what it does:

  * binds 127.0.0.1, because there is no authentication and a laptop joins
    networks a Pi never will;
  * opens a browser, because there is no terminal to read a URL out of;
  * runs waitress rather than Flask's development server;
  * starts the in-process scheduler, because there is no cron here.

Writable state does not live next to this executable — a one-file build
unpacks into a temporary directory that is deleted on exit. See `_state_root`
in app/config.py for where it goes instead.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

DEFAULT_PORT = 8000
# If the preferred port is busy — a second copy, or something else entirely —
# walk up rather than dying with a stack trace the user cannot act on.
PORT_ATTEMPTS = 20


def _free_port(host: str, preferred: int) -> int | None:
    import socket

    for port in range(preferred, preferred + PORT_ATTEMPTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    return None


def _open_browser(url: str) -> None:
    """Open the page once the server is actually accepting connections.

    Opening it immediately races the server and lands the user on a browser
    error page, which reads as "the app is broken" rather than "too early".
    """
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
        except urllib.error.HTTPError:
            break  # Responding, even if with a redirect or an error status.
        except OSError:
            time.sleep(0.25)
            continue
        else:
            break
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a headless box has no browser to open
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FPL Helper on this machine.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: this machine only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    parser.add_argument("--config", help="path to a config.yaml")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--state-dir",
        help="where to keep config, database and logs (default: your user data directory)",
    )
    args = parser.parse_args(argv)

    if args.state_dir:
        # Read by app.config at import time, so it has to be set before the
        # first import of anything under app/.
        import os

        os.environ["FPL_STATE_DIR"] = args.state_dir

    from app import create_app
    from app.config import STATE_ROOT
    from app.config import ConfigError

    port = _free_port(args.host, args.port)
    if port is None:
        print(f"\n  Could not find a free port from {args.port} upwards. Is it already running?\n")
        return 1

    try:
        app = create_app(args.config, start_scheduler=True)
    except ConfigError as exc:
        print(f"\n{exc}\n")
        return 1

    url = f"http://{args.host}:{port}/"
    cfg = app.config["FPL_CONFIG"]

    # flush= on every line: this console window is the only feedback a user
    # who double-clicked the file gets, and block-buffered stdout would leave
    # it empty until something else happened to flush it.
    print(flush=True)
    print(f"  FPL Helper  ->  {url}", flush=True)
    if cfg.needs_setup:
        print("  First run — the browser will ask for your FPL team id.", flush=True)
    else:
        print(f"  teams: {', '.join(str(t) for t in cfg.team_ids)}", flush=True)
    print(f"  your data: {STATE_ROOT}", flush=True)
    print("  Close this window to stop.", flush=True)
    print(flush=True)

    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    from waitress import serve

    logging.getLogger("waitress").setLevel(logging.ERROR)
    try:
        serve(app, host=args.host, port=port, threads=8, _quiet=True)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
