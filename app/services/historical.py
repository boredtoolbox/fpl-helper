"""vaastav/Fantasy-Premier-League historical dataset.

Downloads the handful of CSVs the loaders below actually open, loads
`merged_gw.csv` for the configured seasons into `historical_player_gw`, and
aggregates a per-(player, opponent) head-to-head table.

Four files per season are needed, so they are fetched over plain HTTPS rather
than by cloning the repository. git is one more thing to install — and one more
thing to be missing on a machine that just downloaded a binary — while even a
blobless sparse clone drags in the whole working tree (roughly 115 MB) to read
about 12 MB of CSV. ETags mean an unchanged file costs a 304 and nothing else.

Player identity across seasons uses the FPL `code` (a stable per-person id that
survives transfers and season rollovers), read from each season's
`players_raw.csv`. Opponent identity uses `master_team_list.csv`, which maps a
season's team id to a team name; names are then matched to current team ids.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from ..db import log_fetch, set_meta, upsert_many, utcnow

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/vaastav/Fantasy-Premier-League"
RAW_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
USER_AGENT = "fpl-helper/1.0 (personal FPL analysis tool)"
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3
# Path -> the ETag it last arrived with, so a refresh re-downloads only what
# actually changed. Lives inside the dataset directory, next to what it describes.
ETAG_FILE = ".etags.json"

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

# Not season-specific, so it is fetched once per refresh rather than per season.
SHARED_FILES = ("data/master_team_list.csv",)


def normalise_team_name(name: str | None) -> str:
    if not name:
        return ""
    name = str(name).strip()
    return TEAM_NAME_ALIASES.get(name, name)


def season_files(season: str) -> tuple[str, ...]:
    """The only paths this module opens for a season, relative to the data dir."""
    return (
        f"data/{season}/players_raw.csv",
        f"data/{season}/teams.csv",
        f"data/{season}/gws/merged_gw.csv",
    )


def merged_gw_path(data_dir: Path, season: str) -> Path:
    """The file that carries the actual gameweek rows for a season."""
    return Path(data_dir) / "data" / season / "gws" / "merged_gw.csv"


def _load_etags(data_dir: Path) -> dict[str, str]:
    try:
        raw = json.loads((data_dir / ETAG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_etags(data_dir: Path, etags: dict[str, str]) -> None:
    try:
        (data_dir / ETAG_FILE).write_text(json.dumps(etags, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        # Losing the cache costs a re-download next time, nothing more.
        log.warning("could not write %s: %s", data_dir / ETAG_FILE, exc)


def _write_atomic(resp: requests.Response, dest: Path) -> None:
    """Stream to a sibling temp file, then rename over the target.

    A half-written CSV is worse than a stale one, because it still parses: the
    loader would silently drop every row after the truncation instead of
    failing and leaving the previous data in place.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _download(
    session: requests.Session, rel_path: str, dest: Path, etags: dict[str, str]
) -> bool:
    """Fetch one CSV to `dest`. True if `dest` is usable afterwards."""
    url = f"{RAW_BASE}/{rel_path}"
    headers: dict[str, str] = {}
    # An ETag only means anything while the file it describes is still there;
    # sending one for a file we have since deleted would earn a 304 and no data.
    if dest.exists() and rel_path in etags:
        headers["If-None-Match"] = etags[rel_path]

    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = session.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True)
        except requests.RequestException as exc:
            log.warning("GET %s failed (attempt %d): %s", rel_path, attempt + 1, exc)
        else:
            with resp:
                if resp.status_code == 304:
                    log.debug("%s unchanged", rel_path)
                    return True
                if resp.status_code == 404:
                    # A season the dataset has not published yet, most likely.
                    log.warning("%s is not in the dataset (404).", rel_path)
                    return dest.exists()
                if resp.status_code == 200:
                    try:
                        _write_atomic(resp, dest)
                    except (OSError, requests.RequestException) as exc:
                        log.error("could not write %s: %s", dest, exc)
                        return dest.exists()
                    etag = resp.headers.get("ETag")
                    if etag:
                        etags[rel_path] = etag
                    else:
                        etags.pop(rel_path, None)
                    log.info("fetched %s", rel_path)
                    return True
                log.warning(
                    "GET %s returned HTTP %d (attempt %d)", rel_path, resp.status_code, attempt + 1
                )
        if attempt < DOWNLOAD_RETRIES - 1:
            time.sleep(2 ** attempt)

    # Falling back to the copy on disk is the bargain the old `git pull` failure
    # made too: stale history beats no history.
    if dest.exists():
        log.warning("could not refresh %s; keeping the copy already on disk.", rel_path)
        return True
    log.error("could not download %s, and there is no local copy.", rel_path)
    return False


def _note_legacy_clone(data_dir: Path) -> None:
    """Older installs cloned the repo here. Nothing reads the checkout now."""
    if not (data_dir / ".git").exists():
        return
    log.warning(
        "%s is an old git checkout of the dataset, which nothing reads any more "
        "— the CSVs are downloaded directly now. Deleting the whole directory is "
        "safe and reclaims about 115 MB; the next refresh re-fetches the ~11 MB "
        "it actually needs.",
        data_dir,
    )


def ensure_dataset(data_dir: Path, seasons: Iterable[str]) -> bool:
    """Download the CSVs the loaders read. True if any season is loadable."""
    data_dir = Path(data_dir)
    seasons = list(seasons)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("cannot create %s: %s", data_dir, exc)
        return False
    _note_legacy_clone(data_dir)

    etags = _load_etags(data_dir)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/plain, */*"})

    usable: list[str] = []
    try:
        for rel in SHARED_FILES:
            _download(session, rel, data_dir / rel, etags)
        for season in seasons:
            for rel in season_files(season):
                _download(session, rel, data_dir / rel, etags)
            # merged_gw.csv carries the rows; the other two only decorate them,
            # and load_team_map already falls back to the master list when a
            # season's own teams.csv is missing. So that one file decides
            # whether the season is worth loading at all.
            if merged_gw_path(data_dir, season).exists():
                usable.append(season)
            else:
                log.warning("season %s has no gameweek data available; skipping it.", season)
    finally:
        session.close()
        _save_etags(data_dir, etags)

    return bool(usable)


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


def load_id_maps(data_dir: Path, season: str) -> tuple[dict[int, int], dict[int, str]]:
    """Return (season element id -> stable player code, element id -> position)."""
    rows = _read_csv(Path(data_dir) / "data" / season / "players_raw.csv")
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


def load_team_map(data_dir: Path, season: str) -> dict[int, str]:
    """season team id -> normalised team name.

    Prefers the season's own teams.csv; master_team_list.csv is the fallback but
    it lags behind by a couple of seasons, so it can't be the primary source.
    """
    out: dict[int, str] = {}
    for r in _read_csv(Path(data_dir) / "data" / season / "teams.csv"):
        tid = _num(r.get("id"), int, -1)
        if tid >= 0:
            out[tid] = normalise_team_name(r.get("name"))
    if out:
        return out
    for r in _read_csv(Path(data_dir) / "data" / "master_team_list.csv"):
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


def load_season(conn: sqlite3.Connection, data_dir: Path, season: str) -> int:
    """Load one season's merged_gw.csv into historical_player_gw."""
    path = merged_gw_path(data_dir, season)
    rows = _read_csv(path)
    if not rows:
        log_fetch(conn, "historical", season, False, f"no rows at {path}")
        return 0

    code_by_id, pos_by_id = load_id_maps(data_dir, season)
    team_map = load_team_map(data_dir, season)

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
    conn: sqlite3.Connection, data_dir: Path, seasons: Iterable[str], *, download: bool = True
) -> dict:
    """Full historical refresh: fetch the CSVs, load seasons, rebuild head-to-head."""
    seasons = list(seasons)
    result: dict[str, Any] = {"seasons": {}, "head_to_head": 0}
    if download and not ensure_dataset(Path(data_dir), seasons):
        log.warning("historical dataset unavailable; keeping whatever is already in the DB.")
        result["error"] = "dataset unavailable"
        return result
    for season in seasons:
        result["seasons"][season] = load_season(conn, Path(data_dir), season)
    result["head_to_head"] = build_head_to_head(conn, seasons)
    set_meta(conn, "historical_synced_at", utcnow())
    return result
