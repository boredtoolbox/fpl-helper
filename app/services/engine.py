"""The decision engine: expected points per player per gameweek.

Pure Python and pure arithmetic — no AI anywhere in this module. The crowd
layer (services/crowd_intel.py) produces bounded *adjustments* that get applied
here; it never picks anything.

Scoring rules encoded below are the 2025/26 rules carried into 2026/27,
including the defensive-contribution ("DefCon") point:
  * DEF  -> 2 pts at >= 10 (clearances + blocks + interceptions + tackles)
  * MID/FWD -> 2 pts at >= 12 (the above + recoveries)
"""
from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..db import jdump, set_meta, upsert_many, utcnow

log = logging.getLogger(__name__)

GK, DEF, MID, FWD = 1, 2, 3, 4
POSITION_NAMES = {GK: "GKP", DEF: "DEF", MID: "MID", FWD: "FWD"}

GOAL_POINTS = {GK: 6, DEF: 6, MID: 5, FWD: 4}
ASSIST_POINTS = 3
CLEAN_SHEET_POINTS = {GK: 4, DEF: 4, MID: 1, FWD: 0}
DEFCON_THRESHOLD = {DEF: 10, MID: 12, FWD: 12}
DEFCON_POINTS = 2
SAVES_PER_POINT = 3
YELLOW_POINTS = -1
RED_POINTS = -3

# Fallback per-match points spread when a player has too little history, used
# for the captaincy ceiling estimate.
DEFAULT_STD = {GK: 2.0, DEF: 2.6, MID: 3.0, FWD: 3.2}

# Priors for newly promoted sides with no top-flight history in the dataset.
PROMOTED_ATTACK = 0.82
PROMOTED_DEFENCE = 1.18
MIN_TEAM_MATCHES = 5

HOME_GOALS_FACTOR = 1.10
AWAY_GOALS_FACTOR = 0.92

HORIZON_DISCOUNT = 0.9


# --------------------------------------------------------------------------
# Team strength
# --------------------------------------------------------------------------
@dataclass
class TeamStrength:
    team_id: int
    name: str
    attack: float = 1.0        # xG scored per match / league average
    defence: float = 1.0       # xG conceded per match / league average (>1 = leaky)
    matches: int = 0
    xgf_per_match: float = 0.0
    xga_per_match: float = 0.0
    promoted_prior: bool = False


def _season_team_totals(conn: sqlite3.Connection, table: str, where: str, params: Sequence[Any]) -> dict[str, list[tuple[float, float]]]:
    """team name -> [(xg_for, xg_against), ...] one entry per fixture."""
    sql = f"""
        SELECT team_name, fixture,
               SUM(expected_goals) AS xgf,
               MAX(expected_goals_conceded) AS xga
        FROM {table}
        WHERE {where} AND minutes > 0
        GROUP BY team_name, fixture
    """
    out: dict[str, list[tuple[float, float]]] = {}
    for row in conn.execute(sql, params):
        name = row["team_name"]
        if not name:
            continue
        out.setdefault(name, []).append((row["xgf"] or 0.0, row["xga"] or 0.0))
    return out


def _current_team_totals(conn: sqlite3.Connection) -> dict[str, list[tuple[float, float]]]:
    """Same shape, but from this season's player_gw_history joined to teams."""
    sql = """
        SELECT t.name AS team_name, h.fixture,
               SUM(h.expected_goals) AS xgf,
               MAX(h.expected_goals_conceded) AS xga
        FROM player_gw_history h
        JOIN players p ON p.id = h.player_id
        JOIN teams   t ON t.id = p.team
        WHERE h.minutes > 0
        GROUP BY t.name, h.fixture
    """
    out: dict[str, list[tuple[float, float]]] = {}
    for row in conn.execute(sql):
        out.setdefault(row["team_name"], []).append((row["xgf"] or 0.0, row["xga"] or 0.0))
    return out


def compute_team_strength(
    conn: sqlite3.Connection, seasons: Sequence[str], current_weight: float | None = None
) -> dict[int, TeamStrength]:
    """Blend this season's and past seasons' xG rates into per-team ratings.

    `current_weight` shifts from 0 (season not started — lean entirely on
    history) toward 1 as gameweeks accumulate.
    """
    teams = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams")}
    if current_weight is None:
        current_weight = season_current_weight(conn)

    hist_recent = _season_team_totals(
        conn, "historical_player_gw", "season = ?", (seasons[0],)
    ) if seasons else {}
    hist_all = _season_team_totals(
        conn,
        "historical_player_gw",
        f"season IN ({','.join('?' for _ in seasons)})",
        tuple(seasons),
    ) if seasons else {}
    current = _current_team_totals(conn)

    def blended(name: str) -> tuple[float, float, int]:
        """Return (xgf/match, xga/match, matches) for one team."""
        cur = current.get(name, [])
        # Prefer the most recent completed season; widen if that's thin.
        hist = hist_recent.get(name) or hist_all.get(name) or []
        w = current_weight if cur else 0.0
        parts: list[tuple[float, float, float]] = []  # (weight, xgf, xga)
        if cur:
            parts.append((w, sum(x for x, _ in cur) / len(cur), sum(y for _, y in cur) / len(cur)))
        if hist:
            parts.append(
                (1.0 - w, sum(x for x, _ in hist) / len(hist), sum(y for _, y in hist) / len(hist))
            )
        if not parts:
            return 0.0, 0.0, 0
        total_w = sum(p[0] for p in parts) or 1.0
        xgf = sum(p[0] * p[1] for p in parts) / total_w
        xga = sum(p[0] * p[2] for p in parts) / total_w
        return xgf, xga, len(cur) + len(hist)

    raw: dict[int, tuple[float, float, int]] = {
        tid: blended(name) for tid, name in teams.items()
    }
    known = [v for v in raw.values() if v[2] >= MIN_TEAM_MATCHES]
    league_xgf = statistics.fmean([v[0] for v in known]) if known else 1.4
    league_xga = statistics.fmean([v[1] for v in known]) if known else 1.4
    league_xgf = max(league_xgf, 0.2)
    league_xga = max(league_xga, 0.2)

    out: dict[int, TeamStrength] = {}
    for tid, name in teams.items():
        xgf, xga, matches = raw[tid]
        if matches < MIN_TEAM_MATCHES:
            # Promoted / unknown side: sit them below average rather than at it.
            out[tid] = TeamStrength(
                tid, name, PROMOTED_ATTACK, PROMOTED_DEFENCE, matches,
                league_xgf * PROMOTED_ATTACK, league_xga * PROMOTED_DEFENCE,
                promoted_prior=True,
            )
            continue
        out[tid] = TeamStrength(
            team_id=tid,
            name=name,
            attack=_clamp(xgf / league_xgf, 0.5, 1.8),
            defence=_clamp(xga / league_xga, 0.5, 1.8),
            matches=matches,
            xgf_per_match=xgf,
            xga_per_match=xga,
        )
    out["_league_xgf"] = league_xgf  # type: ignore[index]
    out["_league_xga"] = league_xga  # type: ignore[index]
    return out


def season_current_weight(conn: sqlite3.Connection, full_at: int = 10) -> float:
    """0.0 before a ball is kicked, ramping to 1.0 after `full_at` gameweeks."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM gameweeks WHERE finished = 1"
    ).fetchone()
    played = row["n"] if row else 0
    return _clamp(played / float(full_at), 0.0, 1.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Player rates
# --------------------------------------------------------------------------
@dataclass
class PlayerRates:
    player_id: int
    position: int
    team: int
    xg90: float = 0.0
    xa90: float = 0.0
    defcon90: float = 0.0
    defcon_hit_rate: float = 0.0     # share of 60+ min appearances hitting the threshold
    saves90: float = 0.0
    bonus90: float = 0.0
    yellow90: float = 0.0
    red90: float = 0.0
    minutes_per_appearance: float = 0.0
    start_rate: float = 0.0          # recent starts / recent matchdays
    appearance_rate: float = 0.0
    sample_matches: int = 0
    recent_matches: int = 0
    points_std: float = 0.0
    goal_conversion: float = 1.0     # goals / xG, damped — the "finishing" nudge


def _rate_rows(conn: sqlite3.Connection, seasons: Sequence[str]) -> dict[int, dict[str, Any]]:
    """Aggregate per-90 rates from past seasons, keyed by current player id."""
    if not seasons:
        return {}
    placeholders = ",".join("?" for _ in seasons)
    sql = f"""
        SELECT p.id AS player_id,
               SUM(h.minutes)                AS minutes,
               COUNT(*)                      AS matches,
               SUM(CASE WHEN h.minutes > 0 THEN 1 ELSE 0 END) AS appearances,
               SUM(h.starts)                 AS starts,
               SUM(h.expected_goals)         AS xg,
               SUM(h.expected_assists)       AS xa,
               SUM(h.goals_scored)           AS goals,
               SUM(h.defensive_contribution) AS defcon,
               SUM(h.saves)                  AS saves,
               SUM(h.bonus)                  AS bonus,
               SUM(h.yellow_cards)           AS yellows,
               SUM(h.red_cards)              AS reds
        FROM historical_player_gw h
        JOIN players p ON p.code = h.player_code
        WHERE h.season IN ({placeholders})
        GROUP BY p.id
    """
    return {r["player_id"]: dict(r) for r in conn.execute(sql, tuple(seasons))}


def _current_rate_rows(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    sql = """
        SELECT player_id,
               SUM(minutes)                AS minutes,
               COUNT(*)                    AS matches,
               SUM(CASE WHEN minutes > 0 THEN 1 ELSE 0 END) AS appearances,
               SUM(starts)                 AS starts,
               SUM(expected_goals)         AS xg,
               SUM(expected_assists)       AS xa,
               SUM(goals_scored)           AS goals,
               SUM(defensive_contribution) AS defcon,
               SUM(saves)                  AS saves,
               SUM(bonus)                  AS bonus,
               SUM(yellow_cards)           AS yellows,
               SUM(red_cards)              AS reds
        FROM player_gw_history
        GROUP BY player_id
    """
    return {r["player_id"]: dict(r) for r in conn.execute(sql)}


def seasons_with_defcon(conn: sqlite3.Connection, seasons: Sequence[str]) -> list[str]:
    """Seasons whose rows carry defensive-contribution data.

    The DefCon stat was introduced for 2025/26; earlier seasons store zeros.
    Averaging across them would halve every rate, so they are excluded from all
    DefCon maths (but still used for goals, assists, minutes and so on).
    """
    out: list[str] = []
    for season in seasons:
        row = conn.execute(
            "SELECT SUM(defensive_contribution) AS total FROM historical_player_gw WHERE season = ?",
            (season,),
        ).fetchone()
        if row and (row["total"] or 0) > 0:
            out.append(season)
    return out


def _defcon_rate_rows(
    conn: sqlite3.Connection, seasons: Sequence[str]
) -> dict[int, tuple[float, float]]:
    """player id -> (defensive contributions, minutes) over DefCon-era seasons."""
    if not seasons:
        return {}
    placeholders = ",".join("?" for _ in seasons)
    sql = f"""
        SELECT p.id AS player_id,
               SUM(h.defensive_contribution) AS dc,
               SUM(h.minutes)                AS minutes
        FROM historical_player_gw h
        JOIN players p ON p.code = h.player_code
        WHERE h.season IN ({placeholders})
        GROUP BY p.id
    """
    return {
        r["player_id"]: (r["dc"] or 0.0, r["minutes"] or 0.0)
        for r in conn.execute(sql, tuple(seasons))
    }


def defcon_hit_rates(conn: sqlite3.Connection, seasons: Sequence[str]) -> dict[int, tuple[int, int]]:
    """player id -> (60+ min appearances, of which hit the DefCon threshold).

    Counts every 60-minute appearance in the given seasons, including games
    with no defensive actions at all — filtering those out would inflate the
    rate. Pass only seasons that actually recorded the stat.
    """
    out: dict[int, tuple[int, int]] = {}
    if seasons:
        placeholders = ",".join("?" for _ in seasons)
        sql = f"""
            SELECT p.id AS player_id, p.element_type AS pos,
                   COUNT(*) AS n,
                   SUM(CASE WHEN h.defensive_contribution >=
                        CASE WHEN p.element_type = 2 THEN 10 ELSE 12 END
                       THEN 1 ELSE 0 END) AS hits
            FROM historical_player_gw h
            JOIN players p ON p.code = h.player_code
            WHERE h.season IN ({placeholders}) AND h.minutes >= 60
            GROUP BY p.id
        """
        for r in conn.execute(sql, tuple(seasons)):
            out[r["player_id"]] = (r["n"] or 0, r["hits"] or 0)
    sql_cur = """
        SELECT h.player_id AS player_id, COUNT(*) AS n,
               SUM(CASE WHEN h.defensive_contribution >=
                    CASE WHEN p.element_type = 2 THEN 10 ELSE 12 END
                   THEN 1 ELSE 0 END) AS hits
        FROM player_gw_history h
        JOIN players p ON p.id = h.player_id
        WHERE h.minutes >= 60
        GROUP BY h.player_id
    """
    for r in conn.execute(sql_cur):
        n, hits = out.get(r["player_id"], (0, 0))
        out[r["player_id"]] = (n + (r["n"] or 0), hits + (r["hits"] or 0))
    return out


def _points_spread(conn: sqlite3.Connection, seasons: Sequence[str]) -> dict[int, float]:
    """Std dev of per-appearance points, for the captaincy ceiling."""
    series: dict[int, list[float]] = {}
    if seasons:
        placeholders = ",".join("?" for _ in seasons)
        sql = f"""
            SELECT p.id AS player_id, h.total_points AS pts
            FROM historical_player_gw h
            JOIN players p ON p.code = h.player_code
            WHERE h.season IN ({placeholders}) AND h.minutes > 0
        """
        for r in conn.execute(sql, tuple(seasons)):
            series.setdefault(r["player_id"], []).append(float(r["pts"] or 0))
    for r in conn.execute(
        "SELECT player_id, total_points AS pts FROM player_gw_history WHERE minutes > 0"
    ):
        series.setdefault(r["player_id"], []).append(float(r["pts"] or 0))
    return {
        pid: statistics.pstdev(vals)
        for pid, vals in series.items()
        if len(vals) >= 4
    }


def _recent_starts(conn: sqlite3.Connection, lookback: int = 5) -> dict[int, tuple[int, int]]:
    """player id -> (matchdays in window, starts) using this season's history."""
    row = conn.execute("SELECT MAX(round) AS mx FROM player_gw_history").fetchone()
    max_round = (row["mx"] if row else None) or 0
    if not max_round:
        return {}
    low = max(1, max_round - lookback + 1)
    out: dict[int, tuple[int, int]] = {}
    for r in conn.execute(
        "SELECT player_id, COUNT(*) AS n, SUM(starts) AS s "
        "FROM player_gw_history WHERE round >= ? GROUP BY player_id",
        (low,),
    ):
        out[r["player_id"]] = (r["n"] or 0, r["s"] or 0)
    return out


def _per90(total: float | None, minutes: float | None) -> float:
    minutes = minutes or 0.0
    if minutes <= 0:
        return 0.0
    return (total or 0.0) * 90.0 / minutes


def compute_player_rates(
    conn: sqlite3.Connection, seasons: Sequence[str], current_weight: float | None = None
) -> dict[int, PlayerRates]:
    """Blend historical and current per-90 rates for every player."""
    if current_weight is None:
        current_weight = season_current_weight(conn)

    hist = _rate_rows(conn, seasons)
    cur = _current_rate_rows(conn)
    dc_seasons = seasons_with_defcon(conn, seasons)
    dc_hist = _defcon_rate_rows(conn, dc_seasons)
    defcon = defcon_hit_rates(conn, dc_seasons)
    spread = _points_spread(conn, seasons)
    recent = _recent_starts(conn)

    out: dict[int, PlayerRates] = {}
    for row in conn.execute(
        "SELECT id, element_type, team, minutes, starts, status, "
        "chance_of_playing_next_round, expected_goals_per_90, "
        "expected_assists_per_90, defensive_contribution_per_90, saves_per_90, "
        "starts_per_90 FROM players"
    ):
        pid = row["id"]
        pos = row["element_type"] or MID
        h = hist.get(pid)
        c = cur.get(pid)

        def blend(field: str) -> float:
            """Weighted blend of a per-90 rate across the two sources."""
            hv = _per90(h[field], h["minutes"]) if h else None
            cv = _per90(c[field], c["minutes"]) if c else None
            if hv is None and cv is None:
                return 0.0
            if hv is None:
                return cv or 0.0
            if cv is None:
                return hv
            w = current_weight
            return w * cv + (1.0 - w) * hv

        hist_minutes = (h or {}).get("minutes") or 0
        cur_minutes = (c or {}).get("minutes") or 0
        total_matches = ((h or {}).get("matches") or 0) + ((c or {}).get("matches") or 0)
        appearances = ((h or {}).get("appearances") or 0) + ((c or {}).get("appearances") or 0)
        starts = ((h or {}).get("starts") or 0) + ((c or {}).get("starts") or 0)

        # Fall back to bootstrap season aggregates when there's no match-level data
        # (e.g. a new signing whose only trace is the carried-over season totals).
        xg90 = blend("xg") or (row["expected_goals_per_90"] or 0.0)
        xa90 = blend("xa") or (row["expected_assists_per_90"] or 0.0)
        sv90 = blend("saves") or (row["saves_per_90"] or 0.0)

        # DefCon uses only seasons that actually recorded the stat.
        dc_total, dc_minutes = dc_hist.get(pid, (0.0, 0.0))
        dc_hist90 = _per90(dc_total, dc_minutes)
        dc_cur90 = _per90(c["defcon"], c["minutes"]) if c else None
        if dc_cur90 and dc_hist90:
            dc90 = current_weight * dc_cur90 + (1.0 - current_weight) * dc_hist90
        else:
            dc90 = dc_cur90 or dc_hist90 or (row["defensive_contribution_per_90"] or 0.0)

        n60, hits = defcon.get(pid, (0, 0))
        if n60 >= 3:
            hit_rate = hits / n60
        elif dc90 > 0 and pos in DEFCON_THRESHOLD:
            # No per-match sample: approximate from the rate with a Poisson tail.
            hit_rate = _poisson_at_least(DEFCON_THRESHOLD[pos], dc90)
        else:
            hit_rate = 0.0

        rec_n, rec_s = recent.get(pid, (0, 0))
        if rec_n >= 2:
            start_rate = rec_s / rec_n
        elif total_matches:
            # starts/matches counts past injury absence as rotation risk. For a
            # player who is fit *today* that double-counts, so lean toward the
            # role rate (how often they start when available) instead.
            availability_rate = starts / total_matches
            role_rate = starts / appearances if appearances else availability_rate
            currently_fit = (
                (row["status"] or "a") == "a"
                and row["chance_of_playing_next_round"] in (None, 100)
            )
            start_rate = max(availability_rate, 0.9 * role_rate) if currently_fit else availability_rate
        else:
            start_rate = _clamp((row["starts_per_90"] or 0.0), 0.0, 1.0)

        goals = ((h or {}).get("goals") or 0) + ((c or {}).get("goals") or 0)
        total_xg = ((h or {}).get("xg") or 0.0) + ((c or {}).get("xg") or 0.0)
        # Damp the finishing multiplier hard — goals/xG is very noisy.
        if total_xg >= 5.0:
            conversion = _clamp(0.5 + 0.5 * (goals / total_xg), 0.85, 1.15)
        else:
            conversion = 1.0

        out[pid] = PlayerRates(
            player_id=pid,
            position=pos,
            team=row["team"],
            xg90=xg90,
            xa90=xa90,
            defcon90=dc90,
            defcon_hit_rate=_clamp(hit_rate, 0.0, 0.98),
            saves90=sv90,
            bonus90=blend("bonus"),
            yellow90=blend("yellows"),
            red90=blend("reds"),
            minutes_per_appearance=(
                (hist_minutes + cur_minutes) / appearances if appearances else 0.0
            ),
            start_rate=_clamp(start_rate, 0.0, 1.0),
            appearance_rate=_clamp(appearances / total_matches, 0.0, 1.0) if total_matches else 0.0,
            sample_matches=total_matches,
            recent_matches=rec_n,
            points_std=spread.get(pid, DEFAULT_STD.get(pos, 2.8)),
            goal_conversion=conversion,
        )
    return out


def _poisson_at_least(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 0.0
    cumulative = 0.0
    term = math.exp(-lam)
    for i in range(k):
        cumulative += term
        term *= lam / (i + 1)
    return _clamp(1.0 - cumulative, 0.0, 1.0)


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
@dataclass
class Availability:
    """Two distinct probabilities, which must not be conflated.

    `p_available` is the chance the player is fit and in the matchday squad at
    all; `p_start` is the chance they start. An injured player has both at zero,
    while a fit squad rotation option can have p_available near 1 and p_start
    low — the difference is what drives cameo minutes.
    """

    p_start: float
    reasons: list[str] = field(default_factory=list)
    source: str = "model"
    p_available: float = 1.0

    def __post_init__(self) -> None:
        # A player can never be more likely to start than to be available.
        self.p_available = max(self.p_available, self.p_start)


def base_availability(player_row: Any, rates: PlayerRates) -> Availability:
    """Start probability from official flags + recent starts, before crowd intel."""
    reasons: list[str] = []
    status = (player_row["status"] or "a").lower()
    chance = player_row["chance_of_playing_next_round"]
    p = rates.start_rate

    if status in ("i", "s", "u", "n"):
        label = {
            "i": "injured", "s": "suspended",
            "u": "unavailable", "n": "on loan / not in squad",
        }[status]
        news = (player_row["news"] or "").strip()
        reasons.append(f"Official FPL status: {label}" + (f" — {news}" if news else ""))
        return Availability(0.0, reasons, "official", p_available=0.0)

    available = 1.0
    if chance is not None:
        official = _clamp(chance / 100.0, 0.0, 1.0)
        if official < 1.0:
            news = (player_row["news"] or "").strip()
            reasons.append(
                f"Official FPL: {chance}% chance of playing"
                + (f" — {news}" if news else "")
            )
            p = min(p, official)
            available = official

    if rates.sample_matches == 0:
        # Unknown quantity (new signing, no data): assume a rotation risk.
        p = min(p if p > 0 else 0.5, 0.5)
        reasons.append("No match history available — start probability capped at 50%")

    return Availability(_clamp(p, 0.0, 1.0), reasons, p_available=_clamp(available, 0.0, 1.0))


# --------------------------------------------------------------------------
# Per-fixture projection
# --------------------------------------------------------------------------
@dataclass
class Projection:
    player_id: int
    event: int
    fixture: int
    opponent_team: int
    was_home: bool
    difficulty: int
    xpts: float
    xpts_raw: float
    p_start: float
    expected_minutes: float
    components: dict[str, float]
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    std_dev: float = 0.0


def expected_goals_in_match(
    attacking: TeamStrength, defending: TeamStrength, is_home: bool, league_xg: float
) -> float:
    """Poisson mean goals for `attacking` against `defending`."""
    factor = HOME_GOALS_FACTOR if is_home else AWAY_GOALS_FACTOR
    return max(0.15, league_xg * attacking.attack * defending.defence * factor)


def project_fixture(
    rates: PlayerRates,
    availability: Availability,
    team: TeamStrength,
    opponent: TeamStrength,
    is_home: bool,
    league_xg: float,
    h2h_multiplier: float = 1.0,
) -> tuple[float, dict[str, float], float]:
    """Return (xPts, component breakdown, expected minutes) for one fixture."""
    pos = rates.position
    p_start = availability.p_start

    # Minutes: starters usually go the distance; cameos are short.
    typical = rates.minutes_per_appearance or (75.0 if p_start > 0.5 else 20.0)
    start_minutes = _clamp(typical if typical > 45 else 80.0, 45.0, 90.0)
    # Cameo minutes are only possible in the gap between "available" and
    # "starting" — an unavailable player gets none.
    p_cameo = max(0.0, availability.p_available - p_start) * _clamp(
        rates.appearance_rate, 0.0, 0.6
    )
    expected_minutes = p_start * start_minutes + p_cameo * 18.0
    # P(reaching 60 minutes) — drives appearance points and clean sheets.
    p60 = p_start * _clamp((start_minutes - 25.0) / 65.0, 0.0, 1.0)
    p_any = _clamp(p_start + p_cameo, 0.0, 1.0)

    # Opponent-adjusted attacking output.
    team_goals = expected_goals_in_match(team, opponent, is_home, league_xg)
    opp_goals = expected_goals_in_match(opponent, team, not is_home, league_xg)
    baseline = max(league_xg, 0.2)
    attack_scale = team_goals / baseline

    minutes_share = expected_minutes / 90.0
    xg = rates.xg90 * minutes_share * attack_scale * rates.goal_conversion * h2h_multiplier
    xa = rates.xa90 * minutes_share * attack_scale * h2h_multiplier

    c: dict[str, float] = {}
    c["appearance"] = p60 * 2.0 + (p_any - p60) * 1.0
    c["goals"] = xg * GOAL_POINTS.get(pos, 4)
    c["assists"] = xa * ASSIST_POINTS

    p_cs = math.exp(-opp_goals)
    c["clean_sheet"] = p_cs * CLEAN_SHEET_POINTS.get(pos, 0) * p60

    if pos in DEFCON_THRESHOLD:
        c["defcon"] = rates.defcon_hit_rate * DEFCON_POINTS * p60
    else:
        c["defcon"] = 0.0

    if pos == GK:
        c["saves"] = (rates.saves90 * minutes_share) / SAVES_PER_POINT
    else:
        c["saves"] = 0.0

    c["bonus"] = rates.bonus90 * minutes_share

    negatives = 0.0
    if pos in (GK, DEF):
        # -1 per 2 goals conceded, expectation over a Poisson(opp_goals) count.
        negatives -= _expected_concede_penalty(opp_goals) * p60
    negatives += rates.yellow90 * minutes_share * YELLOW_POINTS
    negatives += rates.red90 * minutes_share * RED_POINTS
    c["negatives"] = negatives

    xpts = sum(c.values())
    return max(0.0, xpts), c, expected_minutes


def _expected_concede_penalty(lam: float, cap: int = 9) -> float:
    """E[floor(goals_conceded / 2)] for goals ~ Poisson(lam)."""
    if lam <= 0:
        return 0.0
    total = 0.0
    term = math.exp(-lam)
    for k in range(cap + 1):
        total += (k // 2) * term
        term *= lam / (k + 1)
    return total


def head_to_head_multiplier(
    matches: int | None, avg_points: float | None, position_avg: float
) -> float:
    """Small, capped nudge from the 2-season head-to-head record.

    Deliberately weak (±5%): the samples are 3-8 matches and mostly noise, but
    it is one of the things a human manager actually looks at.
    """
    if not matches or matches < 3 or avg_points is None or position_avg <= 0:
        return 1.0
    ratio = avg_points / position_avg
    # Shrink toward 1 by sample size, then clamp.
    weight = min(matches / 8.0, 1.0) * 0.25
    return _clamp(1.0 + (ratio - 1.0) * weight, 0.95, 1.05)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def upcoming_fixtures(conn: sqlite3.Connection, horizon: int) -> list[dict[str, Any]]:
    """Fixtures for the next `horizon` gameweeks that still have football in them.

    The window is taken from the fixtures rather than from `gameweeks.finished`,
    because those two disagree for most of a week. FPL leaves a gameweek flagged
    current — and therefore unfinished — until bonus points and data checks land,
    which is days after its last whistle. Anchoring on that spent its first slot
    on a gameweek with nothing left to play, so a six-gameweek horizon quietly
    projected five and every ticker carried a permanent empty cell.

    Selecting distinct events also steps over a gameweek with no fixtures at all
    rather than counting it against the horizon.
    """
    events = [
        r["event"] for r in conn.execute(
            "SELECT DISTINCT event FROM fixtures "
            "WHERE finished = 0 AND event IS NOT NULL ORDER BY event LIMIT ?",
            (horizon,),
        )
    ]
    if not events:
        return []
    placeholders = ",".join("?" for _ in events)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM fixtures WHERE event IN ({placeholders}) "
            "AND finished = 0 ORDER BY event, kickoff_time",
            events,
        )
    ]


def _position_averages(conn: sqlite3.Connection, seasons: Sequence[str]) -> dict[int, float]:
    """Average points per appearance by position — the h2h comparison baseline."""
    out = {GK: 3.4, DEF: 3.3, MID: 3.3, FWD: 3.4}
    if not seasons:
        return out
    placeholders = ",".join("?" for _ in seasons)
    sql = f"""
        SELECT p.element_type AS pos, AVG(h.total_points) AS avg_pts
        FROM historical_player_gw h
        JOIN players p ON p.code = h.player_code
        WHERE h.season IN ({placeholders}) AND h.minutes >= 45
        GROUP BY p.element_type
    """
    for r in conn.execute(sql, tuple(seasons)):
        if r["pos"] and r["avg_pts"]:
            out[r["pos"]] = float(r["avg_pts"])
    return out


def archive_projections(conn: sqlite3.Connection, now: str) -> int:
    """Keep what we projected for gameweeks that have not kicked off yet.

    Called just before the rebuild wipes the table. Only untouched gameweeks are
    written, so a row stops being updated the moment its first fixture starts —
    what survives is the projection that stood at the deadline, rather than one
    revised halfway through with results already in it.

    Doubles up as the reason a projection can be compared with a score at all:
    nothing else in the database remembers it.
    """
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO projection_history (event, player_id, xpts, captured_at)
        SELECT pr.event, pr.player_id, SUM(pr.xpts), ?
        FROM projections pr
        WHERE pr.event NOT IN (
            SELECT event FROM fixtures
            WHERE event IS NOT NULL AND (started = 1 OR finished = 1)
        )
        GROUP BY pr.event, pr.player_id
        """,
        (now,),
    )
    return cur.rowcount


def build_projections(
    conn: sqlite3.Connection,
    seasons: Sequence[str],
    horizon: int = 6,
    crowd_adjustments: dict[int, Any] | None = None,
) -> int:
    """Compute and store xPts for every player across the horizon."""
    from .crowd_intel import apply_crowd_adjustment, sentiment_multiplier

    current_weight = season_current_weight(conn)
    strengths = compute_team_strength(conn, seasons, current_weight)
    league_xg = strengths.pop("_league_xgf")  # type: ignore[arg-type]
    strengths.pop("_league_xga", None)  # type: ignore[arg-type]
    rates = compute_player_rates(conn, seasons, current_weight)
    fixtures = upcoming_fixtures(conn, horizon)
    pos_avg = _position_averages(conn, seasons)
    crowd_adjustments = crowd_adjustments or {}

    players = {r["id"]: r for r in conn.execute("SELECT * FROM players")}
    team_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams")}

    h2h: dict[tuple[int, str], sqlite3.Row] = {}
    for r in conn.execute("SELECT * FROM head_to_head WHERE matches >= 3"):
        h2h[(r["player_code"], r["opponent_team_name"])] = r

    by_team: dict[int, list[dict[str, Any]]] = {}
    for f in fixtures:
        by_team.setdefault(f["team_h"], []).append({**f, "is_home": True})
        by_team.setdefault(f["team_a"], []).append({**f, "is_home": False})

    rows: list[dict[str, Any]] = []
    now = utcnow()
    archive_projections(conn, now)
    conn.execute("DELETE FROM projections")

    for pid, player in players.items():
        rate = rates.get(pid)
        if rate is None or not player["team"]:
            continue
        team_strength = strengths.get(player["team"])
        if team_strength is None:
            continue
        crowd = crowd_adjustments.get(pid)
        raw_avail = base_availability(player, rate)
        avail = apply_crowd_adjustment(raw_avail, crowd, player)
        sentiment_mult, sentiment_reason = sentiment_multiplier(crowd)

        for fixture in by_team.get(player["team"], []):
            is_home = fixture["is_home"]
            opp_id = fixture["team_a"] if is_home else fixture["team_h"]
            opp = strengths.get(opp_id)
            if opp is None:
                continue
            opp_name = team_names.get(opp_id, "")
            record = h2h.get((player["code"], opp_name))
            multiplier = head_to_head_multiplier(
                record["matches"] if record else None,
                record["avg_points"] if record else None,
                pos_avg.get(rate.position, 3.3),
            )
            xpts, components, exp_minutes = project_fixture(
                rate, avail, team_strength, opp, is_home, league_xg, multiplier
            )
            if sentiment_mult != 1.0:
                xpts *= sentiment_mult
                components = {k: v * sentiment_mult for k, v in components.items()}

            adjustments = [{"kind": "availability", "reason": r} for r in avail.reasons]
            if sentiment_reason:
                adjustments.append(
                    {"kind": "sentiment", "reason": sentiment_reason,
                     "multiplier": sentiment_mult}
                )
            if multiplier != 1.0 and record:
                adjustments.append(
                    {
                        "kind": "head_to_head",
                        "reason": (
                            f"{record['matches']} matches vs {opp_name} in the last 2 seasons, "
                            f"averaging {record['avg_points']:.1f} pts "
                            f"({(multiplier - 1) * 100:+.1f}% applied)"
                        ),
                        "multiplier": multiplier,
                    }
                )

            # xpts_raw = the stats-only number before crowd capping, so the UI
            # can show "6.2 -> 0.0" style before/after explanations.
            xpts_raw, _, _ = project_fixture(
                rate, raw_avail, team_strength, opp, is_home, league_xg, multiplier
            )

            rows.append(
                {
                    "player_id": pid,
                    "event": fixture["event"],
                    "fixture": fixture["id"],
                    "opponent_team": opp_id,
                    "was_home": 1 if is_home else 0,
                    "difficulty": fixture["team_h_difficulty"] if is_home else fixture["team_a_difficulty"],
                    "xpts": round(xpts, 4),
                    "xpts_raw": round(xpts_raw, 4),
                    "p_start": round(avail.p_start, 4),
                    "expected_minutes": round(exp_minutes, 2),
                    "components": jdump({k: round(v, 4) for k, v in components.items()}),
                    "adjustments": jdump(adjustments),
                    "std_dev": round(rate.points_std, 3),
                    "computed_at": now,
                }
            )

    upsert_many(conn, "projections", rows)
    set_meta(conn, "projections_built_at", now)
    set_meta(conn, "season_current_weight", round(current_weight, 3))
    log.info(
        "projections: %d rows for %d players over %d fixtures (current-season weight %.2f)",
        len(rows), len(players), len(fixtures), current_weight,
    )
    return len(rows)


def horizon_totals(
    conn: sqlite3.Connection, discount: float = HORIZON_DISCOUNT
) -> dict[int, float]:
    """Discounted sum of xPts per player across the stored horizon."""
    row = conn.execute("SELECT MIN(event) AS mn FROM projections").fetchone()
    first = row["mn"] if row and row["mn"] else 0
    totals: dict[int, float] = {}
    for r in conn.execute("SELECT player_id, event, xpts FROM projections"):
        weight = discount ** max(0, (r["event"] or first) - first)
        totals[r["player_id"]] = totals.get(r["player_id"], 0.0) + (r["xpts"] or 0.0) * weight
    return totals
