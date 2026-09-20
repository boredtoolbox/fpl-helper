"""Flask application factory.

Serves pages only. The in-process refresh scheduler is opt-in via
`refresh_hour`; with it unset (the default) cron owns both data jobs and the
web process never starts one on its own.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Flask, send_from_directory

from .config import BUNDLE_ROOT, Config, IMAGE_ROOT, STATE_ROOT, load_config
from .db import init_db
from .logging_setup import setup_logging

log = logging.getLogger(__name__)

__version__ = "1.0.0"


def _start_scheduler(app: Flask, cfg: Config) -> None:
    """Daily refresh via APScheduler, in-process.

    Skipped entirely when `refresh_hour` is null, which is how you hand the
    schedule to cron: the web app then only ever serves pages and the manual
    Refresh button, and never starts a refresh on its own.
    """
    if cfg.refresh_hour is None:
        log.info("refresh_hour is not set — no in-process refresh (cron owns it).")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler not installed — no automatic refresh will run.")
        return

    from .refresh import run_refresh

    scheduler = BackgroundScheduler(timezone=_local_timezone())

    def job() -> None:
        log.info("scheduled refresh starting")
        try:
            run_refresh(cfg)
        except Exception:  # noqa: BLE001 - the scheduler thread must survive
            log.exception("scheduled refresh failed")

    scheduler.add_job(
        job,
        CronTrigger(hour=cfg.refresh_hour, minute=0),
        id="daily_refresh",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    app.extensions["scheduler"] = scheduler
    log.info("scheduler started: daily refresh at %02d:00 local time", cfg.refresh_hour)


def _local_timezone():
    """A real IANA zone for APScheduler.

    `datetime.now().astimezone().tzinfo` stringifies to a UTC offset like "+08",
    which zoneinfo cannot resolve — so ask tzlocal, and fall back to UTC rather
    than letting the scheduler take the whole app down at startup.
    """
    try:
        import tzlocal

        return tzlocal.get_localzone()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not determine the local timezone (%s); scheduling in UTC", exc)
        from datetime import timezone

        return timezone.utc


def create_app(config_path: str | None = None, *, start_scheduler: bool = True) -> Flask:
    setup_logging()
    cfg = load_config(config_path)

    app = Flask(
        __name__,
        template_folder=str(BUNDLE_ROOT / "templates"),
        static_folder=str(BUNDLE_ROOT / "static"),
    )
    app.config["FPL_CONFIG"] = cfg
    # Session only stores which of my own teams is selected — no secrets in it.
    app.config["SECRET_KEY"] = os.environ.get("FPL_SECRET_KEY") or _local_secret()
    app.config["JSON_SORT_KEYS"] = False

    init_db(cfg.db_path)

    # Kits and crests are downloaded by the refresh job, so they sit under the
    # writable state directory rather than in the bundled static folder. This
    # rule is more specific than Flask's own /static/<path:filename>, so it
    # wins for these two prefixes and the templates' url_for('static', ...)
    # calls need no changing. In a source checkout both point at the same
    # directory, which is why this path is exercised in normal development too.
    @app.route("/static/<any(kits, crests):kind>/<path:filename>")
    def runtime_image(kind: str, filename: str):
        return send_from_directory(IMAGE_ROOT / kind, filename, max_age=86400)

    from .routes import bp

    app.register_blueprint(bp)

    # Mounted last so its before_request guard sees every other endpoint.
    from .setup import register as register_setup

    register_setup(app)

    @app.template_filter("fmt_rank")
    def fmt_rank(value):
        return f"{value:,}" if isinstance(value, (int, float)) and value else "—"

    @app.template_filter("fmt_time")
    def fmt_time(value):
        if not value:
            return "never"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return parsed.astimezone().strftime("%a %d %b %Y, %H:%M")

    @app.template_filter("fmt_kick")
    def fmt_kick(value):
        """A kickoff time, short. No year — a fixture list is always this season."""
        if not value:
            return "TBC"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return parsed.astimezone().strftime("%a %d %b · %H:%M")

    @app.template_filter("fmt_day")
    def fmt_day(value):
        """A fixture day heading: "Saturday 13 September", in local time."""
        if not value:
            return "Date to be confirmed"
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        # %-d drops the leading zero; it is a GNU/BSD extension, and both the Pi
        # and macOS have it. %d is the portable fallback if that ever changes.
        return parsed.strftime("%A %-d %B")

    @app.template_filter("fmt_clock")
    def fmt_clock(value):
        """Just the kickoff time — the date is already the group heading."""
        if not value:
            return "TBC"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return parsed.astimezone().strftime("%H:%M")

    @app.template_filter("until")
    def until(value):
        """How far off something is, in plain words — for the next deadline.

        Coarse on purpose: to the minute inside an hour, to the hour inside a
        day, and days beyond that. A page that is served once and left open
        would only be lying more precisely if it counted seconds.
        """
        if not value:
            return ""
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            return "deadline passed"
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"in {days}d {hours}h"
        if hours:
            return f"in {hours}h {minutes}m"
        return f"in {minutes}m"

    @app.template_filter("signed")
    def signed(value, places=1):
        if value is None:
            return "—"
        return f"{value:+.{places}f}"

    # Flask's reloader runs two processes; only the child should own the
    # scheduler, so we'd double-schedule without this guard. Under a real WSGI
    # server (waitress, gunicorn) WERKZEUG_RUN_MAIN is unset and we start it.
    if start_scheduler and not (app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"):
        _start_scheduler(app, cfg)

    return app


def _local_secret() -> str:
    """Stable per-install secret so sessions survive restarts."""
    path = STATE_ROOT / "data" / ".flask_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = os.urandom(32).hex()
    path.write_text(secret, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret
