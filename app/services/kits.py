"""Team imagery — kits and crests — cached on disk.

Pages never touch the network, so neither the shirts on the squad pitch nor the
crests on the match list can be hotlinked from FPL's CDN: they are fetched once
into `static/kits/` and `static/crests/` and served from there like any other
asset. That also means both still draw with the Pi offline, and that an image is
not re-requested every time a page is opened.

The whole league is about 60 files and 450KB. A file is only fetched when it is
missing, so a normal refresh does no network work here at all — kits and crests
change once a season, not once a day.

The two come from different CDNs and are keyed differently, which is the one
thing to know: shirts are `fantasy.premierleague.com` by FPL team code, crests
are `resources.premierleague.com` by the same code with a `t` in front. Neither
is the team `id`.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import requests

from ..db import log_fetch, set_meta, utcnow

log = logging.getLogger(__name__)

# 110px source for a cell that renders around 46px, so it stays sharp on a
# retina screen without paying for the 220px version.
KIT_URL = "https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_{code}{gk}-110.png"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 fpl-helper/1.0 (personal FPL analysis tool)"
)
# The Premier League's own badge CDN. 70px for a crest that renders around 22px,
# so it stays sharp at 3x without paying for the SVG — twenty complex vector
# badges on one page is real work for a phone, and this page draws twenty.
CREST_URL = "https://resources.premierleague.com/premierleague/badges/70/t{code}.png"
TIMEOUT = 20.0
# A truncated or error page saved as a .png would render as a broken image
# forever, since a present file is never re-fetched.
MIN_BYTES = 400


def kit_name(code: int, goalkeeper: bool = False) -> str:
    """File name for a team's shirt. Keepers wear a different one."""
    return f"shirt_{int(code)}{'_gk' if goalkeeper else ''}.png"


def kits_dir(static_dir: str | Path) -> Path:
    return Path(static_dir) / "kits"


def have_kit(static_dir: str | Path, code: int | None, goalkeeper: bool = False) -> bool:
    if not code:
        return False
    return (kits_dir(static_dir) / kit_name(code, goalkeeper)).exists()


def crest_name(code: int) -> str:
    """File name for a team's crest."""
    return f"crest_{int(code)}.png"


def crests_dir(static_dir: str | Path) -> Path:
    return Path(static_dir) / "crests"


def have_crest(static_dir: str | Path, code: int | None) -> bool:
    if not code:
        return False
    return (crests_dir(static_dir) / crest_name(code)).exists()


def _download(session: requests.Session, url: str, target: Path) -> bool:
    try:
        resp = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        log.warning("image fetch failed %s: %s", url, exc)
        return False
    if resp.status_code != 200 or len(resp.content) < MIN_BYTES:
        log.warning("image fetch %s returned %s (%d bytes)", url, resp.status_code, len(resp.content))
        return False
    if not resp.headers.get("Content-Type", "").startswith("image/"):
        log.warning("image fetch %s was not an image (%s)", url, resp.headers.get("Content-Type"))
        return False
    # Written aside and moved into place, so a half-finished download is never
    # left behind looking like a cached file.
    tmp = target.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(target)
    return True


def sync_kits(
    conn: sqlite3.Connection, static_dir: str | Path, *, force: bool = False
) -> dict[str, Any]:
    """Fetch any missing team kits. Never raises — the pitch works without them."""
    target_dir = kits_dir(static_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    teams = [
        (r["code"], r["short_name"] or r["name"])
        for r in conn.execute("SELECT code, name, short_name FROM teams WHERE code IS NOT NULL")
    ]
    wanted = [
        (code, label, gk)
        for code, label in teams
        for gk in (False, True)
        if force or not (target_dir / kit_name(code, gk)).exists()
    ]
    result: dict[str, Any] = {"teams": len(teams), "fetched": 0, "failed": 0, "skipped": 0}
    if not wanted:
        result["skipped"] = len(teams) * 2
        log.info("kits: all %d already cached", len(teams) * 2)
        return result

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "image/png,image/*"})
    try:
        for code, label, gk in wanted:
            url = KIT_URL.format(code=code, gk="_1" if gk else "")
            if _download(session, url, target_dir / kit_name(code, gk)):
                result["fetched"] += 1
            else:
                result["failed"] += 1
    finally:
        session.close()

    set_meta(conn, "kits_synced_at", utcnow())
    log_fetch(
        conn, "kits", "shirts", result["failed"] == 0,
        f"{result['fetched']} fetched, {result['failed']} failed",
    )
    log.info("kits: %d fetched, %d failed", result["fetched"], result["failed"])
    return result


def sync_crests(
    conn: sqlite3.Connection, static_dir: str | Path, *, force: bool = False
) -> dict[str, Any]:
    """Fetch any missing team crests. Never raises — the match list works without them.

    Same shape as `sync_kits`, and deliberately a separate pass: a promoted club
    can have a crest on the league's CDN before FPL has drawn its shirt, or the
    other way round, and one missing file should not stop the other being
    fetched.
    """
    target_dir = crests_dir(static_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    teams = [
        (r["code"], r["short_name"] or r["name"])
        for r in conn.execute("SELECT code, name, short_name FROM teams WHERE code IS NOT NULL")
    ]
    wanted = [
        (code, label) for code, label in teams
        if force or not (target_dir / crest_name(code)).exists()
    ]
    result: dict[str, Any] = {"teams": len(teams), "fetched": 0, "failed": 0, "skipped": 0}
    if not wanted:
        result["skipped"] = len(teams)
        log.info("crests: all %d already cached", len(teams))
        return result

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "image/png,image/*"})
    try:
        for code, label in wanted:
            if _download(session, CREST_URL.format(code=code), target_dir / crest_name(code)):
                result["fetched"] += 1
            else:
                result["failed"] += 1
                log.warning("crest missing for %s (code %s)", label, code)
    finally:
        session.close()

    set_meta(conn, "crests_synced_at", utcnow())
    log_fetch(
        conn, "images", "crests", result["failed"] == 0,
        f"{result['fetched']} fetched, {result['failed']} failed",
    )
    log.info("crests: %d fetched, %d failed", result["fetched"], result["failed"])
    return result
