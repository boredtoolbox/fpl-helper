# FPL Helper

A self-hosted Fantasy Premier League analysis app. Flask + SQLite +
APScheduler, with no Docker, no build step, no JavaScript framework and no
database server to run.

It projects expected points for every player, optionally sanity-checks those
projections against what FPL creators and the football press are saying, and
ranks the strongest options in each position under the real FPL constraints.

The division of labour is fixed: Python does all the football mathematics, and
the Gemini API only reads natural language and returns structured data. The AI
never picks your team. It can lower a start probability, cap a doubt, or nudge
expected points by at most ±10%, and every one of those adjustments is stored
with the reason that produced it.

It runs on any Linux box with Python 3.11+. It is small enough for a Raspberry
Pi 4 and idles at well under 200 MB of RAM, so a £5/month VPS or an old laptop
in a cupboard is more than enough. Everything below is written for a generic
Linux install, with Pi-specific notes where the two differ.

---

## Pages

| Page | What it's for |
| --- | --- |
| **Matches** (`/matches/<gw>`) | The league's own fixtures and results, rather than anything about your squad. **One gameweek per page**, laid out like the Premier League's own fixture pages: club crests either side of the score, a step either side of the bar and the whole season as a strip between them, matches grouped by the day they are played on. Before kickoff, the time and each side's difficulty rating; after it, the scoreline with goalscorers, assists and cards. `/matches` redirects to whichever gameweek is being played, or the one next up. |
| **My Squads** (`/`) | Your current squad on a pitch, in real club kits, split into keeper / defence / midfield / attack. Per player: what they actually scored that gameweek, their projection, fixture difficulty and availability. Up top: the next transfer deadline, what the squad scored, and what the XI is projected to score next. |
| **My Stats** (`/stats`) | Gameweek points and ranks, plus your position in every classic league you've joined. |
| **Squad Builder** (`/builder`) | Two sections. First, the strongest **seven options in each position** for the next ten gameweeks (or however many are left), ranked on what they have been scoring, the difficulty of the run ahead, this app's xPts and FPL's own EP. Second, **what your YouTube creators are saying**: who each of them is telling you to buy, sell, keep, start or captain, in your configured trust order. Both halves describe a player with the same figures: club, price, the fixture run, points per game, minutes per game, xG per game and xA per game. |
| **Player Stats** (`/players`) | Every player, sortable and filterable, with the underlying numbers: xG, xA, xGI, xGC, DefCon reliability, minutes, form, price, ownership and points per million. Each row opens on a per-opponent history, described below. |

Matches is the one page with no team selector, because it is about the league
rather than about you. Every other page carries its own **team selector** at the
top, so switching squad is one click from wherever you are and the choice
follows you between pages. On Player Stats it marks the players you already own
and can filter down to them.

The fixture ticker, six cells coloured by FPL difficulty, appears throughout;
italics mean an away fixture.

### The history behind the fixtures

The caret next to a name on Player Stats opens two rows of context for the next
six fixtures, fetched on demand rather than shipped with the table:

* **The club's record**: up to the last five meetings with each upcoming
  opponent, as a result and a scoreline.
* **The player's own record**: goals, assists and defensive contributions in
  the games he actually played against them, with the minutes behind each.

A club that has no history against an opponent says so, and says *why*: newly
promoted sides (either your player's club or the opponent) are reported as new
to the league rather than as a blank run of results. DefCon only exists from
2025/26, so earlier meetings show a dash rather than a zero.

---

## Install on Linux

Python **3.11 or newer** and `git` are the only requirements. SQLite needs no
installation, as it is part of Python's standard library, and the database is
created on first run at `data/fpl.db`.

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# Fedora / RHEL / Rocky
sudo dnf install -y python3 python3-pip git

# Arch
sudo pacman -S --needed python python-pip git
```

Check the version before going further. Some long-term-support distros still
ship 3.9, which this will not run on:

```bash
python3 --version   # need 3.11+
```

If yours is older, install a newer Python alongside it (`deadsnakes` on Ubuntu,
or [pyenv](https://github.com/pyenv/pyenv) anywhere) and substitute that
interpreter in the commands below.

### Get the code

```bash
git clone https://github.com/boredtoolbox/fpl-helper.git ~/fpl-helper
cd ~/fpl-helper
```

### Install the dependencies

Pick one of the three. They all work; they differ in how much they touch the
rest of the system.

#### Option A: virtualenv (recommended)

Keeps this app's pinned versions in one directory that you can delete. Nothing
outside `~/fpl-helper` is touched.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Re-activate with `source .venv/bin/activate` in any new shell.

#### Option B: no virtualenv, into your user directory

If you would rather not deal with activation. Packages land in
`~/.local/lib/python3.x/` and belong to your user alone, so there is no `sudo`
involved and the system Python's own site-packages are untouched.

```bash
pip3 install --user -r requirements.txt
```

Debian, Ubuntu and Raspberry Pi OS 12+ refuse this under **PEP 668**
("externally-managed-environment"), because `~/.local` shadows packages `apt`
also manages. Override it explicitly:

```bash
pip3 install --user --break-system-packages -r requirements.txt
```

The flag is safe enough here, as everything in `requirements.txt` is a pure
application dependency, but it is a real trade-off: if `apt` later installs a
different `requests` or `lxml`, the two can disagree. That is why Option A is
the recommendation. To undo it: `pip3 uninstall -y -r requirements.txt`.

#### Option C: uv

If you already use [uv](https://docs.astral.sh/uv/), it creates and populates
the venv in one step and is much faster on a Pi:

```bash
uv venv
uv pip install -r requirements.txt
```

Treat it as Option A from here on; the interpreter is still `.venv/bin/python`.

### Which interpreter do I use afterwards?

This matters, because cron and systemd get **no activated shell** and a minimal
`PATH`. Everywhere below that shows a command, substitute per your choice:

| | Interactive shell | In cron / systemd (absolute path) |
| --- | --- | --- |
| **A / C** (virtualenv) | `source .venv/bin/activate`, then `python` | `/home/<user>/fpl-helper/.venv/bin/python` |
| **B** (`--user`) | `python3` | `/usr/bin/python3` |

Option B needs nothing else: `pip3 install --user` puts the packages on the
system interpreter's import path for your user, so `/usr/bin/python3` finds
them, but **only when the job runs as that same user**, which is why the
systemd unit below sets `User=`.

<details>
<summary>If a dependency fails to build</summary>

`lxml` (pulled in by `trafilatura`) ships wheels for x86-64 and 64-bit ARM, so
this is rare. On a 32-bit Pi or an unusual architecture it compiles from source
and needs headers:

```bash
sudo apt install -y build-essential python3-dev libxml2-dev libxslt1-dev   # Debian/Ubuntu
sudo dnf install -y gcc python3-devel libxml2-devel libxslt-devel          # Fedora
```
</details>

---

## Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

`config.yaml` is **gitignored**. It is where everything personal lives, and it
never leaves your machine. `config.example.yaml` is the committed template, and
the app falls back to it if `config.yaml` is missing, so a fresh clone still
boots (with no teams).

Only `team_ids` has to be filled in. Everything else has a working default.

### Where to put your team IDs

At the top of `config.yaml`:

```yaml
team_ids:
  - 1234567          # required: one or more FPL entry IDs
  - 7654321

team_labels:         # optional; names the entries in the team selector
  1234567: "Main squad"
  7654321: "Experiment"
```

**Finding yours:**

1. Log in at [fantasy.premierleague.com](https://fantasy.premierleague.com).
2. Go to **Pick Team > View Gameweek History**.
3. The URL reads `.../entry/1234567/history`. That number is your entry ID.

Notes:

- List **as many entries as you like**. Every page carries a team selector and
  remembers your choice between pages. Each extra team costs a handful of API
  requests per refresh, nothing more.
- **Entry IDs are not secret in the strict sense** (anyone can view any entry's
  public history), but they identify you personally and link to your name and
  leagues. Keep `config.yaml` out of git, which it already is, and don't paste
  your IDs into issues or screenshots.
- IDs must be **integers, unquoted**. The keys in `team_labels` must match the
  numbers in `team_ids` exactly.
- After changing them, restart the app and run a refresh
  (`python -m app.refresh`) so the new entry's squad and leagues are pulled in.

### Where to put the Gemini API key

**In its own file, never in `config.yaml`.** The config holds only a *path* to
the key, so the key itself cannot be pasted into a gist or a screenshot along
with the rest of your settings.

```bash
mkdir -p secrets
printf '%s\n' 'YOUR_KEY_HERE' > secrets/gemini_key.txt
chmod 600 secrets/gemini_key.txt
chmod 700 secrets
```

The path is configurable, and defaults to that location:

```yaml
gemini_api_key_file: ./secrets/gemini_key.txt
gemini_model: "gemini-3.5-flash-lite"
```

Get a free key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

How the key is handled, so you can verify the claims:

- `secrets/` and `*.key` are gitignored, and the key never enters the config,
  the database, the logs or any page. See `Config.gemini_api_key()` in
  `app/config.py`.
- It is **read at call time, not at startup**, so rotating the key takes effect
  on the next run with no restart.
- Only the first non-empty, non-comment line is used, and a key containing
  whitespace is rejected with a clear message rather than being sent as a
  malformed HTTP header.
- **It is optional.** Without a key the app runs on statistics alone; the crowd
  panel says so and nothing else changes.

**Prefer an environment variable or a secret store?** Point
`gemini_api_key_file` at anything readable: `/run/secrets/gemini_key` for a
Docker/Podman secret, or a path you write at boot from your own vault. For
systemd specifically, `LoadCredential=` is the clean route:

```ini
LoadCredential=gemini:/etc/fpl-helper/gemini_key.txt
Environment="FPL_GEMINI_KEY_FILE=%d/gemini"
```

...with `gemini_api_key_file: ${FPL_GEMINI_KEY_FILE}`. Note that the config does
**not** expand environment variables today, so this needs a one-line change in
`app/config.py`. The file-path default is the supported path.

Model names get retired for new keys, so the model is configurable. If a call
fails with a 404, the API's error message names the current replacement. Put
that in `gemini_model` and re-run the crowd job.

### Where to put your YouTube creator profiles

Under `youtube_channels` in `config.yaml`, **in rank order, most trusted
first**:

```yaml
youtube_channels:
  - "https://www.youtube.com/@LetsTalkFPL"      # full channel URL
  - "@FPLHarry"                                 # a bare handle works too
  - "UCxeOc7eFxq37yW_Nc-69deA"                  # so does a raw channel id
  - name: "Scout"                               # or name it yourself
    url: "https://www.youtube.com/@fantasyfootballscout"
```

**No API key and no YouTube account is needed.** Each channel is read through
its public RSS feed; the app resolves a URL or handle to a `UC...` channel id on
first use and caches it in SQLite, so you never have to dig one out and it costs
one fetch ever, not one per refresh.

**The order matters:**

- Transcripts are taken **round-robin** down this list, so one prolific channel
  cannot spend the whole budget and leave the others unheard. When the budget
  runs out mid-round, it is the lowest-ranked creator who misses out.
- The prompt tells Gemini the ranking, so where creators disagree the
  higher-ranked view is weighted and the disagreement is reported.
- Creators are gathered **ahead of general news**, so news can never push them
  out of the prompt.

**Keep the list short, around five.** These are consulted on every crowd run.

Verify a channel before you trust it:

```bash
python tools/find_channel_id.py @LetsTalkFPL
```

It prints the resolved id and how many recent videos the feed carries. **If the
newest video is weeks old, you have the wrong channel.** Handles are not unique
enough to trust blindly, and an `@handle` is not a channel id. Confirm against
the creator's actual page.

YouTube will temporarily IP-block a burst of transcript requests, so the
gatherer spaces them out, caps them, and gives up the moment it detects a block:

```yaml
crowd_max_transcripts: 12          # total per refresh, across all channels
crowd_max_videos_per_channel: 4    # newest N videos considered per channel
crowd_transcript_delay: 2.0        # seconds between transcript requests
crowd_lookback_days: 3             # how far back to look for videos/articles
```

The defaults put a daily run at roughly 12 requests over 24 seconds, well clear
of the limit. Raise them only if you stop seeing blocks. News and official FPL
flags are unaffected either way.

### News feeds (optional)

`news_rss_feeds` works out of the box and needs no keys. Add or remove any RSS
feed:

```yaml
news_rss_feeds:
  - name: "Fantasy Football Scout"
    url: "https://www.fantasyfootballscout.co.uk/feed/"
```

---

## First run

```bash
source .venv/bin/activate    # Option A/C only; skip it on Option B
python -m app.refresh        # Option B: python3 -m app.refresh
```

This takes a few minutes the first time: it sparse-clones the historical
dataset and walks the FPL API at roughly one request per second. Later runs are
much quicker, about 8 seconds when there is nothing new.

```bash
python run.py                # Option B: python3 run.py
```

It prints the URL to open, including the LAN address so you can reach it from a
phone or another machine on the network. `--host`, `--port`, `--config` and
`--no-scheduler` are all available (`python run.py --help`).

From here on the examples say `python`, meaning *whichever interpreter has the
dependencies*. See [the table above](#which-interpreter-do-i-use-afterwards).

### Reaching it from elsewhere

`run.py` binds `0.0.0.0:8000`, so anything on your LAN can reach it. Open the
port if a firewall is in the way:

```bash
sudo ufw allow 8000/tcp                              # Debian/Ubuntu
sudo firewall-cmd --add-port=8000/tcp --permanent    # Fedora/RHEL
sudo firewall-cmd --reload
```

**There is no authentication, so do not expose it to the internet as-is.**
Anyone who can reach the port can read your squads and press "Refresh now". On a
VPS, bind it to localhost (`--host 127.0.0.1`) and reach it over an SSH tunnel
(`ssh -L 8000:localhost:8000 you@host`) or a VPN like Tailscale. If you must
publish it, put it behind a reverse proxy that handles TLS and authentication.

---

## Run it as a service

A systemd unit keeps it running across reboots and crashes. Create
`/etc/systemd/system/fpl-helper.service`, substituting your username and path:

```ini
[Unit]
Description=FPL Helper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<user>
Group=<user>
WorkingDirectory=/home/<user>/fpl-helper
Environment="PYTHONUNBUFFERED=1"
# Optional: a fixed session key. Without one, a random key is generated
# into data/.flask_secret on first boot, which is fine for a single instance.
# Environment="FPL_SECRET_KEY=change-me"
# Option A/C (virtualenv). For Option B, use /usr/bin/python3 instead.
ExecStart=/home/<user>/fpl-helper/.venv/bin/python -m waitress \
    --host=0.0.0.0 --port=8000 --call app:create_app
Restart=on-failure
RestartSec=15

# Modest hardening: the app only needs its own directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
# The leading "-" means systemd tolerates the directory not existing yet.
# Without it, a missing logs/ stops the service from starting at all.
ReadWritePaths=/home/<user>/fpl-helper/data -/home/<user>/fpl-helper/logs

[Install]
WantedBy=multi-user.target
```

Replace every `<user>` with your username. `ProtectHome=read-only` still lets
Python import from the virtualenv *and* from `~/.local`, so it is correct for
both install options; only `data/` and `logs/` are writable.

`waitress` is a small production WSGI server, pinned in `requirements.txt`. The
interpreter path must be **absolute**, because systemd has no activated shell
and no useful `PATH` to inherit. To use Flask's own server instead (fine on a
private network) replace `ExecStart` with:

```ini
ExecStart=/home/<user>/fpl-helper/.venv/bin/python /home/<user>/fpl-helper/run.py --port 8000
# Option B: ExecStart=/usr/bin/python3 /home/<user>/fpl-helper/run.py --port 8000
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify fpl-helper.service   # catches typos before you start it
sudo systemctl enable --now fpl-helper
systemctl status fpl-helper
journalctl -u fpl-helper -f
```

<details>
<summary>Running it under a dedicated system user instead</summary>

Cleaner on a shared or internet-facing box, as the app gets no home directory
and no login shell:

```bash
sudo useradd --system --home /opt/fpl-helper --shell /usr/sbin/nologin fplhelper
sudo git clone https://github.com/boredtoolbox/fpl-helper.git /opt/fpl-helper
sudo chown -R fplhelper:fplhelper /opt/fpl-helper
sudo -u fplhelper python3 -m venv /opt/fpl-helper/.venv
sudo -u fplhelper /opt/fpl-helper/.venv/bin/pip install -r /opt/fpl-helper/requirements.txt
```

Then set `User=fplhelper`, `Group=fplhelper`, `WorkingDirectory=/opt/fpl-helper`,
`ProtectHome=true`, and `ReadWritePaths=/opt/fpl-helper/data -/opt/fpl-helper/logs` in
the unit. Run the cron jobs as `fplhelper` too (`sudo -u fplhelper crontab -e`), and
make sure `secrets/gemini_key.txt` is owned by `fplhelper` with mode `600`.

Use a virtualenv here regardless of what you chose above, as a `--system`
account has no real home directory for `pip install --user` to write into.
</details>

The service only serves pages. Both data jobs belong to the scheduler, so the
site stays up independently of whether a refresh succeeded. See
[Scheduling](#scheduling).

---

## Refreshing

Two ways:

- **From cron**, covered in [Scheduling](#scheduling) below. This is the normal
  path.
- **From the UI**, with the "Refresh now" button, top right. It runs in the
  background and is locked so two refreshes can't overlap.

From the CLI, which is also what cron calls:

```bash
python -m app.refresh                    # normal run
python -m app.refresh --force-historical # re-pull the historical dataset
python -m app.refresh --quiet            # warnings and errors only
```

The app does **not** schedule anything itself while `refresh_hour` is `null`
(the shipped default). Set it to an hour 0-23 if you would rather the web
process own the schedule and skip cron entirely.

The refresh runs these stages, each independently fault-tolerant: if the FPL
API or GitHub fails, that stage logs the error and the rest carries on.

1. `bootstrap-static` for players, teams and gameweeks
2. `fixtures` for all 380 fixtures with difficulty ratings
3. your entries, picks, history, and every league you are in, covering rank,
   league size and your points in it, all from the one `entry/` response
4. per-player match histories, for anyone whose club has played since we last
   asked (capped at 180 in a single run)
5. the historical dataset (weekly)
6. projections (folding in the stored crowd adjustments)
7. team kit images, only the ones not already cached

Step 6 also archives what it projected for each gameweek still to come, into
`projection_history`. `projections` itself is wiped and rebuilt every run, so
without that archive the number a gameweek was expected to score is lost the
moment it kicks off, and cannot be recovered afterwards, because recomputing it
would use results the model did not have at the time. That archive is what lets
the squad page show projected against actual.

**The refresh never calls Gemini.** It only *reads* the adjustments the crowd
job last stored, so you can hit "Refresh now" as often as you like for free.

It is also cheap in FPL requests, which is what makes that true in practice.
There is no league-standings stage: paging through league tables to find
yourself cost hundreds of requests to rediscover numbers the `entry/` response
had already sent. And a player's match history cannot change while their club
is idle, so between gameweeks stage 4 fetches nothing at all. A refresh with
nothing new to collect takes about **8 seconds**; the first one after a
gameweek does real work and takes a few minutes.

### Retention

Nothing accumulates. A `retention` stage runs at the end of both jobs and holds
storage flat for the whole season:

| What | Policy |
|---|---|
| Crowd intel | The newest successful run replaces the previous one outright. A failed run is kept without its documents, so its error message survives. |
| Projection history | One row per player per gameweek, written while the gameweek is still unplayed and frozen at kick-off. About 25k small rows a season. |
| `fetch_log` | Diagnostic only; rows older than 90 days are dropped. |

Crowd intel is *replaced* rather than archived, because only two rows are ever
read: the newest successful run feeds the projections, and the newest run of any
kind feeds the crowd panel. Source documents are the bulk of it (a transcript
is stored up to 100k characters, twelve per run), so keeping every run costs
roughly **357 MB a season**. Replacing holds it near **2 MB**, flat.

Projections keep working from local data throughout: the adjustments they read
survive every prune, and a failed run never destroys the last good one.

**Pages never touch the network.** Every request reads SQLite; only the refresh
job makes HTTP calls. Data freshness is shown in the footer of every page.

## The crowd-intel job

Crowd intel is not part of the refresh: it costs one Gemini call every time it
runs, so it gets its own schedule instead of firing on every button press.

```bash
python -m app.crowd_refresh              # gather, call Gemini, rebuild
python -m app.crowd_refresh --no-rebuild # store the intel, rebuild later
python -m app.crowd_refresh --quiet      # warnings and errors only
```

It gathers YouTube transcripts and news, makes **one** batched Gemini call (plus
a single retry if the JSON comes back malformed), stores the ratified
adjustments, then rebuilds projections on top of them. If there is
no API key, no documents, or the call fails, it exits non-zero and leaves your
existing projections untouched.

## Scheduling

Both jobs run on a timer, separate from the web service, so the site stays up
whether or not a refresh succeeded.

### With cron

`crontab -e` as the user that owns the checkout, then:

**Option A / C (virtualenv):**

```cron
# FPL data refresh: free, no API calls
0 5 * * * cd /home/<user>/fpl-helper && mkdir -p logs && .venv/bin/python -m app.refresh --quiet >> logs/cron-refresh.log 2>&1

# Crowd intel: one Gemini call per run
0 6 * * * cd /home/<user>/fpl-helper && mkdir -p logs && .venv/bin/python -m app.crowd_refresh --quiet >> logs/cron-crowd.log 2>&1
```

**Option B (`pip install --user`)**, the same lines with the system interpreter:

```cron
0 5 * * * cd /home/<user>/fpl-helper && mkdir -p logs && /usr/bin/python3 -m app.refresh --quiet >> logs/cron-refresh.log 2>&1
0 6 * * * cd /home/<user>/fpl-helper && mkdir -p logs && /usr/bin/python3 -m app.crowd_refresh --quiet >> logs/cron-crowd.log 2>&1
```

Four things about those lines matter:

- **The `cd` is not optional.** Cron starts with no working directory, and
  `config.yaml`, `secrets/` and `data/` are all resolved from the project root.
- **Name the interpreter explicitly.** Cron's `PATH` is minimal and nothing
  activates a virtualenv for you, so a bare `python` often resolves to nothing
  at all. On Option B, the crontab must belong to the **same user** that ran
  `pip install --user`, since `~/.local` is per-user and a root crontab will not
  see those packages.
- **Create `logs/` in the job itself.** It is gitignored, so a fresh clone will
  not have it, and the shell opens a `>>` redirect *before* running the command,
  so a missing directory means the job never starts at all, with no log to say
  why.
- **Order matters.** The refresh runs first so crowd intel reads that morning's
  official injury flags, and the crowd job rebuilds projections last, so by
  06:05 everything on the site reflects both. Keep them an hour apart: they are
  separate processes, and `REFRESH_LOCK` is a `threading.Lock`, which guards
  threads inside one process and cannot stop two processes overlapping.

Cron uses the **system timezone**, which is not necessarily UTC. Confirm what
yours is with `timedatectl` (or `date`) before picking the hours, and remember
that in a DST-observing zone an hour can be skipped or repeated, so pick times
away from the window between 01:00 and 03:00.

<details>
<summary>With systemd timers instead</summary>

Better logging (`journalctl`), a `Persistent=true` catch-up after downtime, and
the same hardening as the service. Two units per job, starting with
`/etc/systemd/system/fpl-refresh.service`:

```ini
[Unit]
Description=FPL Helper data refresh

[Service]
Type=oneshot
User=<user>
WorkingDirectory=/home/<user>/fpl-helper
ExecStart=/home/<user>/fpl-helper/.venv/bin/python -m app.refresh --quiet
# Option B: ExecStart=/usr/bin/python3 -m app.refresh --quiet
```

...and `/etc/systemd/system/fpl-refresh.timer`:

```ini
[Unit]
Description=Run the FPL Helper refresh daily

[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Copy both for the crowd job (`fpl-crowd.*`, `-m app.crowd_refresh`, `06:00:00`).
Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fpl-refresh.timer fpl-crowd.timer
systemctl list-timers 'fpl-*'
journalctl -u fpl-refresh -n 50
```

`OnCalendar` follows the system timezone unless you add `Timezone=UTC`.
</details>

### Pages update themselves

You do not need to reload anything after a job runs. Every page is served with
a short data-version token and polls `/api/status` for it, once a minute when
idle and every five seconds while a job is running. When the token changes, the
page reloads itself.

That polling is what makes cron work for open tabs: the jobs run in their own
processes, so there is nothing to push from, and SQLite is the only thing the
web app and cron share. The same channel carries progress, so the "Refresh now"
button shows the live stage of a **cron** job (`Refreshing:
element_summaries...`), not just one you started from the UI.

Two more details:

- A reload is held back while you are typing in a filter box, and happens when
  you leave the field, so that a 06:00 job cannot wipe a half-typed search.
- A job killed mid-run (power cut, `kill -9`) stops beating; after 15 minutes
  the UI stops believing it and re-enables the button.

The footer shows both jobs separately, since they no longer run together.

---

## How the projection works

For each player and each of the next six gameweeks:

```
xPts = P(start) × appearance points
     + xG/90 × (expected minutes / 90) × goal points for position × finishing adjustment
     + xA/90 × (expected minutes / 90) × 3
     + P(clean sheet) × clean sheet points × P(60 minutes)
     + P(hits DefCon threshold) × 2
     + expected bonus
     + expected save points          (goalkeepers)
     − expected goals conceded, cards
```

**Opponent and venue.** Team attack and defence ratings come from xG for and
against per match, blended between the current season and last season, weighted
entirely to history before a ball is kicked and shifting to the current season
over the first ten gameweeks. Clean sheet probability is Poisson: `exp(−expected
goals conceded)`. Newly promoted sides with no top-flight history get a
below-average prior rather than being treated as average.

**DefCon.** Defenders score 2 points at 10+ clearances, blocks, interceptions
and tackles; midfielders and forwards at 12+ of those plus recoveries.
Reliability is the share of a player's 60-minute appearances that cleared the
bar. The stat only exists from 2025/26 onward, so earlier seasons are excluded
from DefCon maths, since averaging them in would halve every rate.

**Head-to-head.** A player's record against the upcoming opponent over the last
two seasons, weighted at ±5% maximum and only above three matches. It's mostly
noise, so the model barely leans on it, but it is the kind of thing a manager
wants to see, which is what the expandable rows on Player Stats are for.

**Availability and start probability** come from recent starts, official FPL
flags, and the crowd layer. Availability and starting are tracked separately, so
an injured player projects zero while a fit squad player who rarely starts still
earns a little from cameo minutes.

---

## How the Squad Builder scores a player

Four inputs, and only four, which are the ones the page says it uses:

```
performance   = 0.50 x form  +  0.50 x points per game     (FPL points per match)
per match     = 0.35 x performance x FDR
              + 0.30 x FPL's EP    x FDR
              + 0.35 x xPts per fixture, re-scaled for the run
score         = per match  x  matches in the horizon
```

All three baselines are already points-per-match, so they blend without
rescaling. Performance is split evenly between form (the last 30 days) and
points per game (the season), so that a single hot fortnight does not decide
everything.

**FDR multiplier** runs from x1.25 for a difficulty-1 fixture to x0.75 for a 5,
averaged across the run. It is applied to performance and to FPL's EP, but
**not** to xPts: the projection engine has already adjusted per fixture for the
opponent, so multiplying again would count the same difficulty twice.

**xPts is re-scaled instead.** The engine's horizon (`horizon_gws`, 6 by
default) is shorter than this page's ten, so its per-fixture average is
stretched across the rest of the run by the ratio of the two windows' average
difficulty, on the basis that "the gameweeks the engine has not reached are 8%
kinder than the ones it has". When the horizon *is* the engine's window the
ratio is 1 and nothing happens.

**Multiplying by the number of matches** is what makes a double gameweek worth
twice a single one and a blank worth nothing. Runs are laid out one cell per
gameweek so a blank is visible as a gap rather than silently pulling the next
fixture left.

Three gates apply before any of that: a player needs a fixture in the horizon,
has to be available (FPL's own flags, *and* not reported injured or suspended by
the crowd layer, since anyone the engine has zeroed cannot be recommended here),
and has to clear the minutes floor, which is the page's one adjustable filter.

Early in a season form, EP and points per game are three names for the same
handful of matches, and a weighted blend of one number is that number. The page
counts how many eligible players that is currently true of and says so, rather
than implying a blend it is not doing.

**Per-game rates** for minutes, xG and xA divide by *appearances*, not by
gameweeks: a rotation option who has featured twice in six weeks is described by
the two matches he played, not marked down for the four he watched. Appearances
come from FPL's own `points_per_game` (total points ÷ appearances, so the count
reads straight back out of it), falling back to counted match history for anyone
sitting on exactly zero points.

---

## What your creators are saying

The second half of the Squad Builder turns the crowd layer's output back into
per-creator advice, in the trust order you configured.

The crowd layer stores **one merged row per player**, holding availability, a
sentiment score and a role note, with each line in `reasons` naming the source
that said it ("Let's Talk FPL: expected to be rotated after midweek"). That
prefix is the only per-creator attribution there is, so it is what the page
reads. Lines from Official FPL and the news feeds are counted and excluded; they
are not creators, and they already drive the availability flags everywhere else.

What the schema does *not* record is a structured verdict, so the chip beside
each line, one of **captain / buy / sell / keep / start / minutes risk / out**,
is a reading of that creator's own wording, and the sentence itself is always
printed next to it. Where the wording does not commit either way the chip says
"mentioned" rather than guessing. Sentiment and the role note are merged across
every source, so they sit in their own column labelled as such rather than being
attributed to whoever's row they happen to be on.

Every configured creator gets a panel whether or not they said anything, so a
channel that has gone quiet is visible as an empty panel rather than as an
absence you have to notice. Where it is empty the panel says which kind of empty
it is, from `meta.crowd_channel_status`: the run could not read the channel, or
it read N transcripts and none of them named a player. A channel with videos but
no transcripts is usually YouTube rate-limiting the scraper, not the creator
going quiet.

---

## Team imagery

Shirts and crests are **fetched once and served locally**, never hotlinked.
Pages never touch the network, so the squad pitch and the match list have to
draw from `static/kits/` and `static/crests/` or not at all. That also means
both still render with the host offline.

The two come from different CDNs and neither is keyed by the FPL team `id`:

| | Source | Key |
| --- | --- | --- |
| Shirts | `fantasy.premierleague.com/dist/img/shirts/...` | team `code` |
| Crests | `resources.premierleague.com/premierleague/badges/70/...` | team `code`, prefixed `t` |

A file is only fetched when it is missing, so the `images` stage does no network
work on a normal refresh; these change once a season. About 60 files and 450KB
for the league. Both directories are gitignored.

---

## Where goalscorers come from

The Matches page needs to know who scored and who assisted in each fixture, and
there are two places that could answer it.

`player_gw_history` looks like the obvious source, since it has `goals_scored`
and `assists` per player per fixture. It is the wrong one, for two reasons:

- **Its coverage is capped.** Element summaries are fetched for whoever has new
  data, prioritised and capped at `DAILY_SUMMARY_BUDGET` per run, so which
  players are in it depends on what the last few runs got round to.
- **It files an own goal under the player who scored it**, not the side it
  counted for, so the goals listed for a team do not add up to that team's
  score.

Reconciling every finished fixture's scoreline against it, 25 of 30 matched; the
five that did not were exactly the five own goals.

The right source was already being fetched and thrown away: each fixture in the
`fixtures/` endpoint carries a `stats` array of per-player goals, assists, own
goals, cards, saves and bonus, split home and away. `sync_fixture_stats` now
stores it in `fixture_stats`, and all 30 of 30 scorelines reconcile.

Rows are **deleted and rewritten** per fixture rather than upserted, because a
stat line can be withdrawn as well as added: a goal reassigned, a red card
rescinded, bonus recalculated once a provisional gameweek is signed off. An
upsert would leave the retracted row behind for good. Only fixtures that have
actually started are touched, so an unplayed fixture cannot have its rows
cleared by a run that sees an empty array.

---

## How the crowd layer works

Gathered daily, best-effort:

- **Official FPL flags**: `status`, `news`, `chance_of_playing_next_round`.
  Highest trust; they alone can force a start probability to zero.
- **News RSS**: via `feedparser`, with article text extracted by `trafilatura`.
- **YouTube transcripts**: recent videos found through each channel's RSS feed
  (no API key needed), transcripts via `youtube-transcript-api`.

Everything goes to Gemini in one batched call that must return strict JSON
matching a fixed schema. The response is validated with `pydantic`; if it's
malformed, the app retries once with the parse error attached, then gives up and
carries on without it. Player names are matched to FPL element IDs with
`rapidfuzz`; names that can't be matched are stored and listed in the UI rather
than guessed at.

The ratification rules are the only place crowd data touches a number:

| Signal | Effect |
| --- | --- |
| `injured` / `suspended`, or an official 0% flag | start probability → 0 |
| `doubt` | capped at the **lowest** of the model value, the crowd's hint, and the official percentage |
| positive sentiment | can never raise a start probability |
| sentiment | ±10% on xPts at most, scaled by confidence, and only with two or more independent sources agreeing |

Every adjustment is written to the database with its reasons, so a projection
can be traced back to, for example, `6.2 → 0.0` alongside "Official FPL status:
injured (hamstring)".

---

## Development

`tests/` is gitignored and is **not part of this repository**; the suite was
kept local to the machine it was written on. If you add one, the points worth
covering are the xPts components, the ratification rules, Gemini JSON validation
and its fallback, head-to-head aggregation, and an end-to-end smoke run with
every network call mocked:

```bash
source .venv/bin/activate            # Option A/C only
pip install pytest                   # Option B: pip3 install --user pytest
python -m pytest tests/ -q
```

Nothing in the app requires it to run.

```
app/
  __init__.py       app factory + scheduler
  config.py         config.yaml loading
  db.py             schema and helpers
  refresh.py        refresh + crowd-job orchestration, CLI
  retention.py      what we keep: replace crowd intel, prune the fetch log
  crowd_refresh.py  crowd-intel CLI (the cron entry point)
  routes.py         Flask routes
  views.py          read-only queries backing the pages
  logging_setup.py  rotating file logs
  services/
    fpl_api.py      rate-limited FPL API client and sync
    historical.py   vaastav dataset loading + head-to-head
    crowd_intel.py  sources, Gemini, ratification
    engine.py       xPts projection
    shortlist.py    Squad Builder: per-position scoring + the creator board
    kits.py         team imagery: shirts and crests, cached under static/
templates/          Jinja2, one per page plus shared macros
static/             one stylesheet, one small JS file
tools/              find_channel_id.py
tests/
```

Logs rotate in `logs/` (5 × 2MB). SQLite lives at `data/fpl.db`.

### Data sources

- [Official FPL API](https://fantasy.premierleague.com/api/): free, no auth on
  the endpoints used here. The client sends a real User-Agent, retries with
  backoff, and stays under one request per second.
- [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League):
  per-gameweek history for past seasons. Sparse-cloned on first run (only the
  seasons in `historical_seasons`), then `git pull`ed weekly. Players are matched
  across seasons by the FPL `code`, which is stable, so no name matching is
  needed for historical data.

---

## Troubleshooting

**"No picks saved for this team yet."** FPL only publishes a squad after a
gameweek deadline passes, so the page stays empty until your first deadline.

**Everything shows zero.** Run `python -m app.refresh` and check `logs/fpl-helper.log`.

**A player projects 0.00 xPts.** They're almost certainly flagged. Check the
Status column on the Player Stats page; a 0% chance of playing forces the
projection to zero by design.

**The crowd panel says "Running on statistics alone."** No Gemini key, or the
last call failed. The error is shown in that panel and in the log. Everything
else still works.

**A YouTube channel yields nothing.** Either the channel ID is wrong (re-resolve
it with `python tools/find_channel_id.py @handle`, since the `@handle` is not a
channel ID), or YouTube has temporarily blocked your IP. The log distinguishes
the two; a block reads "YouTube blocked transcript requests from this IP" and
normally clears within the hour. Nothing else in the refresh is affected.

**`git pull` fails on the historical dataset.** The app keeps using the existing
checkout and logs a warning. Force a fresh pull with
`python -m app.refresh --force-historical`.

## Licence

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0),
full text in [LICENSE](LICENSE).

Use it, run it, modify it and share it freely for any **noncommercial**
purpose — personal use, research, study, hobby projects, and use by charities,
schools, universities and government bodies. Redistribute your changes if you
like; just keep the licence and copyright notice with them.

**Any commercial use needs my written permission first.** That covers selling
it, running it as a paid or ad-supported service, and using it internally at a
for-profit company. Ask via [an issue](https://github.com/boredtoolbox/fpl-helper/issues)
and I'll almost certainly say yes.

Note this is a source-available licence, not an open-source one: the
noncommercial restriction is deliberate, so the OSI would not certify it and
GitHub will report the licence as "Other".
