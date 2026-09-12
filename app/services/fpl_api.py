"""Official FPL API client and SQLite sync.

Politeness rules (the FPL API is free and unauthenticated — don't abuse it):
  * one shared requests.Session with a real User-Agent
  * a hard minimum gap between requests (~1/sec)
  * bounded retries with exponential backoff on 429/5xx/connection errors

Nothing here is called from a web request. The refresh job owns the network;
pages read from SQLite only.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from typing import Any, Iterable, Sequence

import requests

from ..db import jdump, log_fetch, set_meta, upsert_many, utcnow

log = logging.getLogger(__name__)

BASE = "https://fantasy.premierleague.com/api"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 fpl-helper/1.0 (personal FPL analysis tool)"
)
MIN_INTERVAL = 1.05  # seconds between requests
MAX_RETRIES = 4


class FPLApiError(RuntimeError):
    pass


class FPLClient:
    """Rate-limited HTTP client for the public FPL endpoints."""

    def __init__(self, min_interval: float = MIN_INTERVAL, timeout: float = 30.0) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_request = time.monotonic()

    def get(self, path: str, *, allow_404: bool = False) -> Any:
        url = f"{BASE}/{path.lstrip('/')}"
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
            else:
                if resp.status_code == 404 and allow_404:
                    return None
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_error = exc
                        log.warning("GET %s returned non-JSON body", url)
                elif resp.status_code in (429, 500, 502, 503, 504):
                    last_error = FPLApiError(f"HTTP {resp.status_code} for {url}")
                    log.warning("GET %s -> %s (attempt %d)", url, resp.status_code, attempt + 1)
                else:
                    raise FPLApiError(f"HTTP {resp.status_code} for {url}")
            # exponential backoff with jitter
            time.sleep(min(30.0, (2**attempt) * 1.5) + random.uniform(0, 0.75))
        raise FPLApiError(f"GET {url} failed after {MAX_RETRIES} attempts: {last_error}")

    # --- endpoint wrappers -------------------------------------------------
    def bootstrap_static(self) -> dict:
        return self.get("bootstrap-static/")

    def fixtures(self) -> list[dict]:
        return self.get("fixtures/")

    def element_summary(self, player_id: int) -> dict | None:
        return self.get(f"element-summary/{player_id}/", allow_404=True)

    def entry(self, team_id: int) -> dict | None:
        return self.get(f"entry/{team_id}/", allow_404=True)

    def entry_history(self, team_id: int) -> dict | None:
        return self.get(f"entry/{team_id}/history/", allow_404=True)

    def entry_picks(self, team_id: int, gw: int) -> dict | None:
        return self.get(f"entry/{team_id}/event/{gw}/picks/", allow_404=True)

    def event_live(self, gw: int) -> dict | None:
        return self.get(f"event/{gw}/live/", allow_404=True)


# --- coercion helpers ------------------------------------------------------

def _f(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "None"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int | None = None) -> int | None:
    if value in (None, "", "None"):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _b(value: Any) -> int:
    return 1 if value else 0


def _pick(src: dict, keys: Sequence[str]) -> dict:
    return {k: src.get(k) for k in keys}


PLAYER_INT_FIELDS = (
    "id code team element_type now_cost total_points minutes starts goals_scored "
    "assists clean_sheets goals_conceded saves bonus bps yellow_cards red_cards "
    "chance_of_playing_next_round chance_of_playing_this_round penalties_order "
    "corners_and_indirect_freekicks_order direct_freekicks_order "
    "defensive_contribution clearances_blocks_interceptions tackles recoveries"
).split()

PLAYER_FLOAT_FIELDS = (
    "points_per_game form ep_next selected_by_percent expected_goals expected_assists "
    "expected_goal_involvements expected_goals_conceded expected_goals_per_90 "
    "expected_assists_per_90 expected_goal_involvements_per_90 "
    "expected_goals_conceded_per_90 saves_per_90 starts_per_90 clean_sheets_per_90 "
    "defensive_contribution_per_90 influence creativity threat ict_index"
).split()


def _player_row(element: dict, fetched_at: str) -> dict:
    row: dict[str, Any] = {k: _i(element.get(k)) for k in PLAYER_INT_FIELDS}
    row.update({k: _f(element.get(k)) for k in PLAYER_FLOAT_FIELDS})
    row.update(
        {
            "web_name": element.get("web_name"),
            "first_name": element.get("first_name"),
            "second_name": element.get("second_name"),
            "status": element.get("status"),
            "news": element.get("news") or "",
            "news_added": element.get("news_added"),
            "fetched_at": fetched_at,
        }
    )
    return row


def sync_bootstrap(conn: sqlite3.Connection, client: FPLClient) -> dict:
    """Pull bootstrap-static into players / teams / gameweeks."""
    data = client.bootstrap_static()
    now = utcnow()

    teams = [
        {
            "id": _i(t["id"]),
            "code": _i(t.get("code")),
            "name": t.get("name"),
            "short_name": t.get("short_name"),
            "strength": _i(t.get("strength")),
            "strength_overall_home": _i(t.get("strength_overall_home")),
            "strength_overall_away": _i(t.get("strength_overall_away")),
            "strength_attack_home": _i(t.get("strength_attack_home")),
            "strength_attack_away": _i(t.get("strength_attack_away")),
            "strength_defence_home": _i(t.get("strength_defence_home")),
            "strength_defence_away": _i(t.get("strength_defence_away")),
        }
        for t in data.get("teams", [])
    ]
    upsert_many(conn, "teams", teams)

    players = [_player_row(e, now) for e in data.get("elements", [])]
    upsert_many(conn, "players", players)

    events = [
        {
            "id": _i(e["id"]),
            "name": e.get("name"),
            "deadline_time": e.get("deadline_time"),
            "finished": _b(e.get("finished")),
            "data_checked": _b(e.get("data_checked")),
            "is_current": _b(e.get("is_current")),
            "is_next": _b(e.get("is_next")),
            "is_previous": _b(e.get("is_previous")),
            "average_entry_score": _i(e.get("average_entry_score")),
            "highest_score": _i(e.get("highest_score")),
        }
        for e in data.get("events", [])
    ]
    upsert_many(conn, "gameweeks", events)

    set_meta(conn, "bootstrap_fetched_at", now)
    log_fetch(
        conn,
        "bootstrap",
        "bootstrap-static",
        True,
        f"{len(players)} players, {len(teams)} teams, {len(events)} events",
    )
    log.info(
        "bootstrap synced: %d players, %d teams, %d events",
        len(players), len(teams), len(events),
    )
    return {"players": len(players), "teams": len(teams), "events": len(events)}


def sync_fixtures(conn: sqlite3.Connection, client: FPLClient) -> int:
    payload = client.fixtures()
    rows = [
        {
            "id": _i(f["id"]),
            "code": _i(f.get("code")),
            "event": _i(f.get("event")),
            "kickoff_time": f.get("kickoff_time"),
            "team_h": _i(f.get("team_h")),
            "team_a": _i(f.get("team_a")),
            "team_h_score": _i(f.get("team_h_score")),
            "team_a_score": _i(f.get("team_a_score")),
            "team_h_difficulty": _i(f.get("team_h_difficulty")),
            "team_a_difficulty": _i(f.get("team_a_difficulty")),
            "finished": _b(f.get("finished")),
            "started": _b(f.get("started")),
            "minutes": _i(f.get("minutes"), 0),
        }
        for f in payload
    ]
    upsert_many(conn, "fixtures", rows)
    stats = sync_fixture_stats(conn, payload)
    log_fetch(conn, "fixtures", "fixtures", True, f"{len(rows)} fixtures, {stats} stat rows")
    log.info("fixtures synced: %d (%d stat rows)", len(rows), stats)
    return len(rows)


# The stat lines the Matches page reads. FPL also publishes bps, and bonus is
# derived from it, but neither is a thing that happened in the match the way a
# goal is — and every identifier stored is one more row per fixture per run.
MATCH_STAT_IDENTIFIERS = (
    "goals_scored", "assists", "own_goals", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "saves", "bonus",
)


def sync_fixture_stats(conn: sqlite3.Connection, payload: Sequence[dict]) -> int:
    """Per-player match events, from the `stats` array on each fixture.

    Rewritten per fixture rather than upserted: a stat line can be *withdrawn*
    as well as added — a goal reassigned to another player, a red card
    rescinded, bonus recalculated after the provisional round — and an upsert
    would leave the old row behind forever. Only fixtures that have actually
    started are touched, so an unplayed fixture cannot have its rows cleared by
    a run that happens to see an empty array.
    """
    rows: list[dict[str, Any]] = []
    started: list[int] = []
    for f in payload:
        fixture_id = _i(f.get("id"))
        if fixture_id is None or not f.get("started"):
            continue
        started.append(fixture_id)
        for stat in f.get("stats") or []:
            identifier = stat.get("identifier")
            if identifier not in MATCH_STAT_IDENTIFIERS:
                continue
            for side in ("h", "a"):
                for entry in stat.get(side) or []:
                    player_id = _i(entry.get("element"))
                    if player_id is None:
                        continue
                    rows.append({
                        "fixture": fixture_id,
                        "identifier": identifier,
                        "side": side,
                        "player_id": player_id,
                        "value": _i(entry.get("value"), 0),
                    })

    if started:
        marks = ",".join("?" for _ in started)
        conn.execute(f"DELETE FROM fixture_stats WHERE fixture IN ({marks})", started)
    upsert_many(conn, "fixture_stats", rows)
    return len(rows)


HISTORY_INT_FIELDS = (
    "round fixture opponent_team minutes starts total_points goals_scored assists "
    "clean_sheets goals_conceded saves bonus bps yellow_cards red_cards own_goals "
    "penalties_missed penalties_saved defensive_contribution "
    "clearances_blocks_interceptions tackles recoveries value"
).split()

HISTORY_FLOAT_FIELDS = (
    "expected_goals expected_assists expected_goal_involvements expected_goals_conceded"
).split()


def sync_element_summaries(
    conn: sqlite3.Connection, client: FPLClient, player_ids: Iterable[int]
) -> int:
    """Fetch per-player match history for the given players (already prioritised)."""
    total = 0
    for pid in player_ids:
        try:
            data = client.element_summary(int(pid))
        except FPLApiError as exc:
            log.warning("element-summary %s failed: %s", pid, exc)
            log_fetch(conn, "element_summary", str(pid), False, str(exc))
            continue
        if not data:
            continue
        rows = []
        for h in data.get("history", []):
            row: dict[str, Any] = {k: _i(h.get(k), 0) for k in HISTORY_INT_FIELDS}
            row.update({k: _f(h.get(k), 0.0) for k in HISTORY_FLOAT_FIELDS})
            row["player_id"] = int(pid)
            row["was_home"] = _b(h.get("was_home"))
            row["kickoff_time"] = h.get("kickoff_time")
            rows.append(row)
        if rows:
            upsert_many(conn, "player_gw_history", rows)
        log_fetch(conn, "element_summary", str(pid), True, f"{len(rows)} rows")
        total += len(rows)
        conn.commit()
    log.info("element summaries synced: %d history rows", total)
    return total


def _league_phase_total(league: dict) -> int | None:
    """Points scored while in this league, taken from its overall phase.

    Not the same as the season total for a league that started after GW1, which
    is why it is read per league instead of copied off the entry. Phase 1 is the
    whole season; the rest are individual months.
    """
    phases = league.get("active_phases") or []
    for phase in phases:
        if _i(phase.get("phase")) == 1:
            return _i(phase.get("total"))
    return _i(phases[0].get("total")) if phases else None


def sync_entry(conn: sqlite3.Connection, client: FPLClient, team_id: int) -> dict | None:
    """Entry metadata + classic leagues joined."""
    data = client.entry(int(team_id))
    if not data:
        log_fetch(conn, "entry", str(team_id), False, "not found")
        log.warning("entry %s not found (check team_ids in config.yaml)", team_id)
        return None
    now = utcnow()
    upsert_many(
        conn,
        "my_entries",
        [
            {
                "team_id": _i(data.get("id")),
                "name": data.get("name"),
                "player_name": " ".join(
                    x for x in [data.get("player_first_name"), data.get("player_last_name")] if x
                ),
                "summary_overall_points": _i(data.get("summary_overall_points")),
                "summary_overall_rank": _i(data.get("summary_overall_rank")),
                "summary_event_points": _i(data.get("summary_event_points")),
                "summary_event_rank": _i(data.get("summary_event_rank")),
                "current_event": _i(data.get("current_event")),
                "bank": _i(data.get("last_deadline_bank")),
                "value": _i(data.get("last_deadline_value")),
                "total_transfers": _i(data.get("last_deadline_total_transfers")),
                "last_deadline_bank": _i(data.get("last_deadline_bank")),
                "last_deadline_value": _i(data.get("last_deadline_value")),
                "raw_json": jdump(data),
                "fetched_at": now,
            }
        ],
    )

    leagues = (data.get("leagues") or {}).get("classic", []) or []
    league_rows = [
        {
            "team_id": int(team_id),
            "league_id": _i(l.get("id")),
            "name": l.get("name"),
            "league_type": l.get("league_type"),
            "scoring": l.get("scoring"),
            "entry_rank": _i(l.get("entry_rank")),
            "entry_last_rank": _i(l.get("entry_last_rank")),
            "created": l.get("created"),
            # This response already carries our standing in every league,
            # public ones included, so nothing else needs to be fetched for it.
            "rank_count": _i(l.get("rank_count")),
            "entry_percentile_rank": _i(l.get("entry_percentile_rank")),
            "entry_total": _league_phase_total(l),
            "fetched_at": now,
        }
        for l in leagues
    ]
    if league_rows:
        upsert_many(conn, "my_leagues", league_rows)
    log_fetch(conn, "entry", str(team_id), True, f"{len(league_rows)} classic leagues")
    log.info("entry %s synced (%d classic leagues)", team_id, len(league_rows))
    return data


def sync_entry_history(conn: sqlite3.Connection, client: FPLClient, team_id: int) -> int:
    data = client.entry_history(int(team_id))
    if not data:
        log_fetch(conn, "entry_history", str(team_id), False, "not found")
        return 0
    rows = [
        {
            "team_id": int(team_id),
            "event": _i(c.get("event")),
            "points": _i(c.get("points")),
            "total_points": _i(c.get("total_points")),
            "rank": _i(c.get("rank")),
            "overall_rank": _i(c.get("overall_rank")),
            "bank": _i(c.get("bank")),
            "value": _i(c.get("value")),
            "event_transfers": _i(c.get("event_transfers")),
            "event_transfers_cost": _i(c.get("event_transfers_cost")),
            "points_on_bench": _i(c.get("points_on_bench")),
        }
        for c in data.get("current", [])
    ]
    if rows:
        upsert_many(conn, "my_entry_gw", rows)
    chips = [
        {"team_id": int(team_id), "name": c.get("name"), "event": _i(c.get("event"))}
        for c in data.get("chips", [])
    ]
    if chips:
        upsert_many(conn, "my_chips", chips)
    log_fetch(conn, "entry_history", str(team_id), True, f"{len(rows)} gameweeks")
    return len(rows)


def sync_picks(conn: sqlite3.Connection, client: FPLClient, team_id: int, gw: int) -> int:
    data = client.entry_picks(int(team_id), int(gw))
    if not data:
        # Normal before a team's first deadline, or for a GW not yet entered.
        log_fetch(conn, "picks", f"{team_id}:{gw}", False, "no picks available")
        return 0
    rows = [
        {
            "team_id": int(team_id),
            "event": int(gw),
            "player_id": _i(p.get("element")),
            "position": _i(p.get("position")),
            "multiplier": _i(p.get("multiplier")),
            "is_captain": _b(p.get("is_captain")),
            "is_vice_captain": _b(p.get("is_vice_captain")),
            "selling_price": _i(p.get("selling_price")),
            "purchase_price": _i(p.get("purchase_price")),
        }
        for p in data.get("picks", [])
    ]
    if rows:
        conn.execute(
            "DELETE FROM my_picks WHERE team_id=? AND event=?", (int(team_id), int(gw))
        )
        upsert_many(conn, "my_picks", rows)
    log_fetch(conn, "picks", f"{team_id}:{gw}", True, f"{len(rows)} picks")
    return len(rows)
