"""What we keep, and why.

The Pi runs off an SD card, so a table that grows without limit is a slow leak
rather than a harmless one. Two areas grow with every run and nothing ever
trimmed them:

  * crowd intel — the big one. A run stores every source document, and a
    YouTube transcript is truncated at 100k characters, up to 40 documents per
    run. Kept forever that is hundreds of megabytes a season.
  * fetch_log — roughly 200 rows per refresh, around 55k a season.

The rule for crowd intel is *replace*, not *archive*: the projection engine
reads the newest successful run and nothing else, so once a new run lands the
previous one is dead weight. The one exception is a failed run, which is kept —
without its documents — so its error message survives.

Everything here is called with the job's own connection and commits nothing;
the caller owns the transaction, so a failure rolls back with the rest.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)

# fetch_log is diagnostic only — a season of history is far more than anyone
# reads, but a fortnight is too short to investigate "it broke sometime last month".
FETCH_LOG_KEEP_DAYS = 90


def prune_crowd_history(conn: sqlite3.Connection) -> dict[str, int]:
    """Keep the newest successful run, and the newest run whatever its outcome.

    `latest_adjustments` takes the newest `ok = 1` run to feed the projection
    engine; the newest run of any kind is kept alongside it so a failure still
    has its error message. When the newest run succeeded they are the same row
    and only one survives.
    """
    keep: set[int] = set()
    for sql in (
        "SELECT id FROM crowd_intel_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1",
        "SELECT id FROM crowd_intel_runs ORDER BY id DESC LIMIT 1",
    ):
        row = conn.execute(sql).fetchone()
        if row:
            keep.add(int(row["id"]))

    removed = {"crowd_intel_runs": 0, "crowd_intel": 0, "crowd_documents": 0}
    if not keep:
        return removed

    marks = ",".join("?" for _ in keep)
    params = tuple(sorted(keep))
    # Children first: an orphaned row is harder to reason about than a missing one.
    for table in ("crowd_documents", "crowd_intel"):
        removed[table] = conn.execute(
            f"DELETE FROM {table} WHERE run_id NOT IN ({marks})", params
        ).rowcount
    removed["crowd_intel_runs"] = conn.execute(
        f"DELETE FROM crowd_intel_runs WHERE id NOT IN ({marks})", params
    ).rowcount

    # A failed run is kept for its error message alone. Its documents are just a
    # copy of what we were about to send, and they are the bulk of the storage.
    removed["crowd_documents"] += conn.execute(
        "DELETE FROM crowd_documents WHERE run_id IN "
        "(SELECT id FROM crowd_intel_runs WHERE ok = 0)"
    ).rowcount
    return removed


def prune_fetch_log(conn: sqlite3.Connection, keep_days: int = FETCH_LOG_KEEP_DAYS) -> int:
    """Drop diagnostic fetch rows older than `keep_days`."""
    return conn.execute(
        "DELETE FROM fetch_log WHERE fetched_at < datetime('now', ?)",
        (f"-{int(keep_days)} days",),
    ).rowcount


def run_retention(
    conn: sqlite3.Connection, *, keep_days: int = FETCH_LOG_KEEP_DAYS
) -> dict[str, Any]:
    """Both policies. Returns what was removed, for the stage report."""
    crowd = prune_crowd_history(conn)
    result: dict[str, Any] = {
        "crowd_runs": crowd["crowd_intel_runs"],
        "crowd_documents": crowd["crowd_documents"],
        "crowd_adjustments": crowd["crowd_intel"],
        "fetch_log": prune_fetch_log(conn, keep_days),
    }
    total = sum(v for v in result.values() if isinstance(v, int))
    if total:
        log.info(
            "retention: removed %d rows (%s)",
            total, ", ".join(f"{k}={v}" for k, v in result.items() if v),
        )
    return result
