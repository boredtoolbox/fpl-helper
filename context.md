# Project context

Handoff notes for whoever (or whatever) picks this up next. `README.md` covers
*how to run it*; this file covers *what was asked for, what got built, and the
things that aren't obvious from reading the code*.

Built: 21 August 2026.

---

## 1. The brief

Build a Python FPL analysis web app for the **2026/27 season**, running on a
Raspberry Pi. Constraints given up front:

- Flask + SQLite + APScheduler. No Docker, no heavy frameworks, minimal deps.
- Python 3.11+.
- Four pages plus a daily background refresh:
  1. **My Squads** — current squad(s), selector for two team IDs
  2. **My Stats** — GW points, overall rank, per-classic-league standing
  3. **Decision Engine** — xPts projections, best XI, captain/vice, bench order, transfers
     *(removed September 2026 — see the note at the end of this section)*
  4. **Player Stats Explorer** — sortable/filterable table of underlying stats
- **Hybrid AI principle, stated explicitly:** Gemini handles natural language;
  Python handles all FPL maths and constraints. Gemini never picks the team — it
  emits structured adjustments that Python applies, under hard caps.
- Build incrementally: DB + API sync → pages → explorer → engine + optimizer →
  Gemini layer last, as an optional enhancement.
- Run the tests and do an end-to-end smoke run with mocked network before finishing.

Everything in the brief was delivered. Nothing was descoped.

**Since removed (8 September 2026), at the owner's request:** the Decision
Engine page (`/engine`), the Gameweek Plan page (`/plan`), and the optimiser and
planner behind them (`app/services/optimizer.py`, `app/services/planner.py`,
the `decisions` table, the PuLP dependency). Something else is being built in
their place. The *projection* engine (`app/services/engine.py`) stayed — the
squad and explorer pages both read its xPts — as did the crowd layer that feeds
it. Everything about the optimiser below is history, not description; `git show
7479d6b` has the code if it is ever wanted back.

**Added (12 September 2026):** a **Matches** page, first in the nav, ahead of
My Squads — the league's own fixtures and results rather than anything about the
owner's squad. It brought a new `fixture_stats` table with it; see "Match events
come from `fixture_stats`" in section 3, which is the one non-obvious thing
about it.

**Club crests come from the league's badge CDN, not FPL's**, and are keyed by
the team `code` with a `t` in front:
`resources.premierleague.com/premierleague/badges/70/t{code}.png`. The
`premierleague25/` path some pages use 403s. 70px for a crest drawn at 22px, so
it is sharp at 3x without paying for the SVG — twenty complex vector badges on
one page is real work for a phone, and this page draws twenty. Fetched into
`static/crests/` by the same stage as the shirts, which is now called `images`
rather than `kits`.

**One gameweek per URL** (`/matches/<gw>`), at the owner's request and modelled
on premierleague.com's own fixture pages: a step either side, the whole season
as a scrolling strip between them, and matches grouped by the day they are
played on. `/matches` *redirects* rather than renders, so the gameweek you are
looking at is always in the address bar. The first cut stacked "all upcoming"
and "last results" on one page with two independent controls; it was rejected
for exactly that reason.

Two things about it that are easy to get wrong:
- **Gameweek state comes from the fixtures, not `gameweeks.finished`**, which
  stays 0 for days after the last whistle. `matchweek_index()` derives
  played/live/upcoming from started and finished fixture counts in one query —
  38 calls to `gameweek_progress()` would be 76 queries for one navigation bar.
- **Days are grouped after converting to local time.** A 20:00 UTC Saturday
  kickoff is Sunday morning in Singapore, and grouping on the UTC string would
  head it with the wrong day for the person reading it.

**Removed again (12 September 2026), same reason:** the first Squad Builder and
the Predicted XI page, and `app/services/selector.py` /
`app/services/predictor.py` behind them. Squad Builder was rebuilt from scratch
on the same URL; Predicted XI is gone with nothing in its place. What went with
them: the local-search squad selection, the head-to-head-against-this-opponent
multiplier, the three-baseline tab comparison, chip state, derived free
transfers and transfer planning. None of it is referenced anywhere any more —
`git show 7479d6b` predates it, so the only copy is the working tree before this
change.

**The Squad Builder that replaced it** (`app/services/shortlist.py`,
`templates/builder.html`) answers two questions on one page: the best seven
options in each position over the next ten gameweeks, and what each configured
YouTube creator is saying about players. Both halves describe a player with the
same seven figures — club, price, the fixture run, points per game, minutes per
game, xG per game, xA per game — which is the point: a name in both sections
reads straight across. See "The per-creator verdicts are inferred" below for the
one thing about it that is not exact.

Two changes landed with that removal:

- **The team selector moved onto the pages.** It used to sit in the top bar and
  appear on two pages only, which left the explorer with no answer to "whose
  squad is this?". Every page now carries its own selector, the choice is
  remembered in the session, and My Stats shows one team at a time instead of
  stacking every configured team down the page.
- **Player Stats rows expand.** A caret by the name opens the club's last five
  results against each of the next six opponents, and the player's own goals,
  assists and DefCon in the games he played against them. Fetched on demand from
  `/api/player/<id>/opponents`; see `opponent_history` in `app/views.py`.

---

## 2. Current state

**All four pages work, 147 tests pass, full refresh runs clean end to end
(~230s) against the live FPL API with both real teams configured.**

Team IDs live in `config.yaml`, which is gitignored;
`config.example.yaml` carries placeholders. It was developed against two real
entries, shaped like this:

| Entry ID | Team name |
| --- | --- |
| 1234567 | Main squad |
| 7654321 | Second squad |

The Gemini key is in `secrets/gemini_key.txt` (gitignored, chmod 600) and the
crowd layer is **live and verified end to end**: 17 source documents →
55 players resolved, 0 unresolved, 28 projections carrying a crowd adjustment.
Model: `gemini-3.5-flash-lite`.

### The Doku case — read this before changing the ratification rules

The clearest example of what the crowd layer is for, and of its one sharp edge:

| Source | Verdict |
| --- | --- |
| Official FPL | `status=d`, "Calf injury — **75% chance of playing**" |
| Fantasy Football Scout | came off at half-time in the Community Shield, out for **"several weeks"** |

The crowd layer escalated doubt → injured and took Doku from **1.45 → 0.00
xPts**. FFS is almost certainly right (FPL's flags lag reality), so this is the
layer earning its keep — it caught something the official flag understated.

But note it did so on **one source**, overriding a 75% official flag. Sentiment
requires ≥2 sources; the injury path does not. That asymmetry is per the brief
and errs safe, but it is the thing most likely to want tuning. Two-line change in
`apply_crowd_adjustment` to require 2+ sources or official corroboration. The
user has been shown this and has not asked for the change.

Also verified: **0 sentiment nudges were applied** — the ≥2-source gate filtered
every one of them out. The caps work.

**Nothing private is committed.** `config.yaml`, `secrets/`, `data/` and
`logs/` are all in `.gitignore`, so a clone carries the code and
`config.example.yaml` but never a key, a database or an entry ID.

### Season timing matters a lot right now

At time of building it was **GW1 of 2026/27, before the first deadline**
(2026-08-21 17:30 UTC). Consequences that shaped real design decisions:

- **FPL publishes no squad until a deadline passes.** `entry/{id}/event/1/picks/`
  returns nothing. So the Decision Engine **drafts an optimal 15 from scratch**
  on a £100.0m budget instead (`build_initial_squad`), using the same MILP with
  nothing owned. `decision["drafted"]` flags this and the UI explains it.
  Once picks exist, it switches to real transfer planning automatically.
- **Pre-season, `bootstrap-static` per-player stats are LAST season's totals.**
  Haaland showed 27 goals / 2953 minutes before a ball was kicked. Don't treat
  those as current-season data. The engine sources rates from match-level tables
  and only falls back to bootstrap aggregates.
- **League standings are empty pre-season.** `standings.results` is `[]` and
  every member sits under `new_entries` instead. `league.rank_count` is absent.
  "0 of my entries found" in `fetch_log` is correct, not a bug. The stats page
  says so rather than showing bare dashes.

Re-check these assumptions once the season is properly underway.

---

## 3. Non-obvious findings (the expensive knowledge)

These cost real investigation. Don't re-derive them.

**DefCon data only exists from 2025/26.** The 2024-25 season in the vaastav
dataset has `defensive_contribution = 0` on every row — the stat didn't exist.
Averaging both seasons **halves every DefCon rate**. `seasons_with_defcon()`
filters them out; all DefCon maths must go through it. Gabriel's reliability was
0.20 with the bug, 0.40 without.

**DefCon field composition, verified against 29,757 rows:**
- DEF: `defensive_contribution` = CBIT + tackles → threshold **10**
- MID/FWD: = CBIT + tackles + recoveries → threshold **12**
- GK: no DefCon points
So the `defensive_contribution` column can be used directly with a
position-based threshold; no need to recompute from components.

**Historical player identity uses `code`, not `id`.** Each season's
`players_raw.csv` maps that season's `id` → the stable `code`, which matches
`players.code` in bootstrap. This gives **100% exact mapping** across seasons —
no fuzzy matching needed for historical data. (Fuzzy matching is only used for
crowd-intel names, which arrive as free text.)

**`master_team_list.csv` is stale** — it only runs to 2023-24. Use each season's
own `data/<season>/teams.csv` for opponent id → name. `load_team_map()` prefers
it and falls back.

**Sparse checkout is cone-mode**, so it takes directories only, not file paths.
Files sitting directly in a parent directory come along automatically.

**Team xG aggregation:** per fixture, team xG = `SUM(expected_goals)` over that
team's players, but team xGC = `MAX(expected_goals_conceded)` — the latter is
prorated by minutes played, so summing it is wrong.

**Availability ≠ starting.** These must stay separate. An injured player has
both at zero; a fit rotation option has `p_available` near 1 and `p_start` low,
and earns cameo minutes from the gap. Conflating them made injured players
project points. See `Availability.p_available`.

**`datetime.now().astimezone().tzinfo` stringifies to a UTC offset** like
`"+08"`, which `zoneinfo` cannot resolve. Passing it to APScheduler crashes the
app **only under a real WSGI server** — the Flask dev server masked it. Use
`tzlocal.get_localzone()`. This is why the systemd `ExecStart` was tested for
real rather than assumed.

**Resolving an @handle to a channel id is a trap.** A YouTube channel page
mentions many `"channelId"` / `"externalId"` values — recommended channels,
featured shelves — and **the first one is usually not this channel**. Matching
the first occurrence silently resolved `@LetsTalkFPL` to "Let's Talk Football"
and `@FPLFocal` to a dormant 245-subscriber trivia channel, and the app happily
ingested strangers' videos with no error anywhere. Only three markers are
authoritative, and `scrape_channel_id` now trusts nothing else:
`<link rel="canonical">`, `<meta property="og:url">`, and `"rssUrl"`. If none are
present it returns None rather than guessing. Regression test:
`test_the_canonical_link_wins_over_decoy_channel_ids` (verified to fail against
the old regex). **Sanity-check any new channel by its newest video date** — a
months-old upload from an FPL channel means the wrong id.

**The five configured creators, in the user's rank order** (verified correct,
all posting daily):
| # | Creator | Channel id |
| --- | --- | --- |
| 1 | Let's Talk FPL | `UCxeOc7eFxq37yW_Nc-69deA` |
| 2 | Fantasy Football Hub | `UCcqEr3DfrRwtoF2a1yW8qgQ` |
| 3 | FPL Harry | `UCcPWnCj5AKC19HaySZjb25g` |
| 4 | FPL Focal | `UC72QokPHXQ9r98ROfNZmaDw` |
| 5 | FPL Mate | `UCweDAlFm2LnVcOqaFU4_AGA` |

Rank is load-bearing: round-robin runs down this order so a squeezed budget costs
the lowest rank first, and `build_prompt` declares the trust order to Gemini so
disagreements are weighted and reported. Official FPL still outranks all of them
on availability.

**Creator config takes URLs, not channel ids.** `youtube_channels` accepts a
channel URL, an `@handle`, a bare `UC...` id, or a dict with `name`/`url`.
Resolution scrapes the channel page once and caches to the `youtube_channels`
table, so it costs one fetch ever, not one per refresh. The user curates ~5
creators deliberately, so:
- creators are gathered **before news** in `gather_documents` — `crowd_max_documents`
  truncates the tail, and news must never displace them;
- transcripts are taken **round-robin** across channels, so one prolific creator
  can't consume the whole budget;
- per-channel status is written to `meta.crowd_channel_status` and read by the
  Squad Builder's creator panels, so a silent channel says why it is silent.
Don't reorder those without understanding why.

**YouTube IP-blocks transcript bursts.** ~19 requests in quick succession
triggered a block that lasted the rest of the session. The gatherer now spaces
requests (2s), caps them (12/run), and aborts on the first block rather than
hammering. Config: `crowd_max_transcripts`, `crowd_max_videos_per_channel`,
`crowd_transcript_delay`.

**Gemini model names retire.** `gemini-2.5-flash-lite` (the brief's default)
returns 404 for new keys: *"no longer available to new users, use
models/gemini-3.5-flash-lite"*. Now set to **`gemini-3.5-flash-lite`**, verified
working. The API's own 404 message names the replacement — read it and update
`gemini_model` in `config.yaml`.

**Mohamed Salah is not in the 2026/27 FPL player list.** He left for Trabzonspor.
When crowd intel reports him "unresolved", that is correct. Same for Musiala,
Gnabry, Raskin, Devlin, Maloney — all non-PL players from the BBC feed. The
matcher refusing to guess is the designed behaviour.

**Bootstrap has occasional junk**, e.g. Meslier (a GK, 0 minutes) reporting
`goals_scored = 11`. Displayed as-is; not worth "fixing" by inventing data.

**Match events come from `fixture_stats`, never from `player_gw_history`.**
The `fixtures/` endpoint carries a `stats` array per fixture — goals, assists,
own goals, cards, saves, bonus, split h/a — and `sync_fixtures` fetched and
discarded it until 12 September 2026. It is the only complete source: the
history table's coverage is a per-run budget (see below), and it records an own
goal against the player who scored it rather than the side it counted for, so
five of the first thirty fixtures could not be reconciled with their own
scoreline. Off `fixture_stats`, 30 of 30 reconcile. Rows are deleted and
rewritten per started fixture, not upserted — a stat line can be *withdrawn*
(goal reassigned, red card rescinded, bonus recalculated after the provisional
round) and an upsert would strand it.

**`player_gw_history` is not a complete table and never will be.**
`summary_priority()` only fetches element-summaries for players whose club has
played since the last run, prioritised and capped, so in practice every player
has a gameweek-one row and only ~180 have anything after it. Anything that
counts appearances off it will be wrong for most of the game.

**Appearances can be read exactly out of `points_per_game`.** FPL's own figure
is total points ÷ matches appeared in, so `round(total_points / points_per_game)`
recovers the count — checked against every player whose match history *is*
complete, and it agrees to the match every time. It only fails for a player on
exactly zero points, where the counted rows are the fallback. This is what the
Squad Builder's per-game rates divide by; dividing by gameweeks played instead
would mark down every rotation option for the matches he watched.

**The per-creator verdicts on the Squad Builder are inferred, not recorded.**
The extraction schema has no verdict field: Gemini returns one merged row per
player (availability, a start hint, a sentiment score, a role note) and the only
per-source attribution anywhere is the `"Creator: what they said"` prefix each
`reasons` entry is instructed to carry. So the buy/sell/keep/captain chip is a
regex read of that creator's own sentence — `read_verdict()` in
`shortlist.py` — and the sentence is always printed beside it so the reader can
overrule it. Sentiment and the role note are *player*-level and merged across
sources, so they are shown in their own column rather than on a creator's row;
attributing them to one creator would be a lie, and an early draft did exactly
that. If exact verdicts are ever wanted, the fix is a `verdict` field per source
in the response schema plus the prompt rule to fill it — which costs a crowd
re-run before anything shows.

**`crowd_channel_status` existed for weeks with nothing rendering it.** Written
on every gather, never read. The Squad Builder's creator panels are now the only
thing that show it — in the empty state, which is the only place that can tell a
channel that went quiet from one whose transcripts YouTube blocked. A summary
table of the same data shipped and was dropped at the owner's request on
12 September 2026; the per-panel empty states carry what mattered in it, so
`videos` and `total` on the board are now computed and unused.

---

## 4. Architecture

```
app/
  __init__.py       app factory + APScheduler (tzlocal!)
  config.py         config.yaml loading, key read at call time (no restart to rotate)
  db.py             21-table schema, REFRESH_LOCK
  refresh.py        stage orchestration + `python -m app.refresh`
  routes.py         Flask routes — NEVER touch the network
  views.py          read-only queries backing the pages
  logging_setup.py  rotating logs; FPL_LOG_DIR env override
  services/
    fpl_api.py      rate-limited client (1 req/sec, backoff, real UA)
    historical.py   vaastav clone/pull, season load, head-to-head
    crowd_intel.py  sources → Gemini → ratification
    engine.py       xPts projection (pure maths, no AI)
    shortlist.py    Squad Builder: per-position scoring + the creator board
    kits.py         team shirts + crests, fetched once into static/
templates/  base + 5 pages + _macros.html
static/     style.css, app.js (sort/filter/expand, no framework)
tools/      find_channel_id.py (resolve @handle → UC... id)
```

**Hard rule: pages read SQLite only.** The refresh job is the sole network
caller, plus the manual "Refresh now" button (guarded by `REFRESH_LOCK`).

### Refresh stages
Each is independently fault-tolerant via `_stage()` — a failure logs and the
rest continues:
`bootstrap → fixtures → my_teams → leagues → element_summaries → historical
(weekly) → crowd_intel → projections → images → retention`

Crowd intel runs **before** projections so adjustments land in the same run.

### The ratification contract (the safety boundary)
| Signal | Effect |
| --- | --- |
| `injured`/`suspended`, or official 0% | `p_start → 0`, `p_available → 0` |
| `doubt` | capped at **min**(model, crowd hint, official %) |
| positive sentiment | can **never** raise a start probability |
| sentiment | ±10% on xPts max, scaled by confidence, **≥2 sources required** |

Every adjustment is stored with its reasons so the UI can show `6.2 → 0.0`
alongside the cause. If the crowd layer is ever extended, keep these caps.

---

## 5. Design language

Deliberate, not incidental. The palette is derived from **FPL's own fixture
difficulty scale** (`--fdr-1` … `--fdr-5`), because that's the colour language
managers already read fixtures in.

**Refreshed 12 September 2026** — the owner said the first cut looked drab, and
it did: #0f1720 ground, 3px radii, no depth anywhere, so every surface was a
flat rectangle of the same grey. What changed, and the rules that came with it:

- **Two accents, split by job.** Amber (`--accent`) is interaction and status
  only — armband, active nav, focus, the live dot. Indigo (`--accent-2`) is
  decoration only — wordmark, eyebrows, the ambient glow, row hover. Because
  indigo never means anything it can never be confused with a difficulty or a
  warning, which is what lets the page have a second colour at all. Do not give
  indigo a meaning.
- **Every tint is `rgba(var(--x-rgb), a)`.** The first cut had ~25 hard-coded
  rgba literals derived from the palette; changing a colour left its fifteen
  washes behind. The `--x-rgb` channel vars exist so that cannot happen again —
  if you add a tint, add it that way.
- **Depth is two levels, `--shadow` and `--shadow-lg`,** plus a 1px `--edge`
  highlight along the top of panels and ticker cells. That highlight is doing
  most of the work; without it the surfaces go flat again.
- `.panel` gained `overflow: hidden` so a bench strip or a hovered last row is
  clipped to the 9px radius. Safe for sticky table headers — their scroll
  container is `.table-scroll` inside the panel, not the panel.

The **fixture ticker** — one cell per upcoming GW, 🏠/✈️ for venue — is the
signature element and appears on every page. It was originally the 25th column
of the player table and invisible; it now sits fourth, right after Team. Keep it
prominent.

Fonts: Space Grotesk (display) / IBM Plex Sans (body) / IBM Plex Mono (figures),
via Google Fonts with system fallbacks so it degrades offline on the Pi.

---

## 6. Known limitations / things a future session might tackle

- **Single-source injury zeroing.** Sentiment needs ≥2 sources, but one
  `injured` report zeroes a player outright. That's per the brief and errs safe,
  but a creator speculating could bench someone good. Offered to make it require
  2+ sources or official corroboration; user hasn't asked yet. Two-line change in
  `apply_crowd_adjustment`.
- **Crowd output is currently official-flag-heavy.** With YouTube transcripts
  missing (IP block during testing), 27 of the 28 adjustments merely corroborated
  official flags Python already handles; only Doku was a genuine new catch. The
  bigger value-add — predicted lineups, set-piece takers, rotation hints — lives
  in the transcripts. Re-check the mix once the block clears and transcripts flow.
- **`element_summaries` returns 0 rows pre-season** — there are no matches yet.
  Expect this to become the slowest stage once the season runs.
- **Free transfers are no longer estimated anywhere** — the code that
  reconstructed them from `event_transfers` went with the Predicted XI page.
  Nothing in the app reads them now.
- **No h2h / draft league support.** Classic leagues only, as briefed.
- **The Squad Builder's verdict chips are a regex read of a sentence**, because
  the crowd schema records no structured verdict per source. See section 3. The
  quote is always shown beside the chip, so a bad read is visible rather than
  silent, but "mentioned" turns up more often than it would if Gemini were asked
  for the verdict directly.
- **The Squad Builder's horizon outruns the projection engine's.** The page
  scores ten gameweeks; `horizon_gws` is 6, so xPts covers the first six and is
  stretched across the rest by the ratio of the two windows' average FDR. Raising
  `horizon_gws` to 10 would make the correction a no-op and the xPts component
  exact, at the cost of a longer projection stage. Not done because nobody asked
  and the refresh is already ~230s.
- **Head-to-head-against-this-opponent is no longer used by any page.** It was a
  signal in the old Squad Builder and went with it; `last_five_vs_opponent` went
  too. The expandable rows on Player Stats still show per-opponent history, but
  nothing scores on it any more.
- It was developed against a system Python with deps in `~/.local`
  (`--break-system-packages`). The README's install instructions use a
  virtualenv instead, which is what a fresh Linux box should do.

---

## 7. Quick commands

```bash
python3 run.py                                  # start the web app (prints the URL)
python -m pytest tests/ -q                      # if you have a tests/ dir; it is gitignored
python -m app.refresh                           # full refresh (~230s)
python -m app.refresh --force-historical        # re-pull vaastav dataset
python tools/find_channel_id.py @LetsTalkFPL    # resolve a YouTube channel
```

Inspect what the crowd layer decided:
```bash
sqlite3 data/fpl.db "SELECT player_name, availability, n_sources, confidence
                     FROM crowd_intel WHERE resolved=1 ORDER BY id DESC LIMIT 20;"
```

Note: `context.md` is not auto-loaded by Claude Code. Rename or symlink it to
`CLAUDE.md` if you want it picked up automatically at session start.
