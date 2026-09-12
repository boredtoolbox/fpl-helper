"""SQLite access layer.

Plain sqlite3 — no ORM. One schema definition, idempotent `init_db`, and a
handful of helpers. Row factory is sqlite3.Row so callers get dict-like access.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS teams (
    id                    INTEGER PRIMARY KEY,
    code                  INTEGER,
    name                  TEXT NOT NULL,
    short_name            TEXT,
    strength              INTEGER,
    strength_overall_home INTEGER,
    strength_overall_away INTEGER,
    strength_attack_home  INTEGER,
    strength_attack_away  INTEGER,
    strength_defence_home INTEGER,
    strength_defence_away INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id                            INTEGER PRIMARY KEY,
    code                          INTEGER,
    web_name                      TEXT NOT NULL,
    first_name                    TEXT,
    second_name                   TEXT,
    team                          INTEGER,
    element_type                  INTEGER,
    now_cost                      INTEGER,
    status                        TEXT,
    news                          TEXT,
    news_added                    TEXT,
    chance_of_playing_next_round  INTEGER,
    chance_of_playing_this_round  INTEGER,
    total_points                  INTEGER,
    points_per_game               REAL,
    form                          REAL,
    ep_next                       REAL,
    selected_by_percent           REAL,
    minutes                       INTEGER,
    starts                        INTEGER,
    goals_scored                  INTEGER,
    assists                       INTEGER,
    clean_sheets                  INTEGER,
    goals_conceded                INTEGER,
    saves                         INTEGER,
    bonus                         INTEGER,
    bps                           INTEGER,
    yellow_cards                  INTEGER,
    red_cards                     INTEGER,
    penalties_order               INTEGER,
    corners_and_indirect_freekicks_order INTEGER,
    direct_freekicks_order        INTEGER,
    expected_goals                REAL,
    expected_assists              REAL,
    expected_goal_involvements    REAL,
    expected_goals_conceded       REAL,
    expected_goals_per_90         REAL,
    expected_assists_per_90       REAL,
    expected_goal_involvements_per_90 REAL,
    expected_goals_conceded_per_90    REAL,
    saves_per_90                  REAL,
    starts_per_90                 REAL,
    clean_sheets_per_90           REAL,
    defensive_contribution        INTEGER,
    defensive_contribution_per_90 REAL,
    clearances_blocks_interceptions INTEGER,
    tackles                       INTEGER,
    recoveries                    INTEGER,
    influence                     REAL,
    creativity                    REAL,
    threat                        REAL,
    ict_index                     REAL,
    fetched_at                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_code ON players(code);

CREATE TABLE IF NOT EXISTS gameweeks (
    id             INTEGER PRIMARY KEY,
    name           TEXT,
    deadline_time  TEXT,
    finished       INTEGER,
    data_checked   INTEGER,
    is_current     INTEGER,
    is_next        INTEGER,
    is_previous    INTEGER,
    average_entry_score INTEGER,
    highest_score  INTEGER
);

CREATE TABLE IF NOT EXISTS fixtures (
    id                INTEGER PRIMARY KEY,
    code              INTEGER,
    event             INTEGER,
    kickoff_time      TEXT,
    team_h            INTEGER,
    team_a            INTEGER,
    team_h_score      INTEGER,
    team_a_score      INTEGER,
    team_h_difficulty INTEGER,
    team_a_difficulty INTEGER,
    finished          INTEGER,
    started           INTEGER,
    minutes           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fixtures_event ON fixtures(event);
CREATE INDEX IF NOT EXISTS idx_fixtures_teams ON fixtures(team_h, team_a);

-- Who did what in a match, straight from the fixtures endpoint's `stats` array.
--
-- This is the only complete source for it. `player_gw_history` looks like it
-- would do, but element summaries are fetched on a per-player budget, so its
-- coverage is whoever happened to be in the last few runs — and it records an
-- own goal against the player who scored it, not the side it counted for, so
-- five of the first thirty fixtures could not be reconciled with their own
-- scoreline. The API's version has neither problem and was already being
-- fetched and discarded.
--
-- `identifier` is FPL's own name for the stat (goals_scored, assists,
-- own_goals, yellow_cards, red_cards, saves, bonus, bps, ...), `side` is h/a,
-- and `value` is how many — a striker with a hat-trick is one row with value 3.
CREATE TABLE IF NOT EXISTS fixture_stats (
    fixture     INTEGER NOT NULL,
    identifier  TEXT NOT NULL,
    side        TEXT NOT NULL,
    player_id   INTEGER NOT NULL,
    value       INTEGER,
    PRIMARY KEY (fixture, identifier, side, player_id)
);
CREATE INDEX IF NOT EXISTS idx_fixture_stats_fixture ON fixture_stats(fixture);

-- Per-player per-gameweek rows for the CURRENT season (element-summary history)
CREATE TABLE IF NOT EXISTS player_gw_history (
    player_id      INTEGER NOT NULL,
    round          INTEGER NOT NULL,
    fixture        INTEGER NOT NULL,
    opponent_team  INTEGER,
    was_home       INTEGER,
    kickoff_time   TEXT,
    minutes        INTEGER,
    starts         INTEGER,
    total_points   INTEGER,
    goals_scored   INTEGER,
    assists        INTEGER,
    clean_sheets   INTEGER,
    goals_conceded INTEGER,
    saves          INTEGER,
    bonus          INTEGER,
    bps            INTEGER,
    yellow_cards   INTEGER,
    red_cards      INTEGER,
    own_goals      INTEGER,
    penalties_missed INTEGER,
    penalties_saved  INTEGER,
    expected_goals             REAL,
    expected_assists           REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded    REAL,
    defensive_contribution     INTEGER,
    clearances_blocks_interceptions INTEGER,
    tackles        INTEGER,
    recoveries     INTEGER,
    value          INTEGER,
    PRIMARY KEY (player_id, fixture)
);
CREATE INDEX IF NOT EXISTS idx_pgh_player ON player_gw_history(player_id, round);

-- Per-player per-gameweek rows for PAST seasons (vaastav dataset)
CREATE TABLE IF NOT EXISTS historical_player_gw (
    season         TEXT NOT NULL,
    player_code    INTEGER,
    player_id      INTEGER,
    name           TEXT,
    position       TEXT,
    team_name      TEXT,
    round          INTEGER,
    fixture        INTEGER,
    opponent_team_id   INTEGER,
    opponent_team_name TEXT,
    was_home       INTEGER,
    kickoff_time   TEXT,
    minutes        INTEGER,
    starts         INTEGER,
    total_points   INTEGER,
    goals_scored   INTEGER,
    assists        INTEGER,
    clean_sheets   INTEGER,
    goals_conceded INTEGER,
    saves          INTEGER,
    bonus          INTEGER,
    bps            INTEGER,
    yellow_cards   INTEGER,
    red_cards      INTEGER,
    expected_goals             REAL,
    expected_assists           REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded    REAL,
    defensive_contribution     INTEGER,
    clearances_blocks_interceptions INTEGER,
    tackles        INTEGER,
    recoveries     INTEGER,
    value          INTEGER,
    PRIMARY KEY (season, player_id, fixture)
);
CREATE INDEX IF NOT EXISTS idx_hpg_code ON historical_player_gw(player_code);
CREATE INDEX IF NOT EXISTS idx_hpg_opp  ON historical_player_gw(player_code, opponent_team_name);
CREATE INDEX IF NOT EXISTS idx_hpg_team ON historical_player_gw(team_name, opponent_team_name);
CREATE INDEX IF NOT EXISTS idx_hpg_vs   ON historical_player_gw(opponent_team_name);

-- Aggregated (player, opponent) record over the loaded historical seasons
CREATE TABLE IF NOT EXISTS head_to_head (
    player_code        INTEGER NOT NULL,
    opponent_team_name TEXT NOT NULL,
    matches            INTEGER,
    starts             INTEGER,
    avg_points         REAL,
    avg_minutes        REAL,
    goals              INTEGER,
    assists            INTEGER,
    home_matches       INTEGER,
    home_avg_points    REAL,
    away_matches       INTEGER,
    away_avg_points    REAL,
    PRIMARY KEY (player_code, opponent_team_name)
);

CREATE TABLE IF NOT EXISTS my_entries (
    team_id        INTEGER PRIMARY KEY,
    name           TEXT,
    player_name    TEXT,
    summary_overall_points INTEGER,
    summary_overall_rank   INTEGER,
    summary_event_points   INTEGER,
    summary_event_rank     INTEGER,
    current_event  INTEGER,
    bank           INTEGER,
    value          INTEGER,
    total_transfers INTEGER,
    last_deadline_bank  INTEGER,
    last_deadline_value INTEGER,
    raw_json       TEXT,
    fetched_at     TEXT
);

CREATE TABLE IF NOT EXISTS my_picks (
    team_id       INTEGER NOT NULL,
    event         INTEGER NOT NULL,
    player_id     INTEGER NOT NULL,
    position      INTEGER,
    multiplier    INTEGER,
    is_captain    INTEGER,
    is_vice_captain INTEGER,
    selling_price INTEGER,
    purchase_price INTEGER,
    PRIMARY KEY (team_id, event, player_id)
);

CREATE TABLE IF NOT EXISTS my_entry_gw (
    team_id       INTEGER NOT NULL,
    event         INTEGER NOT NULL,
    points        INTEGER,
    total_points  INTEGER,
    rank          INTEGER,
    overall_rank  INTEGER,
    bank          INTEGER,
    value         INTEGER,
    event_transfers INTEGER,
    event_transfers_cost INTEGER,
    points_on_bench INTEGER,
    PRIMARY KEY (team_id, event)
);

CREATE TABLE IF NOT EXISTS my_chips (
    team_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    event    INTEGER,
    PRIMARY KEY (team_id, name, event)
);

CREATE TABLE IF NOT EXISTS my_leagues (
    team_id       INTEGER NOT NULL,
    league_id     INTEGER NOT NULL,
    name          TEXT,
    league_type   TEXT,
    scoring       TEXT,
    entry_rank    INTEGER,
    entry_last_rank INTEGER,
    created       TEXT,
    -- Everything below arrives in the same entry/ response as the columns
    -- above. Storing it is what lets the stats page fill its table without
    -- paging through league standings for numbers we were already sent.
    rank_count            INTEGER,   -- how many teams are in the league
    entry_percentile_rank INTEGER,   -- FPL's own percentile, already computed
    entry_total           INTEGER,   -- points scored while in this league
    fetched_at    TEXT,
    PRIMARY KEY (team_id, league_id)
);

-- Legacy. Nothing writes this any more: our rank, the league size and our
-- points in it all arrive with the entry/ response and live on my_leagues, so
-- paging through standings to rediscover them was pure cost. Kept so an
-- existing database still matches the schema.
CREATE TABLE IF NOT EXISTS league_standings (
    league_id   INTEGER NOT NULL,
    entry_id    INTEGER NOT NULL,
    rank        INTEGER,
    last_rank   INTEGER,
    total       INTEGER,
    event_total INTEGER,
    entry_name  TEXT,
    player_name TEXT,
    num_entries INTEGER,
    fetched_at  TEXT,
    PRIMARY KEY (league_id, entry_id)
);

-- Raw Gemini payloads plus the resolved per-player adjustments
CREATE TABLE IF NOT EXISTS crowd_intel_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT,
    model        TEXT,
    n_documents  INTEGER,
    ok           INTEGER,
    error        TEXT,
    raw_response TEXT,
    general_notes TEXT
);

CREATE TABLE IF NOT EXISTS crowd_intel (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    player_id     INTEGER,
    player_name   TEXT,
    match_score   REAL,
    availability  TEXT,
    start_probability_hint REAL,
    sentiment     REAL,
    predicted_role_change TEXT,
    confidence    REAL,
    n_sources     INTEGER,
    reasons       TEXT,
    resolved      INTEGER,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_crowd_player ON crowd_intel(player_id);

CREATE TABLE IF NOT EXISTS crowd_documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER,
    source     TEXT,
    kind       TEXT,
    title      TEXT,
    url        TEXT,
    published  TEXT,
    text       TEXT
);

-- One row per (player, gameweek) projection, with its component breakdown
CREATE TABLE IF NOT EXISTS projections (
    player_id   INTEGER NOT NULL,
    event       INTEGER NOT NULL,
    fixture     INTEGER NOT NULL,
    opponent_team INTEGER,
    was_home    INTEGER,
    difficulty  INTEGER,
    xpts        REAL,
    xpts_raw    REAL,
    p_start     REAL,
    expected_minutes REAL,
    components  TEXT,
    adjustments TEXT,
    std_dev     REAL,
    computed_at TEXT,
    PRIMARY KEY (player_id, event, fixture)
);
CREATE INDEX IF NOT EXISTS idx_proj_event ON projections(event);

-- What we projected for a gameweek BEFORE it was played.
--
-- `projections` is wiped and rebuilt from scratch on every run, so it only ever
-- holds the fixtures still to come: the moment a gameweek kicks off, what we
-- expected of it is gone. This keeps that number, so a squad's projection can
-- be set against what it actually scored.
--
-- Rows are refreshed on every rebuild while a gameweek is still unplayed and
-- frozen the moment it kicks off, which leaves the last projection made before
-- the deadline — the one that was actually standing when the team was picked.
CREATE TABLE IF NOT EXISTS projection_history (
    event       INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    xpts        REAL,
    captured_at TEXT,
    PRIMARY KEY (event, player_id)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT,
    key        TEXT,
    ok         INTEGER,
    detail     TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_stage ON fetch_log(stage, key);

-- Resolved YouTube channel ids, so a URL/handle is only looked up once.
CREATE TABLE IF NOT EXISTS youtube_channels (
    source      TEXT PRIMARY KEY,
    channel_id  TEXT,
    name        TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Columns added to a table after it first shipped. SCHEMA is all CREATE TABLE
# IF NOT EXISTS, which does nothing at all to a database that already exists, so
# anything added later has to be applied to the live file explicitly. Adding a
# nullable column is cheap and rewrites no rows.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("my_leagues", "rank_count", "INTEGER"),
    ("my_leagues", "entry_percentile_rank", "INTEGER"),
    ("my_leagues", "entry_total", "INTEGER"),
    ("my_leagues", "fetched_at", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table isn't there yet; SCHEMA has just created it or will
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Short-lived connection that commits on success, rolls back on error."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_many(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict[str, Any]],
    columns: Sequence[str] | None = None,
) -> int:
    """INSERT OR REPLACE a batch of dict rows. Returns the row count."""
    rows = list(rows)
    if not rows:
        return 0
    cols = list(columns or rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def log_fetch(
    conn: sqlite3.Connection, stage: str, key: str, ok: bool, detail: str = ""
) -> None:
    conn.execute(
        "INSERT INTO fetch_log (stage, key, ok, detail, fetched_at) VALUES (?,?,?,?,?)",
        (stage, key, 1 if ok else 0, detail[:2000], utcnow()),
    )


def last_fetch(conn: sqlite3.Connection, stage: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE stage=? AND key=? AND ok=1 "
        "ORDER BY fetched_at DESC LIMIT 1",
        (stage, key),
    ).fetchone()
    return row["fetched_at"] if row else None


# Meta key holding the currently-running job, so the web process can see a job
# that cron started in a process of its own.
ACTIVE_JOB_KEY = "active_job"

# Meta keys whose values together define "the data the pages are showing".
# Any job that changes one of these changes the version the browser polls for.
DATA_VERSION_KEYS = (
    "last_refresh_completed",
    "last_refresh_failed_stages",
    "last_crowd_completed",
    "last_crowd_failed_stages",
    "projections_built_at",
)


def last_refresh_time(conn: sqlite3.Connection) -> str | None:
    return get_meta(conn, "last_refresh_completed")


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def jload(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


class RefreshLock:
    """Process-wide guard so two refreshes can't overlap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running_since: datetime | None = None
        self.stage: str = ""

    def acquire(self) -> bool:
        got = self._lock.acquire(blocking=False)
        if got:
            self.running_since = datetime.now(timezone.utc)
            self.stage = "starting"
        return got

    def release(self) -> None:
        self.running_since = None
        self.stage = ""
        try:
            self._lock.release()
        except RuntimeError:
            pass

    @property
    def is_running(self) -> bool:
        return self.running_since is not None


REFRESH_LOCK = RefreshLock()
