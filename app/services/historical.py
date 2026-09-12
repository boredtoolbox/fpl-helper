"""vaastav/Fantasy-Premier-League historical dataset.

Clones (sparsely, blobless — the full repo is large) or pulls the dataset, loads
`merged_gw.csv` for the configured seasons into `historical_player_gw`, and
aggregates a per-(player, opponent) head-to-head table.

Player identity across seasons uses the FPL `code` (a stable per-person id that
survives transfers and season rollovers), read from each season's
`players_raw.csv`. Opponent identity uses `master_team_list.csv`, which maps a
season's team id to a team name; names are then matched to current team ids.
"""
from __future__ import annotations

import csv
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Iterable

from ..db import log_fetch, set_meta, upsert_many, utcnow

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/vaastav/Fantasy-Premier-League"
GIT_TIMEOUT = 900

# Team names that differ between the historical dataset and the current
# bootstrap. Applied to both sides — the loader normalises the dataset as it
# reads it, and the pages normalise a bootstrap name before looking history up —
# so an entry only has to exist, not point in a particular direction.
TEAM_NAME_ALIASES = {
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Spurs": "Tottenham",
    "Tottenham Hotspur": "Tottenham",
    "Nott'm Forest": "Nottingham Forest",
    "Nott'm Forest ": "Nottingham Forest",
    "Sheffield Utd": "Sheffield United",
    "Wolverhampton Wanderers": "Wolves",
    "Brighton and Hove Albion": "Brighton",
    "West Bromwich Albion": "West Brom",
    "Leeds": "Leeds United",
    "Newcastle United": "Newcastle",
    "West Ham United": "West Ham",
    "Ipswich Town": "Ipswich",
    "Leicester City": "Leicester",
    "AFC Bournemouth": "Bournemouth",
    "Norwich City": "Norwich",
    "Luton Town": "Luton",
}


def normalise_team_name(name: str | None) -> str:
    if not name:
        return ""
    name = str(name).strip()
    return TEAM_NAME_ALIASES.get(name, name)


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )


def git_available() -> bool:
    return shutil.which("git") is not None


def ensure_repo(repo_dir: Path, seasons: Iterable[str]) -> bool:
    """Clone on first run, `git pull` afterwards. Returns True if usable."""
    repo_dir = Path(repo_dir)
    seasons = list(seasons)
    if not git_available():
        log.error("git not on PATH — cannot fetch the historical dataset.")
        return False

    # Cone mode takes directories only; files sitting directly in a parent dir
    # (data/master_team_list.csv) come along automatically.
    sparse_paths = [f"data/{s}" for s in seasons]

    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if repo_dir.exists() and any(repo_dir.iterdir()):
            log.warning("%s exists but is not a git repo; leaving it alone.", repo_dir)
            return (repo_dir / "data").exists()
        log.info("Cloning historical dataset into %s (first run, may take a minute)…", repo_dir)
        res = _run_git(
            [
                "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                REPO_URL, str(repo_dir),
            ]
        )
        if res.returncode != 0:
            log.error("clone failed: %s", res.stderr.strip()[:500])
            return False
        res = _run_git(["sparse-checkout", "set", *sparse_paths], cwd=repo_dir)
        if res.returncode != 0:
            log.error("sparse-checkout failed: %s", res.stderr.strip()[:500])
            return False
        log.info("Historical dataset cloned.")
        return True

    # Existing clone: make sure the seasons we want are checked out, then pull.
    _run_git(["sparse-checkout", "set", *sparse_paths], cwd=repo_dir)
    res = _run_git(["pull", "--ff-only", "--depth", "1"], cwd=repo_dir)
    if res.returncode != 0:
        log.warning("git pull failed (using existing checkout): %s", res.stderr.strip()[:300])
    else:
        log.info("Historical dataset updated: %s", res.stdout.strip()[:200] or "up to date")
    return True


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        log.warning("missing historical file: %s", path)
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return list(csv.DictReader(fh))


def _num(value: Any, cast=int, default=0):
    if value in (None, "", "None", "NaN"):
        return default
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return default


def load_id_maps(repo_dir: Path, season: str) -> tuple[dict[int, int], dict[int, str]]:
    """Return (season element id -> stable player code, element id -> position)."""
    rows = _read_csv(Path(repo_dir) / "data" / season / "players_raw.csv")
    code_by_id: dict[int, int] = {}
    pos_by_id: dict[int, str] = {}
    types = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    for r in rows:
        pid = _num(r.get("id"), int, -1)
        code = _num(r.get("code"), int, -1)
        if pid >= 0 and code >= 0:
            code_by_id[pid] = code
            pos_by_id[pid] = types.get(_num(r.get("element_type"), int, 0), "")
    return code_by_id, pos_by_id


def load_team_map(repo_dir: Path, season: str) -> dict[int, str]:
    """season team id -> normalised team name.

    Prefers the season's own teams.csv; master_team_list.csv is the fallback but
    it lags behind by a couple of seasons, so it can't be the primary source.
    """
    out: dict[int, str] = {}
    for r in _read_csv(Path(repo_dir) / "data" / season / "teams.csv"):
        tid = _num(r.get("id"), int, -1)
        if tid >= 0:
            out[tid] = normalise_team_name(r.get("name"))
    if out:
        return out
    for r in _read_csv(Path(repo_dir) / "data" / "master_team_list.csv"):
        if (r.get("season") or "").strip() == season:
            out[_num(r.get("team"), int, -1)] = normalise_team_name(r.get("team_name"))
    if not out:
        log.warning("no team id->name map available for season %s", season)
    return out


HIST_INT_COLS = (
    "minutes starts total_points goals_scored assists clean_sheets goals_conceded "
    "saves bonus bps yellow_cards red_cards defensive_contribution "
    "clearances_blocks_interceptions tackles recoveries value"
).split()
HIST_FLOAT_COLS = (
    "expected_goals expected_assists expected_goal_involvements expected_goals_conceded"
).split()


def load_season(conn: sqlite3.Connection, repo_dir: Path, season: str) -> int:
    """Load one season's merged_gw.csv into historical_player_gw."""
    path = Path(repo_dir) / "data" / season / "gws" / "merged_gw.csv"
    rows = _read_csv(path)
    if not rows:
        log_fetch(conn, "historical", season, False, f"no rows at {path}")
        return 0

    code_by_id, pos_by_id = load_id_maps(repo_dir, season)
    team_map = load_team_map(repo_dir, season)

    out: list[dict[str, Any]] = []
    for r in rows:
        pid = _num(r.get("element"), int, -1)
        if pid < 0:
            continue
        opp_id = _num(r.get("opponent_team"), int, -1)
        rec: dict[str, Any] = {c: _num(r.get(c), int, 0) for c in HIST_INT_COLS}
        rec.update({c: _num(r.get(c), float, 0.0) for c in HIST_FLOAT_COLS})
        rec.update(
            {
                "season": season,
                "player_id": pid,
                "player_code": code_by_id.get(pid),
                "name": (r.get("name") or "").strip(),
                "position": (r.get("position") or pos_by_id.get(pid) or "").strip(),
                "team_name": normalise_team_name(r.get("team")),
                "round": _num(r.get("round") or r.get("GW"), int, 0),
                "fixture": _num(r.get("fixture"), int, 0),
                "opponent_team_id": opp_id,
                "opponent_team_name": team_map.get(opp_id, ""),
                "was_home": 1 if str(r.get("was_home", "")).strip().lower() == "true" else 0,
                "kickoff_time": r.get("kickoff_time"),
            }
        )
        out.append(rec)

    conn.execute("DELETE FROM historical_player_gw WHERE season = ?", (season,))
    upsert_many(conn, "historical_player_gw", out)
    unmapped = sum(1 for r in out if r["player_code"] is None)
    log_fetch(
        conn, "historical", season, True,
        f"{len(out)} rows ({unmapped} without a player code)",
    )
    log.info("historical %s: %d rows loaded (%d unmapped)", season, len(out), unmapped)
    return len(out)


def build_head_to_head(conn: sqlite3.Connection, seasons: Iterable[str]) -> int:
    """Aggregate (player_code, opponent) records across the loaded seasons.

    Only appearances (minutes > 0) count — a string of unused-sub zeroes would
    otherwise drag a player's average against an opponent to nothing.
    """
    seasons = list(seasons)
    placeholders = ",".join("?" for _ in seasons)
    conn.execute("DELETE FROM head_to_head")
    sql = f"""
        INSERT INTO head_to_head (
            player_code, opponent_team_name, matches, starts, avg_points, avg_minutes,
            goals, assists, home_matches, home_avg_points, away_matches, away_avg_points
        )
        SELECT
            player_code,
            opponent_team_name,
            COUNT(*),
            SUM(starts),
            AVG(total_points),
            AVG(minutes),
            SUM(goals_scored),
            SUM(assists),
            SUM(was_home),
            AVG(CASE WHEN was_home = 1 THEN total_points END),
            SUM(1 - was_home),
            AVG(CASE WHEN was_home = 0 THEN total_points END)
        FROM historical_player_gw
        WHERE season IN ({placeholders})
          AND player_code IS NOT NULL
          AND opponent_team_name <> ''
          AND minutes > 0
        GROUP BY player_code, opponent_team_name
    """
    conn.execute(sql, seasons)
    n = conn.execute("SELECT COUNT(*) FROM head_to_head").fetchone()[0]
    set_meta(conn, "head_to_head_built_at", utcnow())
    log.info("head_to_head: %d (player, opponent) pairs", n)
    return n


def sync_historical(
    conn: sqlite3.Connection, repo_dir: Path, seasons: Iterable[str], *, pull: bool = True
) -> dict:
    """Full historical refresh: ensure repo, load seasons, rebuild head-to-head."""
    seasons = list(seasons)
    result: dict[str, Any] = {"seasons": {}, "head_to_head": 0}
    if pull and not ensure_repo(Path(repo_dir), seasons):
        log.warning("historical repo unavailable; keeping whatever is already in the DB.")
        result["error"] = "repo unavailable"
        return result
    for season in seasons:
        result["seasons"][season] = load_season(conn, Path(repo_dir), season)
    result["head_to_head"] = build_head_to_head(conn, seasons)
    set_meta(conn, "historical_synced_at", utcnow())
    return result
