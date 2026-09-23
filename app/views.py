"""Read-only query helpers backing the pages.

Everything here reads SQLite only — no network calls happen during a request.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import ACTIVE_JOB_KEY, DATA_VERSION_KEYS, jload
from .services.engine import (
    DEFCON_THRESHOLD, POSITION_NAMES, defcon_hit_rates, seasons_with_defcon,
)

# --- Colour coding on the player explorer -----------------------------------
#
# Every banded stat is "higher is better", and each is judged against players in
# the same position rather than the whole game: a defender managing 0.10 xG/90
# is doing something useful, a forward on the same number is not.
#
# The cuts come from the live distribution rather than fixed thresholds, so they
# still mean something in August when every season total is tiny, and they keep
# meaning it in May. Top third green, middle third amber, bottom third red.
#
# Cost and ownership are deliberately not banded. Neither is good or bad on its
# own — cheap is only good for what it frees up, and a crowded template is a
# risk or a safety net depending on what you are trying to do.

# Where the cuts are measured from. Always players with real minutes behind
# them, for every stat: over half the game has never been on a pitch, and
# including them drags the middle of the distribution down to nothing — two
# points would grade as an above-average season simply for not being zero.
BAND_MINUTES_FLOOR = 60
# Rates are only *shown* for that same group. A goal in a twenty-minute cameo is
# 4.50 per 90, which is not a fact about the player.
BAND_RATE_STATS = ("xg90", "xa90", "goals90", "assists90", "mins_per_start", "defcon90")
# Season levels are graded for everyone against those cuts, so a player who has
# not featured shows red — which is the honest reading of no returns.
BAND_LEVEL_STATS = ("ep_next", "total_points", "form", "ppg")
# Stats that carry their own sample gate rather than a minutes one, as
# {stat: (count field, minimum)}. DefCon reliability off two matches is noise,
# and colouring noise is worse than leaving it plain.
BAND_SAMPLE_STATS = {"defcon_reliability": ("defcon_sample", 5)}
# The fourth state: there is a number, but not enough football behind it to
# grade. Slate rather than blank, so "we are not calling this one" is something
# the table says out loud instead of a gap you have to interpret. A cell stays
# genuinely empty only where the stat does not apply at all — DefCon and keepers.
BAND_THIN = "b-thin"


def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (pos - low)


def _band(value: float, p33: float | None, p67: float | None) -> str:
    """good / avg / bad for one value against its position's distribution.

    Two cases, and the difference between them is whether the stat separates
    this position at all.

    When both cuts land on the same number the stat mostly does not happen here
    — plenty of defenders have 0.00 goals/90 — so nobody is marked down for it
    and only genuinely clearing the pack earns green. Grading that as a third
    red would say a defender is failing at something defenders do not do.

    When the cuts differ the position really does spread out, and sitting at the
    bottom of it means something. Ties at the lower cut go red rather than
    amber, which is what puts a midfielder who has never once hit the DefCon
    threshold in the red column instead of the middle of the pack.
    """
    if p33 is None or p67 is None:
        return ""
    if p33 == p67:
        return "b-good" if value > p33 else "b-avg"
    if value >= p67:
        return "b-good"
    if value <= p33:
        return "b-bad"
    return "b-avg"


def assign_bands(players: list[dict[str, Any]]) -> None:
    """Attach a colour class per stat to every player, in place."""
    for player in players:
        player["bands"] = {}
    positions = {p["position"] for p in players}
    for position in positions:
        in_position = [p for p in players if p["position"] == position]
        rated = [p for p in in_position if (p["minutes"] or 0) >= BAND_MINUTES_FLOOR]
        for stat in BAND_RATE_STATS + BAND_LEVEL_STATS:
            values = sorted(float(p.get(stat) or 0.0) for p in rated)
            p33, p67 = _percentile(values, 1 / 3), _percentile(values, 2 / 3)
            rates_only = stat in BAND_RATE_STATS
            for player in in_position:
                if rates_only and (player["minutes"] or 0) < BAND_MINUTES_FLOOR:
                    # Too little football for the rate to mean anything. Half a
                    # match of 0.47 xG becomes 0.94 per 90, which would grade
                    # green among the best in the position off one substitution.
                    player["bands"][stat] = BAND_THIN
                    continue
                player["bands"][stat] = _band(float(player.get(stat) or 0.0), p33, p67)

        for stat, (count_field, minimum) in BAND_SAMPLE_STATS.items():
            # Both the cuts and the colouring are limited to players whose
            # sample clears the bar; everyone else is left uncoloured.
            trusted = [
                p for p in in_position
                if p.get(stat) is not None and (p.get(count_field) or 0) >= minimum
            ]
            values = sorted(float(p[stat]) for p in trusted)
            p33, p67 = _percentile(values, 1 / 3), _percentile(values, 2 / 3)
            for player in in_position:
                if player.get(stat) is None:
                    continue  # not a thin sample — the stat does not apply here
                if (player.get(count_field) or 0) < minimum:
                    player["bands"][stat] = BAND_THIN
                else:
                    player["bands"][stat] = _band(float(player[stat]), p33, p67)


# A job that stopped beating for this long is assumed dead — killed, powered
# off, crashed hard enough to skip its cleanup. Without this a single hard kill
# would leave the UI insisting a refresh is running forever.
JOB_STALE_AFTER_SECONDS = 900


def data_version(conn: sqlite3.Connection) -> str:
    """A short token that changes whenever a job changes what the pages show.

    The browser polls this and reloads when it differs from the one it was
    served with — which is how a cron job in another process gets an already-open
    page to update itself.
    """
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    joined = "|".join(str(meta.get(key) or "") for key in DATA_VERSION_KEYS)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def active_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The job running right now, in this process or any other. None if idle."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (ACTIVE_JOB_KEY,)
    ).fetchone()
    payload = jload(row["value"] if row else None)
    if not isinstance(payload, dict):
        return None
    if _age_seconds(payload.get("heartbeat") or payload.get("started_at")) > JOB_STALE_AFTER_SECONDS:
        return None
    return payload


def data_freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    row = conn.execute(
        "SELECT stage, key, ok, detail, fetched_at FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "last_refresh": meta.get("last_refresh_completed"),
        "last_refresh_seconds": meta.get("last_refresh_seconds"),
        "failed_stages": [s for s in (meta.get("last_refresh_failed_stages") or "").split(",") if s],
        # The crowd job runs on its own schedule now, so it needs its own line.
        "last_crowd": meta.get("last_crowd_completed"),
        "crowd_failed_stages": [
            s for s in (meta.get("last_crowd_failed_stages") or "").split(",") if s
        ],
        "bootstrap_fetched_at": meta.get("bootstrap_fetched_at"),
        "projections_built_at": meta.get("projections_built_at"),
        "historical_synced_at": meta.get("historical_synced_at"),
        "season_current_weight": meta.get("season_current_weight"),
        "last_event": dict(row) if row else None,
    }


def gameweek_info(conn: sqlite3.Connection) -> dict[str, Any]:
    current = conn.execute("SELECT * FROM gameweeks WHERE is_current = 1").fetchone()
    nxt = conn.execute("SELECT * FROM gameweeks WHERE is_next = 1").fetchone()
    return {
        "current": dict(current) if current else None,
        "next": dict(nxt) if nxt else None,
        "season_started": bool(
            conn.execute("SELECT COUNT(*) AS n FROM gameweeks WHERE finished = 1").fetchone()["n"]
        ),
    }


def my_entries(conn: sqlite3.Connection, cfg) -> list[dict[str, Any]]:
    """Configured teams, whether or not they've been fetched yet."""
    rows = {r["team_id"]: dict(r) for r in conn.execute("SELECT * FROM my_entries")}
    out = []
    for team_id in cfg.team_ids:
        entry = rows.get(team_id) or {"team_id": team_id, "name": None}
        entry["label"] = cfg.label_for(team_id) or entry.get("name") or f"Team {team_id}"
        entry["known"] = team_id in rows
        out.append(entry)
    return out


def gameweek_progress(conn: sqlite3.Connection, event: int) -> dict[str, Any]:
    """How far through its own fixtures a gameweek is.

    Taken from the fixtures rather than `gameweeks.finished`, which stays 0 for
    days after the last whistle while FPL runs its data checks. Four states:

      upcoming    nothing has kicked off
      live        under way — any total is a running one, not a result
      provisional every match played, but FPL has not marked the week checked,
                  so bonus and any correction can still move it
      final       FPL has signed it off

    The distinction that matters on the page is `live`: a partial score sitting
    next to a full-gameweek projection reads as a collapse rather than a
    gameweek that is only half played.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN started = 1 THEN 1 ELSE 0 END) AS started, "
        "SUM(CASE WHEN finished = 1 THEN 1 ELSE 0 END) AS finished "
        "FROM fixtures WHERE event = ?",
        (event,),
    ).fetchone()
    total = (row["total"] if row else 0) or 0
    started = (row["started"] if row else 0) or 0
    finished = (row["finished"] if row else 0) or 0

    gw = conn.execute(
        "SELECT finished, data_checked FROM gameweeks WHERE id = ?", (event,)
    ).fetchone()
    checked = bool(gw["data_checked"]) if gw else False

    if not total:
        state = "unknown"
    elif started == 0:
        state = "upcoming"
    elif finished < total:
        state = "live"
    elif checked:
        state = "final"
    else:
        state = "provisional"
    return {
        "state": state,
        "total": total,
        "started": started,
        "finished": finished,
        "settled": state in ("provisional", "final"),
    }


def squad_for(conn: sqlite3.Connection, team_id: int, event: int | None = None) -> dict[str, Any]:
    """The 15 picked for a gameweek, enriched with projections and next fixture."""
    if event is None:
        row = conn.execute(
            "SELECT MAX(event) AS e FROM my_picks WHERE team_id = ?", (team_id,)
        ).fetchone()
        event = row["e"] if row and row["e"] else None
    if not event:
        return {"event": None, "picks": [], "available": False}

    proj_event_row = conn.execute("SELECT MIN(event) AS e FROM projections").fetchone()
    proj_event = proj_event_row["e"] if proj_event_row and proj_event_row["e"] else event

    sql = """
        SELECT mp.position, mp.multiplier, mp.is_captain, mp.is_vice_captain,
               mp.selling_price, mp.purchase_price,
               p.id, p.web_name, p.element_type, p.now_cost, p.status, p.news,
               p.chance_of_playing_next_round, p.form, p.total_points,
               p.selected_by_percent, p.ep_next,
               t.name AS team_name, t.short_name AS team_short, t.code AS team_code
        FROM my_picks mp
        JOIN players p ON p.id = mp.player_id
        LEFT JOIN teams t ON t.id = p.team
        WHERE mp.team_id = ? AND mp.event = ?
        ORDER BY mp.position
    """
    picks = [dict(r) for r in conn.execute(sql, (team_id, event))]

    # The ticker spans the whole projection horizon, so it is built from every
    # event -- not just the one the xPts numbers below are scoped to.
    ticker_fixtures: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT pr.player_id, pr.event, pr.was_home, pr.difficulty, t.short_name AS opp "
        "FROM projections pr LEFT JOIN teams t ON t.id = pr.opponent_team "
        "ORDER BY pr.event"
    ):
        ticker_fixtures.setdefault(r["player_id"], []).append(
            {
                "opp": r["opp"],
                "home": bool(r["was_home"]),
                "difficulty": r["difficulty"],
                "event": r["event"],
            }
        )

    projections = {}
    for r in conn.execute(
        "SELECT pr.player_id, pr.xpts, pr.p_start, pr.expected_minutes, pr.components, "
        "pr.adjustments FROM projections pr WHERE pr.event = ?",
        (proj_event,),
    ):
        entry = projections.setdefault(
            r["player_id"], {"xpts": 0.0, "fixtures": [], "adjustments": []}
        )
        entry["xpts"] += r["xpts"] or 0.0
        entry["p_start"] = r["p_start"]
        entry["expected_minutes"] = r["expected_minutes"]
        entry["components"] = jload(r["components"], {})
        entry["adjustments"] = jload(r["adjustments"], [])
        entry["fixtures"] = ticker_fixtures.get(r["player_id"], [])[:6]

    # What we projected for this gameweek before it kicked off. Only present for
    # gameweeks that passed through a refresh while still unplayed — see
    # `archive_projections`; anything older than that feature is simply not there.
    archived = {
        r["player_id"]: r["xpts"]
        for r in conn.execute(
            "SELECT player_id, xpts FROM projection_history WHERE event = ?", (event,)
        )
    }
    archived_at = conn.execute(
        "SELECT MAX(captured_at) AS at FROM projection_history WHERE event = ?", (event,)
    ).fetchone()

    live_points = {
        r["player_id"]: r["total_points"]
        for r in conn.execute(
            "SELECT player_id, SUM(total_points) AS total_points FROM player_gw_history "
            "WHERE round = ? GROUP BY player_id",
            (event,),
        )
    }

    from .config import IMAGE_ROOT
    from .services.kits import have_kit, kit_name

    static_dir = IMAGE_ROOT
    for pick in picks:
        pick["position_name"] = POSITION_NAMES.get(pick["element_type"], "?")
        pick["price"] = (pick["now_cost"] or 0) / 10.0
        pick["on_bench"] = pick["position"] > 11
        pick["projection"] = projections.get(pick["id"], {})
        pick["gw_points"] = live_points.get(pick["id"])
        pick["xpts_last"] = archived.get(pick["id"])
        pick["flagged"] = (pick["status"] or "a") != "a"
        # Keepers wear a different shirt. A missing file leaves this None and
        # the pitch falls back to a plain card rather than a broken image.
        keeper = pick["element_type"] == 1
        pick["kit"] = (
            f"kits/{kit_name(pick['team_code'], keeper)}"
            if have_kit(static_dir, pick["team_code"], keeper) else None
        )

    xi = [p for p in picks if not p["on_bench"]]
    bench = [p for p in picks if p["on_bench"]]
    counts = {pos: sum(1 for p in xi if p["position_name"] == pos) for pos in ("GKP", "DEF", "MID", "FWD")}
    chips = [
        dict(r) for r in conn.execute(
            "SELECT name, event FROM my_chips WHERE team_id = ? ORDER BY event", (team_id,)
        )
    ]
    # FPL's own total for the gameweek is authoritative: it already has the
    # captain doubled and any transfer hit taken off, neither of which falls out
    # of summing the picks. The sum is kept as a fallback for a gameweek FPL has
    # not filed yet.
    scored = conn.execute(
        "SELECT points, points_on_bench, event_transfers, event_transfers_cost, rank "
        "FROM my_entry_gw WHERE team_id = ? AND event = ?",
        (team_id, event),
    ).fetchone()
    xi_actual = sum(
        (p["gw_points"] or 0) * (p["multiplier"] or 0) for p in xi
    ) if any(p["gw_points"] is not None for p in xi) else None
    bench_actual = sum((p["gw_points"] or 0) for p in bench)
    # Captain doubled, because the score it is being compared against is.
    xi_projected = (
        round(sum((archived.get(p["id"]) or 0.0) * (p["multiplier"] or 0) for p in xi), 1)
        if any(p["id"] in archived for p in xi) else None
    )
    progress = gameweek_progress(conn, event)
    points = (scored["points"] if scored else xi_actual)
    # Only worth stating once every match is in. Mid-gameweek it would be
    # measuring a full projection against a partial score.
    over_under = (
        round(points - xi_projected, 1)
        if (xi_projected is not None and points is not None and progress["settled"])
        else None
    )
    return {
        "event": event,
        "projection_event": proj_event,
        "available": bool(picks),
        "picks": picks,
        "xi": xi,
        "bench": bench,
        "formation": f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}" if xi else "",
        "xi_xpts": round(sum(p["projection"].get("xpts", 0.0) for p in xi), 2),
        "squad_value": round(sum(p["now_cost"] or 0 for p in picks) / 10.0, 1),
        "chips": chips,
        "captain": next((p for p in picks if p["is_captain"]), None),
        "vice": next((p for p in picks if p["is_vice_captain"]), None),
        "scored": dict(scored) if scored else None,
        "progress": progress,
        "xi_projected": xi_projected,
        "projected_at": archived_at["at"] if archived_at else None,
        "over_under": over_under,
        "xi_actual": xi_actual,
        "bench_actual": bench_actual,
        "counted": sum(1 for p in xi if p["gw_points"] is not None),
    }


def entry_stats(conn: sqlite3.Connection, team_id: int) -> dict[str, Any]:
    """Gameweek history, overall rank, and every classic league standing."""
    entry = conn.execute(
        "SELECT * FROM my_entries WHERE team_id = ?", (team_id,)
    ).fetchone()
    history = [
        dict(r) for r in conn.execute(
            "SELECT * FROM my_entry_gw WHERE team_id = ? ORDER BY event", (team_id,)
        )
    ]
    leagues = [
        dict(r) for r in conn.execute(
            """
            SELECT league_id, name, entry_rank, entry_last_rank,
                   rank_count AS num_entries, entry_percentile_rank,
                   entry_total AS total, fetched_at
            FROM my_leagues
            WHERE team_id = ?
            ORDER BY COALESCE(rank_count, 0) DESC, name
            """,
            (team_id,),
        )
    ]
    # A gameweek score is the same number in every league, and the entry record
    # already has it, so it is filled in here rather than stored per league.
    event_total = entry["summary_event_points"] if entry else None
    for league in leagues:
        rank = league.get("entry_rank")
        last = league.get("entry_last_rank")
        league["rank"] = rank
        # last_rank is 0, not null, until a league has been ranked twice.
        league["movement"] = (last - rank) if (rank and last) else None
        league["event_total"] = event_total
        total = league.get("num_entries")
        # Computed from the count where we have it — FPL's own percentile is
        # rounded to the nearest five, so it is only the fallback.
        pct = league.get("entry_percentile_rank")
        league["percentile"] = (
            round(100.0 * rank / total, 1) if (rank and total)
            else float(pct) if pct is not None
            else None
        )

    best = max(history, key=lambda h: h["points"] or 0, default=None)
    worst = min(history, key=lambda h: h["points"] or 0, default=None)
    return {
        "entry": dict(entry) if entry else None,
        "history": history,
        "leagues": leagues,
        "totals": {
            "points": entry["summary_overall_points"] if entry else None,
            "rank": entry["summary_overall_rank"] if entry else None,
            "event_points": entry["summary_event_points"] if entry else None,
            "event_rank": entry["summary_event_rank"] if entry else None,
            "played": len(history),
            "average": round(sum(h["points"] or 0 for h in history) / len(history), 1) if history else None,
            "best": best,
            "worst": worst,
            "total_hits": sum(h["event_transfers_cost"] or 0 for h in history),
            "bench_points": sum(h["points_on_bench"] or 0 for h in history),
        },
    }


def player_table(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every player with underlying stats + projections, for the explorer."""
    next_opp: dict[int, list[dict[str, Any]]] = {}

    # `projections` is read for the fixture run only — the opponent, where it is
    # played and how hard it is. The xPts columns this table used to carry were
    # dropped, so the projected points themselves are not selected.
    for r in conn.execute(
        "SELECT pr.player_id, pr.event, pr.was_home, pr.difficulty, "
        "t.short_name AS opp FROM projections pr "
        "LEFT JOIN teams t ON t.id = pr.opponent_team ORDER BY pr.event"
    ):
        pid = r["player_id"]
        next_opp.setdefault(pid, []).append(
            {
                "opp": r["opp"],
                "home": bool(r["was_home"]),
                "difficulty": r["difficulty"],
                "event": r["event"],
            }
        )

    # DefCon reliability uses the engine's own calculation, so the column and
    # the projection can never disagree.
    all_seasons = [
        r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM historical_player_gw ORDER BY season DESC"
        )
    ]
    reliability: dict[int, dict[str, Any]] = {
        pid: {"n": n, "rate": hits / n}
        for pid, (n, hits) in defcon_hit_rates(
            conn, seasons_with_defcon(conn, all_seasons)
        ).items()
        if n
    }

    crowd: dict[int, dict[str, Any]] = {}
    run = conn.execute(
        "SELECT id FROM crowd_intel_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run:
        for r in conn.execute(
            "SELECT player_id, availability, sentiment, confidence, reasons, n_sources "
            "FROM crowd_intel WHERE run_id = ? AND player_id IS NOT NULL",
            (run["id"],),
        ):
            crowd[r["player_id"]] = {
                "availability": r["availability"],
                "sentiment": r["sentiment"],
                "confidence": r["confidence"],
                "n_sources": r["n_sources"],
                "reasons": jload(r["reasons"], []),
            }

    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT p.*, t.name AS team_name, t.short_name AS team_short "
        "FROM players p LEFT JOIN teams t ON t.id = p.team"
    ):
        pid = r["id"]
        price = (r["now_cost"] or 0) / 10.0
        minutes = r["minutes"] or 0
        rel = reliability.get(pid)
        fixtures = next_opp.get(pid, [])[:6]

        out.append(
            {
                "id": pid,
                "name": r["web_name"],
                "full_name": f"{r['first_name']} {r['second_name']}".strip(),
                "team": r["team_short"] or "",
                "team_name": r["team_name"] or "",
                "position": POSITION_NAMES.get(r["element_type"], "?"),
                "position_id": r["element_type"],
                "price": price,
                "total_points": r["total_points"] or 0,
                "ppm": round((r["total_points"] or 0) / price, 2) if price else 0.0,
                "ppg": r["points_per_game"] or 0.0,
                "form": r["form"] or 0.0,
                "ep_next": r["ep_next"] or 0.0,
                "ownership": r["selected_by_percent"] or 0.0,
                "minutes": minutes,
                "starts": r["starts"] or 0,
                "xg90": round(r["expected_goals_per_90"] or 0.0, 2),
                "xa90": round(r["expected_assists_per_90"] or 0.0, 2),
                "xgi90": round(r["expected_goal_involvements_per_90"] or 0.0, 2),
                "xgc90": round(r["expected_goals_conceded_per_90"] or 0.0, 2),
                "xg": round(r["expected_goals"] or 0.0, 2),
                "xa": round(r["expected_assists"] or 0.0, 2),
                "goals": r["goals_scored"] or 0,
                "assists": r["assists"] or 0,
                # Per 90 from actual output, alongside the expected-goals view of
                # the same thing: xG/90 says how good the chances were, G/90 says
                # what was done with them.
                "goals90": round(90.0 * (r["goals_scored"] or 0) / minutes, 2) if minutes else 0.0,
                "assists90": round(90.0 * (r["assists"] or 0) / minutes, 2) if minutes else 0.0,
                # How long they last when picked, which is a different question
                # from total minutes: it separates a nailed starter from someone
                # who plays often but comes off on the hour.
                "mins_per_start": round(minutes / (r["starts"] or 0), 1) if (r["starts"] or 0) else 0.0,
                "defcon90": round(r["defensive_contribution_per_90"] or 0.0, 2),
                "defcon_reliability": round(rel["rate"], 3) if rel else None,
                "defcon_sample": rel["n"] if rel else 0,
                "defcon_threshold": DEFCON_THRESHOLD.get(r["element_type"]),
                "bonus": r["bonus"] or 0,
                "bps": r["bps"] or 0,
                "ict": r["ict_index"] or 0.0,
                "saves90": round(r["saves_per_90"] or 0.0, 2),
                "status": r["status"] or "a",
                "news": r["news"] or "",
                "chance": r["chance_of_playing_next_round"],
                "fixtures": fixtures,
                "next_opponent": fixtures[0] if fixtures else None,
                "crowd": crowd.get(pid),
            }
        )
    assign_bands(out)
    return out


# --- The match list ----------------------------------------------------------
#
# Fixtures and results for the whole league, which is the one view here that is
# not about the user's own squad.
#
# Everything about who did what comes from `fixture_stats`, never from
# `player_gw_history`: see the note on the table in db.py for why. The short
# version is that the history table is fetched on a per-player budget, so its
# coverage is arbitrary, and it files an own goal under the player who scored it
# rather than the side it counted for.

# Stat lines that belong under a team on a result card, in the order they read.
GOAL_IDENTIFIERS = ("goals_scored", "own_goals")


def _fixture_stats(conn: sqlite3.Connection, fixture_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """fixture id -> the recorded stat lines, with player names attached.

    LEFT JOIN on purpose: `players` is the current bootstrap, so a player who
    has since left the league is no longer in it — and his goals still happened.
    """
    if not fixture_ids:
        return {}
    marks = ",".join("?" for _ in fixture_ids)
    out: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
        f"SELECT fs.fixture, fs.identifier, fs.side, fs.value, fs.player_id, "
        f"p.web_name, t.short_name AS team_short "
        f"FROM fixture_stats fs "
        f"LEFT JOIN players p ON p.id = fs.player_id "
        f"LEFT JOIN teams t ON t.id = p.team "
        f"WHERE fs.fixture IN ({marks}) "
        f"ORDER BY fs.value DESC, p.web_name",
        fixture_ids,
    ):
        out.setdefault(r["fixture"], []).append(
            {
                "identifier": r["identifier"],
                "side": r["side"],
                "value": r["value"] or 0,
                "player_id": r["player_id"],
                # A player who has left the league keeps his goals; only his name
                # is gone, and a dash says that rather than dropping the line.
                "name": r["web_name"] or "—",
                "team": r["team_short"] or "",
            }
        )
    return out


def _side(team: dict[str, Any] | None, score: Any, difficulty: Any) -> dict[str, Any]:
    team = team or {"id": None, "name": "?", "short": "?", "code": None}
    return {
        **team,
        "score": score,
        "difficulty": difficulty,
        "goals": [],
        "assists": [],
        "cards": [],
        "saves": [],
        "bonus": [],
    }


def match_day(conn: sqlite3.Connection, event: int) -> dict[str, Any]:
    """One gameweek's fixtures, with scorers and assists where they exist."""
    from .config import IMAGE_ROOT
    from .services.kits import crest_name, have_crest

    static_dir = IMAGE_ROOT
    teams = {
        r["id"]: {
            "id": r["id"],
            "name": r["name"],
            "short": r["short_name"],
            "code": r["code"],
            # None until the refresh has fetched it; the card falls back to the
            # three-letter code so the row keeps its shape either way.
            "crest": (
                f"crests/{crest_name(r['code'])}"
                if have_crest(static_dir, r["code"]) else None
            ),
        }
        for r in conn.execute("SELECT id, name, short_name, code FROM teams")
    }
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM fixtures WHERE event = ? ORDER BY kickoff_time, id", (event,)
        )
    ]
    stats = _fixture_stats(conn, [r["id"] for r in rows])

    fixtures: list[dict[str, Any]] = []
    for r in rows:
        home = _side(teams.get(r["team_h"]), r["team_h_score"], r["team_h_difficulty"])
        away = _side(teams.get(r["team_a"]), r["team_a_score"], r["team_a_difficulty"])
        sides = {"h": home, "a": away}

        for line in stats.get(r["id"], []):
            scored_by = sides.get(line["side"])
            if scored_by is None:
                continue
            if line["identifier"] == "goals_scored":
                scored_by["goals"].append({**line, "own": False})
            elif line["identifier"] == "own_goals":
                # Filed under the side it counted FOR, which is the other one,
                # and marked — that is both the football convention and what
                # makes the listed goals add up to the scoreline.
                other = sides["a"] if line["side"] == "h" else sides["h"]
                other["goals"].append({**line, "own": True})
            elif line["identifier"] == "assists":
                scored_by["assists"].append(line)
            elif line["identifier"] in ("yellow_cards", "red_cards"):
                scored_by["cards"].append({**line, "red": line["identifier"] == "red_cards"})
            elif line["identifier"] == "saves":
                scored_by["saves"].append(line)
            elif line["identifier"] == "bonus":
                scored_by["bonus"].append(line)

        for side in (home, away):
            # An own goal is not the scorer's achievement, so it sorts last
            # rather than topping the list on a two-goal value.
            side["goals"].sort(key=lambda g: (g["own"], -g["value"], g["name"]))
            side["assists"].sort(key=lambda a: (-a["value"], a["name"]))
            side["bonus"].sort(key=lambda b: -b["value"])

        fixtures.append(
            {
                "id": r["id"],
                "kickoff_time": r["kickoff_time"],
                "started": bool(r["started"]),
                "finished": bool(r["finished"]),
                "minutes": r["minutes"] or 0,
                "home": home,
                "away": away,
                "has_stats": bool(stats.get(r["id"])),
            }
        )

    return {
        "event": event,
        "fixtures": fixtures,
        "days": _group_by_day(fixtures),
        "progress": gameweek_progress(conn, event),
    }


def _local_date(kickoff: str | None) -> str | None:
    """The calendar date a kickoff falls on, in the reader's own timezone.

    A 20:00 UTC Saturday kickoff is Sunday morning in Singapore, and a fixture
    list that says otherwise is wrong for the person reading it. Grouping
    therefore has to happen after the conversion, not on the UTC string.
    """
    if not kickoff:
        return None
    try:
        when = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().date().isoformat()


def _group_by_day(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fixtures split into the days they are played on, in order.

    Fixtures arrive ordered by kickoff, so consecutive grouping is enough and
    keeps a day that straddles midnight in one block. A fixture with no kickoff
    time yet gets its own group at the end rather than being hidden.
    """
    days: list[dict[str, Any]] = []
    for fixture in fixtures:
        date = _local_date(fixture["kickoff_time"])
        if not days or days[-1]["date"] != date:
            days.append({"date": date, "fixtures": []})
        days[-1]["fixtures"].append(fixture)
    return days


def matchweek_index(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every gameweek that has fixtures, with the state of each — one query.

    State comes from the fixtures, not from `gameweeks.finished`, which stays 0
    for days after the last whistle while FPL runs its data checks. Building the
    navigation strip off that flag would leave a gameweek that is over and paid
    out looking like it was still to come.
    """
    names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM gameweeks")
    }
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT event, COUNT(*) AS total, "
        "SUM(CASE WHEN started = 1 THEN 1 ELSE 0 END) AS started, "
        "SUM(CASE WHEN finished = 1 THEN 1 ELSE 0 END) AS finished "
        "FROM fixtures WHERE event IS NOT NULL GROUP BY event ORDER BY event"
    ):
        started = r["started"] or 0
        finished = r["finished"] or 0
        if started == 0:
            state = "upcoming"
        elif finished < r["total"]:
            state = "live"
        else:
            state = "played"
        out.append(
            {
                "event": r["event"],
                "name": names.get(r["event"]) or f"Gameweek {r['event']}",
                "total": r["total"],
                "state": state,
            }
        )
    return out


def current_matchweek(index: list[dict[str, Any]]) -> int | None:
    """The matchweek to open on: the one being played, else the one next up.

    Falls back to the last played week once the season is over, so the page
    always lands on football rather than on an empty May.
    """
    if not index:
        return None
    for week in index:
        if week["state"] == "live":
            return week["event"]
    for week in index:
        if week["state"] == "upcoming":
            return week["event"]
    return index[-1]["event"]


# --- Expandable per-opponent history on the explorer -------------------------
#
# Two questions the table itself cannot answer, both asked per upcoming fixture:
# how the player's club has fared against that opponent, and what the player
# personally did in those games. Both are capped at the last five meetings —
# further back is a different squad and a different manager.
HISTORY_LIMIT = 5


def _current_season_label(conn: sqlite3.Connection) -> str:
    """The live season as the historical dataset would name it, e.g. "2026-27".

    Taken from the first kickoff of the season rather than the wall clock, so it
    is right in July when nothing has been played and right in May when the
    calendar year has moved on.
    """
    row = conn.execute(
        "SELECT MIN(kickoff_time) AS first FROM fixtures WHERE kickoff_time IS NOT NULL"
    ).fetchone()
    start = (row["first"] or "")[:4] if row else ""
    if not start.isdigit():
        return ""
    year = int(start)
    return f"{year}-{str(year + 1)[2:]}"


def _team_lookup(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Current teams keyed by id, with the name the historical dataset uses.

    The two sources spell half a dozen clubs differently — bootstrap says "Man
    City", the dataset says "Manchester City" — so every join between them goes
    through the same normaliser the loader used.
    """
    from .services.historical import normalise_team_name

    by_id: dict[int, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, name, short_name FROM teams"):
        by_id[r["id"]] = {
            "id": r["id"],
            "short": r["short_name"] or r["name"],
            "name": r["name"],
            "hist_name": normalise_team_name(r["name"]),
        }
    return by_id


def _team_meetings(
    conn: sqlite3.Connection, team: str, opponents: set[str], exclude_season: str
) -> dict[str, list[dict[str, Any]]]:
    """Past results for one club against each named opponent, newest first.

    The dataset stores players, not matches, so the scoreline is reconstructed:
    within a fixture the largest `goals_conceded` on a team's rows is what that
    team let in, because whoever played the full match conceded all of them —
    and one side's goals conceded is the other side's goals scored. That picks up
    own goals for free, which summing `goals_scored` would miss.
    """
    if not opponents:
        return {}
    marks = ",".join("?" for _ in opponents)
    sql = f"""
        WITH ours AS (
            SELECT season, fixture,
                   MAX(opponent_team_name) AS opp,
                   MAX(was_home) AS was_home,
                   MAX(round)    AS round,
                   MAX(goals_conceded) AS conceded
            FROM historical_player_gw
            WHERE team_name = ? AND season <> ? AND opponent_team_name IN ({marks})
            GROUP BY season, fixture
        ),
        theirs AS (
            SELECT season, fixture, MAX(goals_conceded) AS conceded
            FROM historical_player_gw
            WHERE opponent_team_name = ? AND season <> ?
            GROUP BY season, fixture
        )
        SELECT ours.season, ours.round, ours.opp, ours.was_home,
               theirs.conceded AS scored, ours.conceded AS against
        FROM ours JOIN theirs
          ON theirs.season = ours.season AND theirs.fixture = ours.fixture
        ORDER BY ours.season DESC, ours.round DESC
    """
    params = (team, exclude_season, *sorted(opponents), team, exclude_season)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(sql, params):
        out.setdefault(r["opp"], []).append(
            {
                "season": r["season"],
                "round": r["round"],
                "home": bool(r["was_home"]),
                "scored": r["scored"] or 0,
                "against": r["against"] or 0,
                "result": _result(r["scored"] or 0, r["against"] or 0),
            }
        )
    return out


def _result(scored: int, against: int) -> str:
    return "W" if scored > against else ("L" if scored < against else "D")


def _current_team_meetings(
    conn: sqlite3.Connection, team_id: int, season: str
) -> dict[int, list[dict[str, Any]]]:
    """This season's finished results for one club, keyed by opponent team id."""
    out: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT event, team_h, team_a, team_h_score, team_a_score FROM fixtures "
        "WHERE finished = 1 AND team_h_score IS NOT NULL AND team_a_score IS NOT NULL "
        "AND (team_h = ? OR team_a = ?) ORDER BY event DESC",
        (team_id, team_id),
    ):
        home = r["team_h"] == team_id
        scored = r["team_h_score"] if home else r["team_a_score"]
        against = r["team_a_score"] if home else r["team_h_score"]
        opponent = r["team_a"] if home else r["team_h"]
        out.setdefault(opponent, []).append(
            {
                "season": season,
                "round": r["event"],
                "home": home,
                "scored": scored,
                "against": against,
                "result": _result(scored, against),
            }
        )
    return out


def _player_meetings(
    conn: sqlite3.Connection,
    player_code: int | None,
    opponents: set[str],
    exclude_season: str,
    defcon_seasons: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """A player's past appearances against each named opponent, newest first.

    Appearances only. A run of unused-substitute zeroes says nothing about how
    the player fares against a team, and printing "0 goals, 0 assists" for a
    game they watched would read as a bad performance rather than no performance.
    """
    if not player_code or not opponents:
        return {}
    marks = ",".join("?" for _ in opponents)
    sql = f"""
        SELECT season, round, opponent_team_name AS opp, was_home, minutes,
               goals_scored, assists, defensive_contribution, total_points,
               saves, goals_conceded
        FROM historical_player_gw
        WHERE player_code = ? AND season <> ? AND minutes > 0
          AND opponent_team_name IN ({marks})
        ORDER BY season DESC, round DESC
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(sql, (player_code, exclude_season, *sorted(opponents))):
        out.setdefault(r["opp"], []).append(
            {
                "season": r["season"],
                "round": r["round"],
                "home": bool(r["was_home"]),
                "minutes": r["minutes"] or 0,
                "goals": r["goals_scored"] or 0,
                "assists": r["assists"] or 0,
                # DefCon only exists from 2025/26. Older rows store a zero,
                # which is a missing stat rather than a quiet defensive shift.
                "defcon": r["defensive_contribution"] if r["season"] in defcon_seasons else None,
                # Keepers are scored on these two rather than on goals and
                # assists; the page shows whichever pair the position earns.
                "saves": r["saves"] or 0,
                "conceded": r["goals_conceded"] or 0,
                "points": r["total_points"] or 0,
            }
        )
    return out


def _current_player_meetings(
    conn: sqlite3.Connection, player_id: int, season: str
) -> dict[int, list[dict[str, Any]]]:
    """This season's appearances for one player, keyed by opponent team id."""
    out: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT round, opponent_team, was_home, minutes, goals_scored, assists, "
        "defensive_contribution, total_points, saves, goals_conceded "
        "FROM player_gw_history "
        "WHERE player_id = ? AND minutes > 0 ORDER BY round DESC",
        (player_id,),
    ):
        out.setdefault(r["opponent_team"], []).append(
            {
                "season": season,
                "round": r["round"],
                "home": bool(r["was_home"]),
                "minutes": r["minutes"] or 0,
                "goals": r["goals_scored"] or 0,
                "assists": r["assists"] or 0,
                "defcon": r["defensive_contribution"],
                "saves": r["saves"] or 0,
                "conceded": r["goals_conceded"] or 0,
                "points": r["total_points"] or 0,
            }
        )
    return out


def _season_log(
    conn: sqlite3.Connection,
    player_id: int,
    team_id: int,
    element_type: int,
    teams: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Every match of this season for one player, oldest first, plus the totals.

    The opponent blocks beside it answer "how does this player go against these
    teams"; this answers "what have they actually been doing lately", which is
    the other half of the same question, and the half the table's season-long
    averages flatten into a single number.

    The spine is the club's finished fixtures, not the player's history rows.
    That distinction is the whole correctness of this function: element
    summaries are fetched on a per-player budget, so 300-odd players are
    carrying a single history row from whenever they were last picked up, and
    building the log out of those rows would quietly drop the gameweeks nobody
    had fetched yet — the player who scored in GW2 would be shown a season with
    no GW2 in it. Walking the fixtures instead means every gameweek that has
    been played is listed for everyone, and the gaps are marked as gaps.

    Each fixture is then filled from the best source that has it:

    * `player_gw_history` — minutes, DefCon, clean sheet, goals conceded and the
      match's points. Only this has them, and only where it has been fetched.
    * `fixture_stats` — goals, assists, saves, bonus and cards. Complete for
      every fixture and every player (see the note on the table in db.py), so
      these read as real zeroes even on a gameweek with no history row.

    A fixture with no history row is therefore `partial`, not `blank`: what was
    scored is known, what was played is not, and the page says so rather than
    printing a nought the player never earned.
    """
    threshold = DEFCON_THRESHOLD.get(element_type)

    # Every event this player registered, whether or not their summary is in.
    events: dict[int, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT fixture, identifier, value FROM fixture_stats WHERE player_id = ?",
        (player_id,),
    ):
        events.setdefault(r["fixture"], {})[r["identifier"]] = r["value"] or 0

    history: dict[int, sqlite3.Row] = {
        r["fixture"]: r for r in conn.execute(
            "SELECT fixture, round, opponent_team, was_home, minutes, starts, "
            "total_points, goals_scored, assists, clean_sheets, goals_conceded, "
            "saves, bonus, bps, yellow_cards, red_cards, own_goals, "
            "penalties_missed, penalties_saved, expected_goals, expected_assists, "
            "defensive_contribution FROM player_gw_history WHERE player_id = ?",
            (player_id,),
        )
    }

    # The club's finished matches, plus any fixture the player has a history row
    # for that is not among them — which is how a mid-season move keeps the
    # half-season played in the other shirt.
    #
    # The other half of a move is left alone on purpose: the new club's earlier
    # fixtures stay in the list, marked as not fetched. Nothing here separates
    # "was not at this club yet" from "summary not pulled yet", and a rule that
    # dropped the club fixture whenever a history row sat elsewhere in the round
    # would throw away the second match of a double gameweek. Seventeen players
    # carry a spare row; none of them is shown as a blank they sat through.
    spine: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT id, event, team_h, team_a, team_h_score, team_a_score, "
        "team_h_difficulty, team_a_difficulty, kickoff_time FROM fixtures "
        "WHERE finished = 1 AND (team_h = ? OR team_a = ?)",
        (team_id, team_id),
    ):
        home = r["team_h"] == team_id
        spine[r["id"]] = {
            "event": r["event"],
            "home": home,
            "opp_id": r["team_a"] if home else r["team_h"],
            "difficulty": r["team_h_difficulty"] if home else r["team_a_difficulty"],
            "scored": r["team_h_score"] if home else r["team_a_score"],
            "against": r["team_a_score"] if home else r["team_h_score"],
            "kickoff": r["kickoff_time"] or "",
        }
    for fixture, h in history.items():
        if fixture in spine:
            continue
        spine[fixture] = {
            "event": h["round"],
            "home": bool(h["was_home"]),
            "opp_id": h["opponent_team"],
            "difficulty": None,
            "scored": None,
            "against": None,
            "kickoff": "",
        }

    rows: list[dict[str, Any]] = []
    for fixture, meta in sorted(
        spine.items(), key=lambda kv: (kv[1]["event"] or 0, kv[1]["kickoff"], kv[0])
    ):
        h = history.get(fixture)
        ev = events.get(fixture, {})
        opponent = teams.get(meta["opp_id"]) or {}
        scored, against = meta["scored"], meta["against"]
        defcon = h["defensive_contribution"] if h is not None else None
        rows.append(
            {
                "event": meta["event"],
                "home": meta["home"],
                "opp": opponent.get("short") or "?",
                "opp_name": opponent.get("name") or "Unknown",
                "difficulty": meta["difficulty"],
                # `loaded` is what the page keys its three row states off: a
                # played game, a game watched, and a game we cannot yet say
                # which of those it was.
                "loaded": h is not None,
                "minutes": h["minutes"] or 0 if h is not None else None,
                "started": bool(h["starts"]) if h is not None else None,
                # Events come from the complete source, so they stand whether or
                # not the summary has been fetched.
                "goals": ev.get("goals_scored", 0),
                "assists": ev.get("assists", 0),
                "saves": ev.get("saves", 0),
                "bonus": ev.get("bonus", 0),
                "yellow": ev.get("yellow_cards", 0),
                "red": ev.get("red_cards", 0),
                "own_goals": ev.get("own_goals", 0),
                "pens_missed": ev.get("penalties_missed", 0),
                "pens_saved": ev.get("penalties_saved", 0),
                "defcon": defcon,
                # Whether the shift banked the 2 points, not just how busy it
                # was: 9 actions and 10 are one point apart in the table and
                # two apart on the scoresheet.
                "defcon_hit": bool(threshold and defcon is not None and defcon >= threshold),
                "clean_sheet": bool(h["clean_sheets"]) if h is not None else None,
                "conceded": h["goals_conceded"] or 0 if h is not None else None,
                "points": h["total_points"] or 0 if h is not None else None,
                "score": (
                    f"{scored}\u2013{against}"
                    if scored is not None and against is not None else ""
                ),
                "result": (
                    _result(scored, against)
                    if scored is not None and against is not None else ""
                ),
            }
        )

    return {"rows": rows, "totals": _season_totals(conn, player_id, rows, threshold)}


def _season_totals(
    conn: sqlite3.Connection,
    player_id: int,
    rows: list[dict[str, Any]],
    threshold: int | None,
) -> dict[str, Any]:
    """The season's figures, taken from the players table rather than summed.

    Summing the log would make the total only as complete as the log, and on a
    player whose summary is a gameweek behind that is a total that disagrees
    with the Pts column three inches above it. The bootstrap row is the same
    number FPL shows and is right for everybody, so the footer is trustworthy
    even while a row above it is still waiting to be filled in.
    """
    r = conn.execute(
        "SELECT minutes, starts, goals_scored, assists, clean_sheets, "
        "goals_conceded, saves, bonus, yellow_cards, red_cards, "
        "defensive_contribution, total_points FROM players WHERE id = ?",
        (player_id,),
    ).fetchone()
    loaded = [row for row in rows if row["loaded"]]
    return {
        "games": len(rows),
        # Counted off the log, so it can only speak for the rows it has.
        "loaded": len(loaded),
        "missing": len(rows) - len(loaded),
        "played": sum(1 for row in loaded if row["minutes"]),
        "starts": r["starts"] or 0,
        "minutes": r["minutes"] or 0,
        "goals": r["goals_scored"] or 0,
        "assists": r["assists"] or 0,
        "defcon": r["defensive_contribution"] or 0,
        "defcon_hits": sum(1 for row in loaded if row["defcon_hit"]),
        "defcon_chances": sum(1 for row in loaded if (row["minutes"] or 0) >= 60),
        "threshold": threshold,
        "saves": r["saves"] or 0,
        "clean_sheets": r["clean_sheets"] or 0,
        "conceded": r["goals_conceded"] or 0,
        "bonus": r["bonus"] or 0,
        "yellow": r["yellow_cards"] or 0,
        "red": r["red_cards"] or 0,
        "points": r["total_points"] or 0,
    }


def opponent_history(conn: sqlite3.Connection, player_id: int) -> dict[str, Any] | None:
    """Club and player record against each of a player's next fixtures.

    Returns None when the player id is unknown. An opponent with no history at
    all — a promoted club, or one whose last top-flight season predates the
    loaded data — comes back with empty lists, which the page reports as "no
    data" rather than as a run of poor results.
    """
    row = conn.execute(
        "SELECT p.id, p.code, p.web_name, p.first_name, p.second_name, p.team, "
        "p.element_type FROM players p WHERE p.id = ?",
        (player_id,),
    ).fetchone()
    if row is None:
        return None

    teams = _team_lookup(conn)
    club = teams.get(row["team"])
    season = _current_season_label(conn)

    # The same six fixtures the row's ticker shows, so the two line up.
    fixtures: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT pr.event, pr.was_home, pr.difficulty, pr.opponent_team "
        "FROM projections pr WHERE pr.player_id = ? ORDER BY pr.event LIMIT 6",
        (player_id,),
    ):
        opponent = teams.get(r["opponent_team"])
        fixtures.append(
            {
                "event": r["event"],
                "home": bool(r["was_home"]),
                "difficulty": r["difficulty"],
                "opp_id": r["opponent_team"],
                "opp": (opponent or {}).get("short") or "?",
                "opp_name": (opponent or {}).get("name") or "Unknown",
                "hist_name": (opponent or {}).get("hist_name") or "",
            }
        )

    wanted = {f["hist_name"] for f in fixtures if f["hist_name"]}
    # Which opponents the loaded seasons have ever seen. A club that appears
    # nowhere is newly promoted (or was last up before the data starts), and
    # that is a different statement from "we have met but not recently".
    # The player's own club is checked alongside them: when it is the newcomer
    # every row is empty for one reason, and saying so once beats repeating
    # "no meetings" six times as though the opponents were the problem.
    club_name = (club or {}).get("hist_name", "")
    lookup = sorted(wanted | ({club_name} if club_name else set()))
    known = set()
    if lookup:
        marks = ",".join("?" for _ in lookup)
        known = {
            r["team_name"] for r in conn.execute(
                f"SELECT DISTINCT team_name FROM historical_player_gw "
                f"WHERE team_name IN ({marks})",
                tuple(lookup),
            )
        }
    defcon_seasons = set(
        seasons_with_defcon(
            conn,
            [
                r["season"] for r in conn.execute(
                    "SELECT DISTINCT season FROM historical_player_gw ORDER BY season DESC"
                )
            ],
        )
    )
    club_hist = _team_meetings(conn, club_name, wanted, season)
    club_live = _current_team_meetings(conn, row["team"], season) if club else {}
    player_hist = _player_meetings(conn, row["code"], wanted, season, defcon_seasons)
    player_live = _current_player_meetings(conn, player_id, season)

    for fixture in fixtures:
        key, opp_id = fixture["hist_name"], fixture["opp_id"]
        fixture["known"] = key in known
        # This season first: it is both the most recent and the most relevant.
        fixture["team_form"] = (
            club_live.get(opp_id, []) + club_hist.get(key, [])
        )[:HISTORY_LIMIT]
        fixture["player_form"] = (
            player_live.get(opp_id, []) + player_hist.get(key, [])
        )[:HISTORY_LIMIT]

    return {
        "player": {
            "id": row["id"],
            "name": row["web_name"],
            "full_name": f"{row['first_name']} {row['second_name']}".strip(),
            "position": POSITION_NAMES.get(row["element_type"], "?"),
            "team": (club or {}).get("short") or "",
            "team_name": (club or {}).get("name") or "",
            # Lets the page mark the games that actually banked the 2 points
            # rather than printing a raw count nobody can score in their head.
            "defcon_threshold": DEFCON_THRESHOLD.get(row["element_type"]),
            "club_known": club_name in known,
        },
        "limit": HISTORY_LIMIT,
        "fixtures": fixtures,
        "season": season,
        "season_log": _season_log(
            conn, player_id, row["team"], row["element_type"], teams
        ),
    }


def squad_player_ids(conn: sqlite3.Connection, team_id: int) -> set[int]:
    """Who a team owns in its most recent saved picks. Empty before GW1."""
    row = conn.execute(
        "SELECT MAX(event) AS e FROM my_picks WHERE team_id = ?", (team_id,)
    ).fetchone()
    event = row["e"] if row and row["e"] else None
    if not event:
        return set()
    return {
        r["player_id"] for r in conn.execute(
            "SELECT player_id FROM my_picks WHERE team_id = ? AND event = ?",
            (team_id, event),
        )
    }
