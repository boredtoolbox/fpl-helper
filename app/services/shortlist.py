"""Squad Builder: the best few options in each position, and what the creators
say about them.

Two questions, one page, one set of numbers:

  * Who are the strongest seven options in each position for the gameweeks
    ahead?
  * Of the YouTube creators the user follows, who is telling them to buy, sell,
    keep, start or captain whom?

Both halves describe a player with the same figures — club, price, the fixture
run, and four per-game rates — so a name that turns up in both can be read
straight across without converting anything in your head.

Nothing here touches the network. The whole page is a handful of SQLite reads
and some arithmetic, redone per request rather than cached: the inputs are all
in the database already and the sums are cheaper than an invalidation rule.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Sequence

from ..db import jload
from .engine import POSITION_NAMES

GK, DEF, MID, FWD = 1, 2, 3, 4
POSITION_ORDER = (GK, DEF, MID, FWD)

# Seven per position: enough that the obvious picks have company and a
# differential can show up next to them, few enough to read four tables in one
# screen without scrolling.
TOP_N = 7

# Ten gameweeks, or however many are left if the season is nearly done.
HORIZON_DEFAULT = 10
HORIZON_MAX = 10
MIN_MINUTES_DEFAULT = 90

# --- The ranking ------------------------------------------------------------
#
# Four inputs, and deliberately only four:
#
#   performance  what the player has actually been scoring, per match
#   difficulty   how kind the run of fixtures ahead is
#   xPts         this app's own projection engine
#   EP           FPL's own expected points, as an outside opinion
#
# The three baselines are all "FPL points per match", so they blend without any
# rescaling. Performance is split evenly between form (the last 30 days) and
# points per game (the season), because one is the recent truth and the other is
# the memory that stops a single hot fortnight deciding everything.
W_PERF, W_EP, W_XPTS = 0.35, 0.30, 0.35
FORM_SHARE = 0.5

# Fixture difficulty as a multiplier on a per-match baseline. A 5 is worth about
# three-fifths of a 1, which is roughly the spread between the easiest and
# hardest fixtures in practice — enough to reorder a shortlist, not enough to
# put a poor player on it for one kind week.
FDR_MULTIPLIER = {1: 1.25, 2: 1.12, 3: 1.00, 4: 0.88, 5: 0.75}


def _fdr_multiplier(difficulty: Any) -> float:
    return FDR_MULTIPLIER.get(int(difficulty or 3), 1.0)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def horizon_events(conn: sqlite3.Connection, horizon: int) -> list[int]:
    """The next `horizon` gameweeks that still have football left in them.

    Taken from unfinished fixtures rather than `gameweeks.finished`, which stays
    0 for days after the last whistle while FPL runs its data checks — that flag
    would keep a gameweek that is over and paid out at the head of the run.
    Fewer than `horizon` come back near the end of a season, which is the
    "or remaining gameweeks" case and needs no special handling anywhere else.
    """
    return [
        r["event"] for r in conn.execute(
            "SELECT DISTINCT event FROM fixtures "
            "WHERE event IS NOT NULL AND finished = 0 ORDER BY event LIMIT ?",
            (horizon,),
        )
    ]


def fixture_runs(
    conn: sqlite3.Connection, events: Sequence[int]
) -> dict[int, list[dict[str, Any]]]:
    """club id -> one ticker cell per gameweek in the horizon.

    One cell per *gameweek*, not per fixture, so the runs line up column for
    column across a table: a club with nothing that week gets a blank cell
    rather than quietly borrowing the next week's fixture and looking like it
    has an easier run than it does. A double gameweek puts both matches in the
    one cell and is marked as such.
    """
    if not events:
        return {}
    marks = ",".join("?" for _ in events)
    per_team_event: dict[tuple[int, int], list[dict[str, Any]]] = {}

    sql = f"""
        SELECT f.event, f.team_h, f.team_a, f.team_h_difficulty, f.team_a_difficulty,
               th.short_name AS h_short, th.name AS h_name,
               ta.short_name AS a_short, ta.name AS a_name
        FROM fixtures f
        LEFT JOIN teams th ON th.id = f.team_h
        LEFT JOIN teams ta ON ta.id = f.team_a
        WHERE f.event IN ({marks}) AND f.finished = 0
        ORDER BY f.event, f.kickoff_time
    """
    for r in conn.execute(sql, tuple(events)):
        for team, opp_short, opp_name, home, difficulty in (
            (r["team_h"], r["a_short"], r["a_name"], True, r["team_h_difficulty"]),
            (r["team_a"], r["h_short"], r["h_name"], False, r["team_a_difficulty"]),
        ):
            if team is None:
                continue
            per_team_event.setdefault((int(team), r["event"]), []).append(
                {
                    "event": r["event"],
                    "opp": opp_short or "?",
                    "opp_name": opp_name or "?",
                    "home": home,
                    "difficulty": difficulty,
                }
            )

    teams = {team for team, _ in per_team_event}
    runs: dict[int, list[dict[str, Any]]] = {}
    for team in teams:
        cells: list[dict[str, Any]] = []
        for event in events:
            matches = per_team_event.get((team, event), [])
            if not matches:
                cells.append({"event": event, "blank": True})
                continue
            for match in matches:
                cells.append({**match, "double": len(matches) > 1})
        runs[team] = cells
    return runs


# --------------------------------------------------------------------------
# Per-game rates
# --------------------------------------------------------------------------
def appearance_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """player id -> matches they have actually appeared in this season.

    Two sources, because neither is complete on its own.

    Match history is only fetched for players whose club has played since the
    last run, prioritised and capped, so most of the game has rows for gameweek
    one and nothing since. But FPL's own `points_per_game` is total points
    divided by appearances, so the count can be read back out of it exactly —
    verified against the players whose history *is* complete, where the two
    agree to the match every time.

    The division only fails for a player sitting on exactly zero points, so the
    larger of the two wins: the derived figure where there are points, the
    counted rows where there are not.
    """
    counted: dict[int, int] = {
        r["player_id"]: r["n"]
        for r in conn.execute(
            "SELECT player_id, COUNT(*) AS n FROM player_gw_history "
            "WHERE minutes > 0 GROUP BY player_id"
        )
    }
    out: dict[int, int] = dict(counted)
    for r in conn.execute(
        "SELECT id, total_points, points_per_game FROM players "
        "WHERE points_per_game IS NOT NULL AND points_per_game <> 0"
    ):
        derived = round((r["total_points"] or 0) / r["points_per_game"])
        if derived > 0:
            out[r["id"]] = max(out.get(r["id"], 0), int(derived))
    return out


def crowd_by_player(conn: sqlite3.Connection) -> tuple[dict[int, dict[str, Any]], dict[str, Any] | None]:
    """The latest successful crowd run's per-player intel, and the run itself."""
    run = conn.execute(
        "SELECT id, run_at, n_documents, model FROM crowd_intel_runs "
        "WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run:
        return {}, None
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT player_id, player_name, availability, sentiment, confidence, "
        "n_sources, reasons, predicted_role_change FROM crowd_intel "
        "WHERE run_id = ? AND resolved = 1 AND player_id IS NOT NULL",
        (run["id"],),
    ):
        out[r["player_id"]] = {
            "player_name": r["player_name"],
            "availability": r["availability"] or "unknown",
            "sentiment": r["sentiment"] or 0.0,
            "confidence": r["confidence"] or 0.0,
            "n_sources": r["n_sources"] or 0,
            "reasons": jload(r["reasons"], []) or [],
            "role_change": r["predicted_role_change"],
        }
    return out, dict(run)


def player_rows(
    conn: sqlite3.Connection, events: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """Every player, scored across the horizon. Keyed by player id.

    Unfiltered on purpose: the shortlist wants only the fit and the played, but
    the creator section has to be able to describe whoever a creator named, and
    that is regularly someone injured or barely used. Filtering happens once, in
    `shortlist`, on a copy of this.
    """
    runs = fixture_runs(conn, events)
    apps = appearance_counts(conn)
    crowd, _ = crowd_by_player(conn)

    # The projection engine runs over its own horizon, which is shorter than
    # this page's. Only the part that overlaps is read; the rest is extrapolated
    # below, and the page says so.
    projected: dict[int, list[dict[str, Any]]] = {}
    if events:
        marks = ",".join("?" for _ in events)
        for r in conn.execute(
            f"SELECT player_id, event, xpts, difficulty FROM projections "
            f"WHERE event IN ({marks})",
            tuple(events),
        ):
            projected.setdefault(r["player_id"], []).append(dict(r))

    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT p.*, t.name AS team_name, t.short_name AS team_short "
        "FROM players p LEFT JOIN teams t ON t.id = p.team"
    ):
        pid = r["id"]
        cells = runs.get(r["team"] or -1, [])
        played = [c for c in cells if not c.get("blank")]
        games = apps.get(pid, 0)
        minutes = r["minutes"] or 0

        form = float(r["form"] or 0.0)
        ppg = float(r["points_per_game"] or 0.0)
        ep = float(r["ep_next"] or 0.0)
        perf = FORM_SHARE * form + (1.0 - FORM_SHARE) * ppg

        fixture_mult = _mean([_fdr_multiplier(c["difficulty"]) for c in played])
        fdr_avg = _mean([float(c["difficulty"] or 3) for c in played])

        legs = projected.get(pid, [])
        xpts_pg = _mean([float(leg["xpts"] or 0.0) for leg in legs])
        # xPts is already opponent-adjusted per fixture, so no difficulty
        # multiplier goes on top of it — that would count the same fixture
        # twice. What it does need is stretching from the engine's window to
        # this page's: the ratio of the two windows' average difficulty says how
        # much kinder (or harsher) the gameweeks the engine has not reached are.
        # When the horizon *is* the engine's window the ratio is 1 and this is a
        # no-op, which is the behaviour you want from a correction.
        proj_mult = _mean([_fdr_multiplier(leg["difficulty"]) for leg in legs])
        xpts_adj = xpts_pg * (fixture_mult / proj_mult) if proj_mult else 0.0

        per_match = (
            W_PERF * perf * fixture_mult
            + W_EP * ep * fixture_mult
            + W_XPTS * xpts_adj
        )

        status = r["status"] or "a"
        chance = r["chance_of_playing_next_round"]
        intel = crowd.get(pid)
        crowd_out = bool(intel and intel["availability"] in ("injured", "suspended"))

        out[pid] = {
            "id": pid,
            "name": r["web_name"],
            "full_name": f"{r['first_name'] or ''} {r['second_name'] or ''}".strip(),
            "team": r["team_short"] or "",
            "team_name": r["team_name"] or "",
            "team_id": r["team"],
            "position": r["element_type"],
            "position_name": POSITION_NAMES.get(r["element_type"], "?"),
            "price": (r["now_cost"] or 0) / 10.0,
            "fixtures": cells,
            "fixture_count": len(played),
            "fdr_avg": round(fdr_avg, 2) if played else None,
            "fixture_mult": round(fixture_mult, 3),
            # The four per-game rates. All divide by appearances rather than by
            # gameweeks played: a rotation option who has featured twice in six
            # weeks is being described by the two matches he played, not marked
            # down for the four he watched. `games` of zero leaves them at zero
            # rather than dividing by it — no football, nothing to rate.
            "games": games,
            "ppg": ppg,
            "mins_per_game": round(minutes / games, 1) if games else 0.0,
            "xg_per_game": round((r["expected_goals"] or 0.0) / games, 2) if games else 0.0,
            "xa_per_game": round((r["expected_assists"] or 0.0) / games, 2) if games else 0.0,
            "minutes": minutes,
            "starts": r["starts"] or 0,
            "form": form,
            "ep_next": ep,
            "total_points": r["total_points"] or 0,
            "ownership": r["selected_by_percent"] or 0.0,
            "xpts_per_game": round(xpts_pg, 2),
            "xpts_projected": len(legs),
            "score_per_match": round(per_match, 2),
            "score": round(per_match * len(played), 1),
            "status": status,
            "news": r["news"] or "",
            "chance": chance,
            "available": status == "a" or (chance is not None and chance >= 75),
            "crowd": intel,
            "crowd_out": crowd_out,
        }
    return out


# --------------------------------------------------------------------------
# The shortlist
# --------------------------------------------------------------------------
def shortlist(
    rows: Iterable[dict[str, Any]],
    min_minutes: int = MIN_MINUTES_DEFAULT,
    top_n: int = TOP_N,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    """Top `top_n` per position, plus what was left out and why.

    Three gates, in the order they cost the most players:

      * no fixture in the horizon — nothing to rank them over;
      * flagged unavailable by FPL, or reported injured or suspended by the
        crowd layer — the same rule the projection engine applies, so a player
        the engine has zeroed cannot appear here as a recommendation;
      * under the minutes floor — a rate off twenty minutes is not a fact about
        a player, and the floor is the page's one adjustable filter.
    """
    eligible: list[dict[str, Any]] = []
    excluded_crowd: list[dict[str, Any]] = []
    dropped = {"no_fixture": 0, "unavailable": 0, "minutes": 0}

    for row in rows:
        if not row["fixture_count"]:
            dropped["no_fixture"] += 1
            continue
        if not row["available"] or row["crowd_out"]:
            dropped["unavailable"] += 1
            # The interesting case: fit as far as FPL is concerned, but a source
            # the user follows says otherwise. Worth naming rather than silently
            # dropping — this is the crowd layer doing the job it exists for.
            if row["available"] and row["crowd_out"]:
                excluded_crowd.append(row)
            continue
        if row["minutes"] < min_minutes:
            dropped["minutes"] += 1
            continue
        eligible.append(row)

    eligible.sort(key=lambda p: (-p["score"], -p["score_per_match"], p["price"]))
    by_position = {
        pos: [p for p in eligible if p["position"] == pos][:top_n]
        for pos in POSITION_ORDER
    }
    excluded_crowd.sort(key=lambda p: -p["score"])
    return by_position, {
        # The whole eligible pool, not just the shortlisted, because the page
        # says things about it — how many players cleared the gates, and how
        # many of them have form, EP and points per game sitting on the same
        # number. Recomputing the gates outside here to answer that would be two
        # copies of the same three conditions.
        "pool": eligible,
        "eligible": len(eligible),
        "dropped": dropped,
        "excluded_crowd": excluded_crowd,
    }


# --------------------------------------------------------------------------
# What the creators said
# --------------------------------------------------------------------------
#
# The crowd layer stores one row per player per run, merged across every source
# that mentioned them, with each entry in `reasons` naming its own source —
# "Let's Talk FPL: expected to be rotated after midweek". That prefix is the
# only per-creator attribution there is, so it is what this reads.
#
# What it cannot read is a structured verdict, because nothing asks for one: the
# extraction schema captures availability, a start-probability hint and a
# sentiment score, all of them merged across sources. So the chip beside each
# line is a *reading* of that creator's own sentence, not something they were
# asked to declare, and the sentence itself is always shown next to it. Where
# the wording does not commit either way the chip says "mentioned" rather than
# guessing.

_QUOTE_SPLIT = re.compile(r"^\s*([^:]{2,60}?)\s*:\s*(.+)$", re.DOTALL)

# Order matters: the first pattern to match wins. Captaincy outranks everything
# because "keeping faith and captaining him" is a captain call, not a hold. The
# minutes warnings sit above "start" so that "may not start, a substitution
# risk" is not read as an endorsement of starting him.
VERDICT_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("captain", "Captain", r"captain|armband"),
    ("sell", "Sell", r"\bsell|shipp?(?:ing)?\s+(?:him\s+)?out|move\s+on\s+from|"
                     r"get(?:ting)?\s+rid|transfer(?:r?ing)?\s+(?:him\s+)?out|"
                     r"downgrad|\bavoid|steer\s+clear"),
    ("buy", "Buy", r"\bbuy|bring(?:ing)?\s+(?:him\s+)?in|transfer(?:r?ing)?\s+(?:him\s+)?in|"
                   r"(?:great|good|top|nice|solid|cheap(?:er)?|budget|value)\s+"
                   r"(?:pick|buy|option|target|punt)|must[-\s]have|getting\s+him"),
    ("risk", "Minutes risk", r"rotation\s+risk|substitut\w*\s+risk|\brotat|"
                             r"\bbench|minutes?\s+(?:risk|concern|doubt)|"
                             r"\d{2}[-\s]minute"),
    ("keep", "Keep", r"\bkeep|\bhold(?:ing)?\b|stick(?:ing)?\s+with|"
                     r"\bstay(?:s|ing)?\b|not\s+selling|\bretain"),
    ("start", "Start", r"\bstart(?:s|ing|er)?\b|\bnailed\b|in\s+the\s+(?:team|xi|side)|"
                       r"first\s+choice|\bplay(?:s|ing)\b"),
)
_COMPILED_VERDICTS = tuple(
    (key, label, re.compile(pattern, re.IGNORECASE))
    for key, label, pattern in VERDICT_PATTERNS
)

# How the chips sort inside one creator's table: the calls that change a squad
# first, the bare mentions last.
VERDICT_RANK = {
    "captain": 0, "buy": 1, "sell": 2, "keep": 3, "start": 4,
    "risk": 5, "out": 6, "doubt": 7, "mention": 8,
}


def read_verdict(quote: str, availability: str) -> tuple[str, str]:
    """(key, label) for what one creator's line amounts to.

    The wording is tried first, so the chip describes what this creator said
    rather than what every source combined implies. Availability is the
    fallback, and it is worth having: "still no sign of him in training" carries
    no verdict word at all, but the player is flagged injured and "out" is the
    honest summary of it.
    """
    for key, label, pattern in _COMPILED_VERDICTS:
        if pattern.search(quote):
            return key, label
    if availability in ("injured", "suspended"):
        return "out", "Out"
    if availability == "doubt":
        return "doubt", "Doubt"
    return "mention", "Mentioned"


def _match_key(name: str) -> str:
    """Loose key for comparing source names: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def configured_creators(conn: sqlite3.Connection, cfg) -> list[dict[str, Any]]:
    """The user's creators in their configured rank order, with last-run status.

    Rank is load-bearing elsewhere — it decides who keeps their transcript slot
    when the budget runs out — so the page shows them in that order too rather
    than by how much they happened to say.

    `crowd_channel_status` is written by the gatherer on every run and records
    how many videos and transcripts each channel actually yielded. Without it a
    channel that went silent, or whose transcripts were blocked, is
    indistinguishable from one that simply had no opinions.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'crowd_channel_status'"
    ).fetchone()
    status_rows = jload(row["value"] if row else None, []) or []
    by_source = {str(s.get("source")): s for s in status_rows if isinstance(s, dict)}

    resolved = {
        r["source"]: r["name"]
        for r in conn.execute("SELECT source, name FROM youtube_channels")
    }

    from .crowd_intel import normalise_channel

    creators: list[dict[str, Any]] = []
    for rank, entry in enumerate(cfg.youtube_channels or [], start=1):
        channel = normalise_channel(entry)
        if channel is None:
            continue
        status = by_source.get(channel.source, {})
        name = status.get("name") or resolved.get(channel.source) or channel.name
        creators.append(
            {
                "rank": rank,
                "name": name,
                "source": channel.source,
                "state": status.get("state"),
                "videos": status.get("videos"),
                "transcripts": status.get("transcripts"),
                "mentions": [],
            }
        )
    return creators


def creator_board(
    conn: sqlite3.Connection, cfg, rows: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """Per-creator verdicts from the most recent crowd run.

    Every mention carries the player's full row, so the table under a creator
    shows exactly the same figures as the shortlists above it.
    """
    creators = configured_creators(conn, cfg)
    by_key = {_match_key(c["name"]): c for c in creators}
    crowd, run = crowd_by_player(conn)

    unattributed = 0
    other_sources: dict[str, int] = {}

    for pid, intel in crowd.items():
        row = rows.get(pid)
        if row is None:
            continue
        for reason in intel["reasons"]:
            parsed = _QUOTE_SPLIT.match(str(reason))
            if not parsed:
                unattributed += 1
                continue
            source, quote = parsed.group(1), parsed.group(2).strip()
            creator = by_key.get(_match_key(source))
            if creator is None:
                # Official flags and the news feeds land here. They are not
                # creators and are not what this section is about, but counting
                # them keeps the totals on the page honest.
                other_sources[source] = other_sources.get(source, 0) + 1
                continue
            key, label = read_verdict(quote, intel["availability"])
            creator["mentions"].append(
                {
                    "player": row,
                    "quote": quote,
                    "verdict": key,
                    "verdict_label": label,
                    "availability": intel["availability"],
                    "sentiment": intel["sentiment"],
                    "confidence": intel["confidence"],
                    "n_sources": intel["n_sources"],
                    "role_change": intel["role_change"],
                }
            )

    for creator in creators:
        creator["mentions"].sort(
            key=lambda m: (VERDICT_RANK.get(m["verdict"], 9), -m["player"]["score"])
        )
        creator["count"] = len(creator["mentions"])

    return {
        "creators": creators,
        "run": run,
        "unattributed": unattributed,
        "other_sources": sorted(
            ({"name": k, "n": v} for k, v in other_sources.items()),
            key=lambda s: -s["n"],
        )[:6],
        "total": sum(c["count"] for c in creators),
    }
