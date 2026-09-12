"""The FPL data refresh, and the separate crowd-intel job.

Every stage is independently fault-tolerant: if the FPL API is slow, or GitHub
is unreachable, the stage logs and the rest carries on. The app always renders
from the last good DB state.

`run_refresh` deliberately does NOT call Gemini. Pulling FPL data is free and
can be done as often as you like — the Refresh button included — while crowd
intel costs an API call every single time, so it lives in `run_crowd_intel`
and cron decides when that runs. The refresh still *reads* whatever
adjustments the last crowd job stored.

Run manually with:  python -m app.refresh
                    python -m app.crowd_refresh
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config, PROJECT_ROOT, load_config
from .db import (
    ACTIVE_JOB_KEY, REFRESH_LOCK, connect, get_meta, init_db, jdump, log_fetch,
    set_meta, utcnow,
)
from .logging_setup import setup_logging
from .retention import run_retention
from .services import crowd_intel, engine, fpl_api, historical, kits

log = logging.getLogger(__name__)

HISTORICAL_INTERVAL_DAYS = 7
# Ceiling on element-summary fetches in one run, so a season's worth of catching
# up is spread over several runs instead of one very long one.
DAILY_SUMMARY_BUDGET = 180
# How long after kick-off a match counts as having new data in it. Comfortably
# past full time, and past the point where FPL finalises bonus points — fetching
# earlier than that would cache provisional scores and never correct them.
MATCH_SETTLES_AFTER = timedelta(hours=3)


class JobHeartbeat:
    """Publishes what is running, through SQLite, for the web process to read.

    Cron runs these jobs in their own process, where REFRESH_LOCK — a plain
    threading.Lock — is invisible. The database is the only thing both processes
    share, so progress goes through `meta`. Each beat restamps the time, which
    is what lets a reader tell a live job from one that was killed mid-run.
    """

    def __init__(self, db_path: str | Path, job: str) -> None:
        # Its own connection on purpose. Sharing the job's would mean every beat
        # committed whatever the current stage had written so far, which would
        # quietly undo _stage's rollback-on-failure.
        self.conn = connect(db_path)
        self.job = job
        self.started_at = utcnow()

    def beat(self, stage: str) -> None:
        self._write({
            "job": self.job, "stage": stage,
            "started_at": self.started_at, "heartbeat": utcnow(),
        })

    def clear(self) -> None:
        self._write(None)
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _write(self, payload: dict[str, Any] | None) -> None:
        # Never let bookkeeping take down the job it is reporting on.
        try:
            set_meta(self.conn, ACTIVE_JOB_KEY, jdump(payload) if payload else "")
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not publish job heartbeat: %s", exc)


# The heartbeat this process is currently publishing. Module-level for the same
# reason REFRESH_LOCK is: only one job runs per process at a time, so _stage can
# find it without being handed it at every call site.
_HEARTBEAT: JobHeartbeat | None = None


def _stage(
    conn: sqlite3.Connection, results: dict[str, Any], name: str, func: Callable[[], Any]
) -> Any:
    """Run one stage, recording success/failure without ever propagating.

    A failed stage is rolled back before returning. Several stages clear a table
    before refilling it — projections and each historical season — so without
    this the caller's commit would persist the delete and leave the app with
    nothing to render. Isolating a stage means undoing it, not just surviving it.
    """
    started = time.monotonic()
    log.info("--- stage: %s ---", name)
    REFRESH_LOCK.stage = name
    if _HEARTBEAT is not None:
        _HEARTBEAT.beat(name)
    try:
        value = func()
    except Exception as exc:  # noqa: BLE001 - stage isolation is the point
        elapsed = time.monotonic() - started
        log.exception("stage %s failed after %.1fs", name, elapsed)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - a broken connection must not mask the real error
            log.warning("could not roll back after stage %s", name)
        results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "seconds": round(elapsed, 1)}
        return None
    elapsed = time.monotonic() - started
    results[name] = {"ok": True, "result": value, "seconds": round(elapsed, 1)}
    log.info("stage %s done in %.1fs", name, elapsed)
    return value


def _days_since(timestamp: str | None) -> float:
    if not timestamp:
        return 1e9
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return 1e9
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0


def current_event(conn: sqlite3.Connection) -> int:
    """The gameweek we care about: the current one, else the next, else 1."""
    for clause in ("is_current = 1", "is_next = 1"):
        row = conn.execute(
            f"SELECT id FROM gameweeks WHERE {clause} ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            return int(row["id"])
    row = conn.execute(
        "SELECT MIN(id) AS id FROM gameweeks WHERE finished = 0"
    ).fetchone()
    return int(row["id"]) if row and row["id"] else 1


def _parse_ts(value: str | None) -> datetime | None:
    """Parse either timestamp format in the DB. FPL sends Z, we write +00:00."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _summary_fetched_at(conn: sqlite3.Connection) -> dict[int, datetime]:
    """When each player's element-summary was last fetched successfully.

    One grouped query rather than a lookup per player: this runs over every
    player in the game, and the point of the exercise is to be cheap.
    """
    out: dict[int, datetime] = {}
    for r in conn.execute(
        "SELECT key, MAX(fetched_at) AS fetched_at FROM fetch_log "
        "WHERE stage = 'element_summary' AND ok = 1 GROUP BY key"
    ):
        when = _parse_ts(r["fetched_at"])
        if when is not None and str(r["key"]).isdigit():
            out[int(r["key"])] = when
    return out


def _team_data_settled_at(conn: sqlite3.Connection) -> dict[int, datetime]:
    """When each team's most recent finished match settled."""
    out: dict[int, datetime] = {}
    for r in conn.execute(
        "SELECT team_h, team_a, kickoff_time FROM fixtures "
        "WHERE finished = 1 AND kickoff_time IS NOT NULL"
    ):
        kickoff = _parse_ts(r["kickoff_time"])
        if kickoff is None:
            continue
        settled = kickoff + MATCH_SETTLES_AFTER
        for team in (r["team_h"], r["team_a"]):
            if team is None:
                continue
            if settled > out.get(int(team), datetime.min.replace(tzinfo=timezone.utc)):
                out[int(team)] = settled
    return out


def summary_priority(conn: sqlite3.Connection, cfg: Config) -> list[int]:
    """Which players to pull element-summary for, in priority order.

    A player's match history only changes when their team plays, so this is
    everyone whose team has finished a match since we last fetched them — my own
    players first, then the most-owned and highest-scoring, then the rest,
    capped so one run stays bounded.

    Between gameweeks nobody qualifies and the list comes back empty, which is
    what makes pressing Refresh repeatedly cost nothing.
    """
    fetched_at = _summary_fetched_at(conn)
    settled_at = _team_data_settled_at(conn)
    team_of = {int(r["id"]): r["team"] for r in conn.execute("SELECT id, team FROM players")}

    def has_new_data(pid: int) -> bool:
        team = team_of.get(pid)
        settled = settled_at.get(int(team)) if team is not None else None
        if settled is None:
            return False  # their team has not finished a match yet
        last = fetched_at.get(pid)
        return last is None or settled > last

    ordered: list[int] = []
    seen: set[int] = set()

    def add(pid: int | None) -> None:
        if not pid:
            return
        pid = int(pid)
        if pid in seen or not has_new_data(pid):
            return
        seen.add(pid)
        ordered.append(pid)

    for r in conn.execute(
        "SELECT DISTINCT player_id FROM my_picks WHERE team_id IN "
        f"({','.join('?' for _ in cfg.team_ids) or 'NULL'})",
        tuple(cfg.team_ids),
    ):
        add(r["player_id"])

    for r in conn.execute(
        "SELECT id FROM players ORDER BY selected_by_percent DESC LIMIT 100"
    ):
        add(r["id"])
    for r in conn.execute("SELECT id FROM players ORDER BY total_points DESC LIMIT 100"):
        add(r["id"])
    # Everyone else who has played since we last looked, best-scoring first, so
    # that a truncated run leaves the least useful players until next time.
    for r in conn.execute("SELECT id FROM players ORDER BY total_points DESC"):
        add(r["id"])

    return ordered[:DAILY_SUMMARY_BUDGET]


def rebuild_projections(conn: sqlite3.Connection, cfg: Config) -> int:
    """Projections, folding in whatever the last crowd-intel job left behind.

    Reads the stored adjustments rather than fetching them, so this is cheap and
    works fine when crowd intel is stale, disabled, or has never run.
    """
    adjustments = crowd_intel.latest_adjustments(conn)
    return engine.build_projections(
        conn, cfg.historical_seasons, cfg.horizon_gws, adjustments
    )


def run_refresh(cfg: Config | None = None, *, force_historical: bool = False) -> dict[str, Any]:
    """Full refresh. Returns a per-stage report; never raises."""
    cfg = cfg or load_config()
    if not REFRESH_LOCK.acquire():
        log.warning("refresh already running since %s — skipping", REFRESH_LOCK.running_since)
        return {"skipped": True, "reason": "a refresh is already running"}

    started = time.monotonic()
    results: dict[str, Any] = {"started_at": utcnow()}
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    global _HEARTBEAT
    _HEARTBEAT = JobHeartbeat(cfg.db_path, "refresh")
    try:
        client = fpl_api.FPLClient()

        _stage(conn, results, "bootstrap", lambda: fpl_api.sync_bootstrap(conn, client))
        conn.commit()
        _stage(conn, results, "fixtures", lambda: fpl_api.sync_fixtures(conn, client))
        conn.commit()

        event = current_event(conn)
        results["event"] = event
        log.info("working gameweek: %d", event)

        def sync_my_teams() -> dict[str, Any]:
            out: dict[str, Any] = {}
            for team_id in cfg.team_ids:
                entry = fpl_api.sync_entry(conn, client, team_id)
                if entry is None:
                    out[str(team_id)] = "entry not found"
                    continue
                fpl_api.sync_entry_history(conn, client, team_id)
                # Picks only exist once a deadline has passed.
                picks = 0
                for gw in range(event, 0, -1):
                    picks = fpl_api.sync_picks(conn, client, team_id, gw)
                    if picks:
                        break
                out[str(team_id)] = {"picks": picks}
                conn.commit()
            return out

        _stage(conn, results, "my_teams", sync_my_teams)

        # No league stage: our rank, the league size and our points in it all
        # arrive with the entry above, so there is nothing left to fetch.

        _stage(
            conn, results, "element_summaries",
            lambda: fpl_api.sync_element_summaries(conn, client, summary_priority(conn, cfg)),
        )
        conn.commit()

        def maybe_historical() -> dict[str, Any]:
            age = _days_since(get_meta(conn, "historical_synced_at"))
            if not force_historical and age < HISTORICAL_INTERVAL_DAYS:
                log.info("historical dataset is %.1f days old — skipping (weekly)", age)
                return {"skipped": True, "age_days": round(age, 1)}
            return historical.sync_historical(
                conn, cfg.historical_repo_dir, cfg.historical_seasons
            )

        _stage(conn, results, "historical", maybe_historical)
        conn.commit()

        _stage(conn, results, "projections", lambda: rebuild_projections(conn, cfg))
        conn.commit()

        _stage(
            conn, results, "images",
            lambda: {
                "kits": kits.sync_kits(conn, PROJECT_ROOT / "static"),
                "crests": kits.sync_crests(conn, PROJECT_ROOT / "static"),
            },
        )
        conn.commit()

        _stage(conn, results, "retention", lambda: run_retention(conn))
        conn.commit()

        elapsed = time.monotonic() - started
        results["seconds"] = round(elapsed, 1)
        results["finished_at"] = utcnow()
        failed = [k for k, v in results.items() if isinstance(v, dict) and v.get("ok") is False]
        results["failed_stages"] = failed
        set_meta(conn, "last_refresh_completed", utcnow())
        set_meta(conn, "last_refresh_seconds", round(elapsed, 1))
        set_meta(conn, "last_refresh_failed_stages", ",".join(failed))
        log_fetch(
            conn, "refresh", "full", not failed,
            f"{elapsed:.0f}s, failed stages: {failed or 'none'}",
        )
        conn.commit()
        log.info(
            "refresh finished in %.0fs (%s)",
            elapsed, f"failed: {', '.join(failed)}" if failed else "all stages ok",
        )
        return results
    finally:
        _HEARTBEAT.clear()
        _HEARTBEAT = None
        conn.close()
        REFRESH_LOCK.release()


def run_crowd_intel(cfg: Config | None = None, *, rebuild: bool = True) -> dict[str, Any]:
    """The crowd-intel job: YouTube + news -> Gemini -> stored adjustments.

    The only code path in the project that spends a Gemini call, and it is not
    reachable from `run_refresh` or the Refresh button — cron owns its schedule.

    Fresh adjustments are inert until something rebuilds on top of them, so by
    default this also re-runs projections. Pass rebuild=False to store the
    intel and leave the next refresh to pick it up.

    Returns a per-stage report; never raises.
    """
    cfg = cfg or load_config()
    if not REFRESH_LOCK.acquire():
        log.warning(
            "a refresh has been running since %s — skipping crowd intel",
            REFRESH_LOCK.running_since,
        )
        return {"skipped": True, "reason": "a refresh is already running"}

    started = time.monotonic()
    results: dict[str, Any] = {"started_at": utcnow()}
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    global _HEARTBEAT
    _HEARTBEAT = JobHeartbeat(cfg.db_path, "crowd")
    try:
        intel = _stage(
            conn, results, "crowd_intel",
            lambda: crowd_intel.refresh_crowd_intel(conn, cfg),
        )
        conn.commit()

        # refresh_crowd_intel reports failure by returning, not raising, so _stage
        # would otherwise record a no-key or dead-API run as a success.
        if isinstance(intel, dict) and not intel.get("ok"):
            results["crowd_intel"]["ok"] = False
            results["crowd_intel"]["error"] = intel.get("error") or "no adjustments produced"

        if not rebuild:
            log.info("--rebuild disabled: projections left as they are")
        elif isinstance(intel, dict) and intel.get("ok"):
            _stage(conn, results, "projections", lambda: rebuild_projections(conn, cfg))
            conn.commit()
            results["event"] = current_event(conn)
        else:
            log.warning("crowd intel produced nothing usable — projections left alone")

        # Runs whatever happened: the point is that yesterday's documents go
        # away once today's run has landed, successful or not.
        _stage(conn, results, "retention", lambda: run_retention(conn))
        conn.commit()

        elapsed = time.monotonic() - started
        results["seconds"] = round(elapsed, 1)
        results["finished_at"] = utcnow()
        failed = [k for k, v in results.items() if isinstance(v, dict) and v.get("ok") is False]
        results["failed_stages"] = failed
        set_meta(conn, "last_crowd_completed", utcnow())
        set_meta(conn, "last_crowd_seconds", round(elapsed, 1))
        set_meta(conn, "last_crowd_failed_stages", ",".join(failed))
        log_fetch(
            conn, "crowd_job", "full", not failed,
            f"{elapsed:.0f}s, failed stages: {failed or 'none'}",
        )
        conn.commit()
        log.info(
            "crowd intel job finished in %.0fs (%s)",
            elapsed, f"failed: {', '.join(failed)}" if failed else "all stages ok",
        )
        return results
    finally:
        _HEARTBEAT.clear()
        _HEARTBEAT = None
        conn.close()
        REFRESH_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the FPL data refresh.")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument(
        "--force-historical", action="store_true",
        help="re-pull the historical dataset even if it was synced recently",
    )
    parser.add_argument("--quiet", action="store_true", help="warnings and errors only")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(level=logging.WARNING if args.quiet else logging.INFO)
    results = run_refresh(cfg, force_historical=args.force_historical)

    if results.get("skipped"):
        print(f"Skipped: {results.get('reason')}")
        return 1
    failed = results.get("failed_stages") or []
    print(f"Refresh finished in {results.get('seconds')}s")
    for name, value in results.items():
        if isinstance(value, dict) and "ok" in value:
            mark = "ok  " if value["ok"] else "FAIL"
            detail = value.get("error") or value.get("result")
            print(f"  [{mark}] {name:<18} {str(detail)[:110]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
