"""Flask routes. Every handler reads from SQLite; none of them touch the network."""
from __future__ import annotations

import logging
import threading

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request,
    session, url_for,
)

from .db import REFRESH_LOCK, connect
from .views import (
    active_job, data_freshness, data_version, entry_stats,
    current_matchweek, gameweek_info, match_day, matchweek_index, my_entries,
    opponent_history, player_table, squad_for, squad_player_ids,
)

log = logging.getLogger(__name__)
bp = Blueprint("main", __name__)


def _conn():
    return connect(current_app.config["FPL_CONFIG"].db_path)


def _cfg():
    return current_app.config["FPL_CONFIG"]


def _selected_team(cfg) -> int | None:
    """Team id from ?team=, else the session, else the first configured team.

    Every page carries its own selector now, so a choice made on one page is
    remembered in the session and the next page opens on the same team.
    """
    requested = request.args.get("team", type=int)
    if requested and requested in cfg.team_ids:
        session["team_id"] = requested
        return requested
    stored = session.get("team_id")
    if stored and stored in cfg.team_ids:
        return stored
    return cfg.team_ids[0] if cfg.team_ids else None


@bp.app_context_processor
def inject_globals():
    """Freshness and the selected team are needed by every page."""
    cfg = _cfg()
    if cfg.needs_setup:
        # The setup pages do not use any of this, and before a team is
        # configured there is nothing in the database to report anyway.
        return {}
    conn = _conn()
    try:
        # active_job sees cron's process too; REFRESH_LOCK only sees this one.
        job = active_job(conn)
        return {
            "freshness": data_freshness(conn),
            "gameweek": gameweek_info(conn),
            "all_entries": my_entries(conn, cfg),
            "selected_team_id": _selected_team(cfg),
            "data_version": data_version(conn),
            "refresh_running": bool(job) or REFRESH_LOCK.is_running,
            "refresh_stage": (job or {}).get("stage") or REFRESH_LOCK.stage,
        }
    finally:
        conn.close()


@bp.route("/matches")
def matches():
    """Open on the matchweek being played, or the one next up.

    A redirect rather than a render, so every matchweek has exactly one URL and
    the one you are looking at is always in the address bar — the same shape as
    the Premier League's own fixture pages.
    """
    conn = _conn()
    try:
        event = current_matchweek(matchweek_index(conn))
    finally:
        conn.close()
    if event is None:
        return render_template("matches.html", week=None, index=[], no_fixtures=True)
    return redirect(url_for("main.matchweek", event=event))


@bp.route("/matches/<int:event>")
def matchweek(event: int):
    """One matchweek: its fixtures, grouped by the day they are played on."""
    conn = _conn()
    try:
        index = matchweek_index(conn)
        events = [w["event"] for w in index]
        if event not in events:
            return render_template(
                "matches.html", week=None, index=index, unknown_event=event
            ), 404

        position = events.index(event)
        return render_template(
            "matches.html",
            week=match_day(conn, event),
            meta=index[position],
            index=index,
            prev_event=events[position - 1] if position > 0 else None,
            next_event=events[position + 1] if position < len(events) - 1 else None,
        )
    finally:
        conn.close()


@bp.route("/")
def squads():
    cfg = _cfg()
    conn = _conn()
    try:
        team_id = _selected_team(cfg)
        if team_id is None:
            return render_template("squads.html", squad=None, team_id=None, no_config=True)
        squad = squad_for(conn, team_id)
        entry = next(
            (e for e in my_entries(conn, cfg) if e["team_id"] == team_id), None
        )
        return render_template(
            "squads.html", squad=squad, team_id=team_id, entry=entry, no_config=False
        )
    finally:
        conn.close()


@bp.route("/stats")
def stats():
    """One team at a time, chosen with the page's own selector."""
    cfg = _cfg()
    conn = _conn()
    try:
        team_id = _selected_team(cfg)
        if team_id is None:
            return render_template("stats.html", team=None, no_config=True)
        team = {
            "team_id": team_id,
            "label": cfg.label_for(team_id) or f"Team {team_id}",
            **entry_stats(conn, team_id),
        }
        if team.get("entry"):
            team["label"] = cfg.label_for(team_id) or team["entry"]["name"]
        return render_template("stats.html", team=team, no_config=False)
    finally:
        conn.close()


@bp.route("/players")
def players():
    """The explorer. The selected team only marks up who you already own."""
    cfg = _cfg()
    conn = _conn()
    try:
        team_id = _selected_team(cfg)
        owned = squad_player_ids(conn, team_id) if team_id else set()
        rows = player_table(conn)
        for row in rows:
            row["owned"] = row["id"] in owned
        teams = sorted({r["team"] for r in rows if r["team"]})
        return render_template(
            "players.html",
            players=rows,
            teams=teams,
            owned_count=sum(1 for r in rows if r["owned"]),
        )
    finally:
        conn.close()


@bp.route("/builder")
def builder():
    """The strongest handful of options per position, and what the creators say.

    Solved on request. Nothing is stored: the inputs are all in SQLite already
    and the whole page is a few reads and some arithmetic, so it is cheaper to
    redo than to cache and invalidate.
    """
    conn = _conn()
    try:
        from .services.shortlist import (
            FDR_MULTIPLIER, HORIZON_DEFAULT, HORIZON_MAX, MIN_MINUTES_DEFAULT,
            POSITION_ORDER, TOP_N, W_EP, W_PERF, W_XPTS, creator_board,
            horizon_events, player_rows, shortlist,
        )

        horizon = min(
            max(request.args.get("horizon", type=int) or HORIZON_DEFAULT, 1), HORIZON_MAX
        )
        raw_minutes = request.args.get("mins", type=int)
        min_minutes = (
            MIN_MINUTES_DEFAULT if raw_minutes is None else max(0, min(raw_minutes, 4000))
        )

        events = horizon_events(conn, horizon)
        rows = player_rows(conn, events)
        lists, meta = shortlist(rows.values(), min_minutes, TOP_N)
        board = creator_board(conn, _cfg(), rows)

        # Which gameweeks the projection engine actually reaches. Its horizon is
        # configured separately and is usually shorter than this page's, and the
        # page says so rather than implying ten gameweeks of projection.
        xpts_events = sorted(
            {
                r["event"] for r in conn.execute(
                    "SELECT DISTINCT event FROM projections ORDER BY event"
                )
            }
            & set(events)
        )

        # Early in a season form, EP and points per game are three names for the
        # same handful of matches, and a weighted blend of one number is that
        # number. Counted rather than assumed, so the page only mentions it when
        # it is actually true.
        identical = sum(
            1 for p in meta["pool"]
            if abs(p["form"] - p["ep_next"]) < 0.05 and abs(p["form"] - p["ppg"]) < 0.05
        )

        gw = gameweek_info(conn)
        played = (gw["current"] or {}).get("id") or max(
            ((gw["next"] or {}).get("id") or 1) - 1, 0
        )

        return render_template(
            "builder.html",
            events=events,
            xpts_events=xpts_events,
            horizon=horizon,
            min_minutes=min_minutes,
            top_n=TOP_N,
            lists=lists,
            meta=meta,
            board=board,
            pool_size=len(rows),
            played_gws=played,
            identical_baselines=identical,
            identical_baselines_pct=(
                round(100 * identical / meta["eligible"]) if meta["eligible"] else 0
            ),
            weights={"perf": W_PERF, "ep": W_EP, "xpts": W_XPTS},
            fdr_multiplier=FDR_MULTIPLIER,
            position_panels=[
                (POSITION_ORDER[0], "GKP",
                 "one starts, one sits — the second is the cheapest legal body you can find"),
                (POSITION_ORDER[1], "DEF",
                 "clean sheets and defensive contributions, so the fixture run matters most here"),
                (POSITION_ORDER[2], "MID",
                 "the deepest position, and where a differential is worth the most"),
                (POSITION_ORDER[3], "FWD",
                 "goals only — no clean sheet, no defensive contribution to fall back on"),
            ],
        )
    finally:
        conn.close()


@bp.route("/api/player/<int:player_id>/opponents")
def player_opponents(player_id: int):
    """Per-opponent history behind an expanded row on the explorer.

    Fetched on demand rather than rendered with the table: six opponents' worth
    of match history for 700-odd players is far more HTML than a page needs, and
    almost none of it is ever opened.
    """
    conn = _conn()
    try:
        payload = opponent_history(conn, player_id)
        if payload is None:
            return jsonify({"error": "unknown player"}), 404
        return jsonify(payload)
    finally:
        conn.close()


@bp.route("/api/status")
def status():
    """What the open pages poll so they can update themselves.

    Deliberately the cheapest handler in the app — two small meta reads, no
    joins — because every open tab hits it on a timer.
    """
    conn = _conn()
    try:
        job = active_job(conn)
        return jsonify(
            {
                "version": data_version(conn),
                "running": bool(job) or REFRESH_LOCK.is_running,
                "job": (job or {}).get("job"),
                "stage": (job or {}).get("stage") or REFRESH_LOCK.stage or None,
            }
        )
    finally:
        conn.close()


@bp.route("/refresh", methods=["POST"])
def refresh_now():
    """Kick off a refresh in a background thread, guarded against overlap.

    REFRESH_LOCK only sees this process, so it cannot tell that cron is halfway
    through a run of its own — and a disabled button is advisory, not a guard: a
    stale page or a double submit still posts. The database check is the real
    one, because it is the only thing both processes can see.
    """
    conn = _conn()
    try:
        running = active_job(conn)
    finally:
        conn.close()
    if running or REFRESH_LOCK.is_running:
        where = f"{(running or {}).get('job', 'refresh')} job" if running else "refresh"
        flash(f"A {where} is already running — try again once it finishes.", "warning")
        return redirect(request.referrer or url_for("main.squads"))

    cfg = _cfg()

    def run() -> None:
        from .refresh import run_refresh

        try:
            run_refresh(cfg)
        except Exception:  # noqa: BLE001
            log.exception("manual refresh failed")

    threading.Thread(target=run, name="manual-refresh", daemon=True).start()
    flash("Refresh started — it runs in the background, reload in a minute or two.", "info")
    return redirect(request.referrer or url_for("main.squads"))
