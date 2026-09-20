"""First-run setup, served as a page instead of asked for as a file edit.

A source checkout is configured by copying `config.example.yaml` and editing
it. That is a reasonable thing to ask of someone who has just run `git clone`
and `pip install`; it is not a reasonable thing to ask of someone who
double-clicked a downloaded binary, and hand-edited YAML is the single most
common way a first run goes wrong (see `_yaml_error_message` in config.py,
which exists entirely because of indentation mistakes).

So when nothing has said which FPL team this install is for, every page
redirects here, the answer is typed into a form, and this module writes the
config file itself.

Nothing the owner of the source repository configured is ever baked into a
build: the generated config starts from the shipped example, and the Gemini
key is whatever the person in front of the browser typed.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request, url_for,
)

from .config import BUNDLE_ROOT, STATE_ROOT, load_config

log = logging.getLogger(__name__)

setup_bp = Blueprint("setup", __name__)

# A desktop install has no cron, so unlike the server default the generated
# config has to let the app refresh itself. 6am local, the same hour the
# README's cron example uses.
DESKTOP_REFRESH_HOUR = 6

CONFIG_HEADER = """\
# Written by FPL Helper's first-run setup. Everything here can be edited by
# hand afterwards; config.example.yaml in the repository documents every key.
"""

# Progress of the first refresh, for the page that watches it. One install
# doing one first run, so a module-level dict is the whole state machine.
_first_run: dict[str, Any] = {"state": "idle", "error": None, "teams": []}
_first_run_lock = threading.Lock()


def _set_first_run(state: str, error: str | None = None, teams: list[str] | None = None) -> None:
    with _first_run_lock:
        _first_run["state"] = state
        _first_run["error"] = error
        if teams is not None:
            _first_run["teams"] = teams


def first_run_state() -> dict[str, Any]:
    with _first_run_lock:
        return dict(_first_run)


def parse_team_ids(raw: str) -> list[int]:
    """Entry ids out of whatever was typed.

    Accepts a bare id, a comma- or space-separated list, and a pasted
    /entry/<id>/... URL, because that URL is exactly what the instructions tell
    people to go and look at.
    """
    found = re.findall(r"entry/(\d+)", raw)
    if not found:
        found = re.findall(r"\d+", raw)
    out: list[int] = []
    for token in found:
        value = int(token)
        if value > 0 and value not in out:
            out.append(value)
    return out


def validate_team_ids(team_ids: list[int]) -> tuple[dict[int, str], list[str]]:
    """Ask the FPL API which of these entries actually exist.

    Names come back as a side effect, which is worth having: the progress page
    shows them, so a typo that happens to be a real entry id is still caught by
    a human reading a stranger's team name back.
    """
    from .services.fpl_api import FPLClient, FPLApiError

    names: dict[int, str] = {}
    errors: list[str] = []
    client = FPLClient()
    for team_id in team_ids:
        try:
            entry = client.entry(team_id)
        except FPLApiError as exc:
            errors.append(
                f"Could not reach the FPL API to check team {team_id} ({exc}). "
                f"Check your internet connection and try again."
            )
            continue
        if not entry:
            errors.append(
                f"FPL has no team with the id {team_id}. Check the number in the "
                f"URL at fantasy.premierleague.com → Points → the /entry/<id>/ part."
            )
            continue
        manager = " ".join(
            str(entry.get(k) or "").strip()
            for k in ("player_first_name", "player_last_name")
        ).strip()
        names[team_id] = f"{entry.get('name') or 'Unnamed team'}" + (
            f" — {manager}" if manager else ""
        )
    return names, errors


def write_config(team_ids: list[int], *, state_root: Path | None = None) -> Path:
    """Generate config.yaml from the shipped example plus the typed team ids.

    Built from the example rather than from a literal here so the defaults stay
    in one place: add a creator to config.example.yaml and new installs pick it
    up without this function knowing anything about it.
    """
    state_root = Path(state_root or STATE_ROOT)
    example = BUNDLE_ROOT / "config.example.yaml"
    data: dict[str, Any] = {}
    if example.exists():
        loaded = yaml.safe_load(example.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded

    data["team_ids"] = list(team_ids)
    # The example ships labels for its placeholder ids; they mean nothing here.
    data.pop("team_labels", None)
    data["refresh_hour"] = DESKTOP_REFRESH_HOUR

    target = state_root / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    target.write_text(CONFIG_HEADER + body, encoding="utf-8")
    log.info("wrote %s for team ids %s", target, team_ids)
    return target


def write_gemini_key(key: str, *, state_root: Path | None = None) -> Path:
    """Store the key in its own file, never in config.yaml.

    Same split the server install uses: the config file is shareable and the
    key file is not, so they are never the same file.
    """
    state_root = Path(state_root or STATE_ROOT)
    target = state_root / "secrets" / "gemini_key.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        # Windows has no mode bits worth setting; the file is under the user's
        # own profile directory either way.
        pass
    log.info("wrote a Gemini key to %s", target)
    return target


def _start_first_refresh(teams: list[str] | None = None) -> None:
    """Run the first refresh in the background so the page can show progress."""
    cfg = current_app.config["FPL_CONFIG"]
    _set_first_run("running", teams=teams or [])

    def run() -> None:
        from .refresh import run_refresh

        try:
            results = run_refresh(cfg)
        except Exception as exc:  # noqa: BLE001 - reported on the page
            log.exception("first refresh failed")
            _set_first_run("failed", str(exc))
            return
        if results.get("skipped"):
            _set_first_run("failed", str(results.get("reason") or "refresh skipped"))
        else:
            _set_first_run("done")

    threading.Thread(target=run, name="first-refresh", daemon=True).start()


@setup_bp.route("/setup", methods=["GET", "POST"])
def setup():
    # Once configured this page has nothing to offer and everything to lose:
    # submitting it again would rewrite config.yaml from the example and drop
    # anything edited by hand since. Editing the file is the way to change
    # settings later.
    cfg = current_app.config.get("FPL_CONFIG")
    if cfg is not None and not cfg.needs_setup:
        return redirect(url_for("main.squads"))

    if request.method == "GET":
        return render_template("setup.html", errors=[], team_raw="")

    team_raw = (request.form.get("team_ids") or "").strip()
    key_raw = (request.form.get("gemini_key") or "").strip()

    errors: list[str] = []
    team_ids = parse_team_ids(team_raw)
    if not team_ids:
        errors.append("Enter your FPL team id — the number in the /entry/<id>/ URL.")

    # A key with whitespace in it becomes an invalid HTTP header much later,
    # in a place that reports it as a transport error. Catch it here instead.
    if key_raw and any(ch.isspace() for ch in key_raw):
        errors.append("That Gemini key contains a space, so it is not a valid key.")

    names: dict[int, str] = {}
    if team_ids and not errors:
        names, lookup_errors = validate_team_ids(team_ids)
        errors.extend(lookup_errors)

    if errors:
        return (
            render_template("setup.html", errors=errors, team_raw=team_raw),
            400,
        )

    write_config(team_ids)
    if key_raw:
        write_gemini_key(key_raw)

    # Reload so require_setup() stops redirecting, and so the refresh uses the
    # new settings rather than the example's placeholders.
    cfg = load_config()
    current_app.config["FPL_CONFIG"] = cfg
    from .db import init_db

    init_db(cfg.db_path)

    # create_app decided against a scheduler because the example config it
    # booted from has `refresh_hour: null`. The config just written asks for
    # one, and a desktop install has no cron to fall back on, so start it now
    # rather than leaving the app un-refreshed until its next restart.
    if cfg.refresh_hour is not None and "scheduler" not in current_app.extensions:
        from . import _start_scheduler

        _start_scheduler(current_app._get_current_object(), cfg)

    _start_first_refresh([names[t] for t in team_ids if t in names])
    return redirect(url_for("setup.progress"))


@setup_bp.route("/setup/progress")
def progress():
    return render_template("setup_progress.html")


@setup_bp.route("/setup/status")
def setup_status():
    """What the progress page polls: the first run's own state plus the stage."""
    from .db import REFRESH_LOCK, connect
    from .views import active_job

    state = first_run_state()
    stage = REFRESH_LOCK.stage
    cfg = current_app.config["FPL_CONFIG"]
    try:
        conn = connect(cfg.db_path)
        try:
            job = active_job(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the page must keep polling regardless
        job = None
    return jsonify(
        {
            "state": state["state"],
            "error": state["error"],
            "teams": state.get("teams") or [],
            "stage": (job or {}).get("stage") or stage or None,
        }
    )


def register(app) -> None:
    """Mount the setup pages and the guard that forces you through them."""
    app.register_blueprint(setup_bp)

    @app.before_request
    def require_setup():
        cfg = app.config.get("FPL_CONFIG")
        if cfg is None or not cfg.needs_setup:
            return None
        endpoint = request.endpoint or ""
        # The setup pages themselves, the CSS they use, and the images route
        # all have to stay reachable or the redirect loops.
        if endpoint.startswith("setup.") or endpoint in {"static", "runtime_image"}:
            return None
        return redirect(url_for("setup.setup"))
