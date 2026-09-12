"""Crowd-intelligence layer: YouTube + RSS -> Gemini -> bounded adjustments.

The division of labour is strict:
  * Gemini reads natural language and emits STRUCTURED JSON only.
  * Python owns every number that reaches the squad.

Gemini can knock a player's start probability down, cap a doubt, or nudge xPts
by at most +/-10%. It cannot pick a team, a captain, or a transfer. Every
adjustment is stored with the reasons that produced it so the UI can explain
itself, and every stage degrades to a no-op if a source (or the API key) is
missing.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from ..db import jdump, log_fetch, set_meta, utcnow

log = logging.getLogger(__name__)

# Hard caps — the crowd layer may nudge, never dominate.
MAX_SENTIMENT_SWING = 0.10   # +/-10% on xPts
MIN_SOURCES_FOR_SENTIMENT = 2
FUZZY_MATCH_THRESHOLD = 86   # rapidfuzz score out of 100

AVAILABILITY_VALUES = ("fit", "doubt", "injured", "suspended", "unknown")


# --------------------------------------------------------------------------
# Response schema
# --------------------------------------------------------------------------
try:
    from pydantic import BaseModel, Field, ValidationError, field_validator

    PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover - pydantic is a listed dependency
    PYDANTIC_AVAILABLE = False
    BaseModel = object  # type: ignore[assignment,misc]
    ValidationError = Exception  # type: ignore[assignment,misc]


if PYDANTIC_AVAILABLE:

    class PlayerIntel(BaseModel):
        player_name: str
        fpl_id_guess: int | None = None
        availability: str = "unknown"
        start_probability_hint: float | None = None
        sentiment: float = 0.0
        predicted_role_change: str | None = None
        reasons: list[str] = Field(default_factory=list)
        confidence: float = 0.0

        @field_validator("availability", mode="before")
        @classmethod
        def _clean_availability(cls, v: Any) -> str:
            v = str(v or "unknown").strip().lower()
            return v if v in AVAILABILITY_VALUES else "unknown"

        @field_validator("sentiment", mode="before")
        @classmethod
        def _clamp_sentiment(cls, v: Any) -> float:
            try:
                return max(-1.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.0

        @field_validator("confidence", mode="before")
        @classmethod
        def _clamp_confidence(cls, v: Any) -> float:
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.0

        @field_validator("start_probability_hint", mode="before")
        @classmethod
        def _clamp_hint(cls, v: Any) -> float | None:
            if v is None or v == "":
                return None
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return None

        @field_validator("reasons", mode="before")
        @classmethod
        def _listify(cls, v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v][:8]

    class CrowdResponse(BaseModel):
        players: list[PlayerIntel] = Field(default_factory=list)
        general_notes: list[str] = Field(default_factory=list)

        @field_validator("general_notes", mode="before")
        @classmethod
        def _listify_notes(cls, v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v][:40]


@dataclass
class CrowdAdjustment:
    """Resolved, player-linked intel ready to be applied by the engine."""

    player_id: int
    player_name: str
    availability: str = "unknown"
    start_probability_hint: float | None = None
    sentiment: float = 0.0
    confidence: float = 0.0
    n_sources: int = 0
    predicted_role_change: str | None = None
    reasons: list[str] = field(default_factory=list)
    match_score: float = 0.0


# --------------------------------------------------------------------------
# Ratification: the only place crowd data touches the numbers
# --------------------------------------------------------------------------
def apply_crowd_adjustment(availability, adjustment: CrowdAdjustment | None, player_row: Any):
    """Fold crowd intel into a start probability. Never raises it above the model."""
    from .engine import Availability, _clamp  # local import avoids a cycle

    if adjustment is None:
        return availability

    p = availability.p_start
    available = availability.p_available
    reasons = list(availability.reasons)
    source = availability.source

    if adjustment.availability in ("injured", "suspended"):
        reasons.append(
            f"Crowd intel: reported {adjustment.availability} — "
            + "; ".join(adjustment.reasons[:3])
        )
        return Availability(0.0, reasons, "crowd", p_available=0.0)

    if adjustment.availability == "doubt":
        candidates = [p]
        if adjustment.start_probability_hint is not None:
            candidates.append(adjustment.start_probability_hint)
        official = player_row["chance_of_playing_next_round"]
        if official is not None:
            candidates.append(_clamp(official / 100.0, 0.0, 1.0))
        capped = min(candidates)
        if capped < p:
            reasons.append(
                f"Crowd intel: doubt — start probability capped {p:.0%} -> {capped:.0%}"
                + (f" ({adjustment.reasons[0]})" if adjustment.reasons else "")
            )
            p = capped
            available = min(available, capped)
            source = "crowd"

    return Availability(
        _clamp(p, 0.0, 1.0), reasons, source, p_available=_clamp(available, 0.0, 1.0)
    )


def sentiment_multiplier(adjustment: CrowdAdjustment | None) -> tuple[float, str | None]:
    """Bounded xPts nudge: +/-10% max, scaled by confidence, >=2 sources required."""
    if adjustment is None or not adjustment.sentiment:
        return 1.0, None
    if adjustment.n_sources < MIN_SOURCES_FOR_SENTIMENT:
        return 1.0, None
    swing = adjustment.sentiment * adjustment.confidence * MAX_SENTIMENT_SWING
    swing = max(-MAX_SENTIMENT_SWING, min(MAX_SENTIMENT_SWING, swing))
    if abs(swing) < 0.005:
        return 1.0, None
    reason = (
        f"Crowd sentiment {adjustment.sentiment:+.2f} across {adjustment.n_sources} sources "
        f"(confidence {adjustment.confidence:.0%}) -> xPts {swing:+.1%}"
    )
    if adjustment.reasons:
        reason += ": " + "; ".join(adjustment.reasons[:2])
    return 1.0 + swing, reason


# --------------------------------------------------------------------------
# Source gathering (all best-effort)
# --------------------------------------------------------------------------
@dataclass
class Document:
    source: str
    kind: str          # "official" | "youtube" | "news"
    title: str
    url: str
    published: str
    text: str
    rank: int | None = None      # creator trust rank, 1 = most trusted


def _recent_cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


class TranscriptsBlocked(RuntimeError):
    """YouTube is refusing transcript requests from this IP."""


CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")

# A YouTube channel page mentions many channel ids — recommended channels,
# featured shelves, the lot — and the first one is very often NOT this channel.
# Only these three markers identify the page's own channel, so they are the only
# ones trusted. Getting this wrong silently pulls in a stranger's videos.
_AUTHORITATIVE_ID_PATTERNS = (
    re.compile(r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'<meta\s+property="og:url"\s+content="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"rssUrl":"https://www\.youtube\.com/feeds/videos\.xml\?channel_id=(UC[\w-]{22})"'),
)
_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]+)"')
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


@dataclass
class Channel:
    """One configured creator. `source` is whatever the user wrote.

    `rank` is the position in `youtube_channels` (1 = most trusted). It decides
    who keeps their slot when the transcript budget runs out, and is passed to
    Gemini so disagreements between creators can be weighted.
    """

    source: str
    name: str
    channel_id: str | None = None
    rank: int | None = None


def _display_name(source: str) -> str:
    """A readable fallback name from a URL or handle."""
    text = source.strip().rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.lstrip("@") or source


def normalise_channel(entry: Any) -> Channel | None:
    """Accept a URL, an @handle, a bare UC... id, or a dict with any of those."""
    if entry is None:
        return None
    if isinstance(entry, str):
        source = entry.strip()
        if not source:
            return None
        if CHANNEL_ID_RE.match(source):
            return Channel(source, _display_name(source), source)
        return Channel(source, _display_name(source))
    if isinstance(entry, dict):
        channel_id = str(entry.get("channel_id") or "").strip() or None
        if channel_id and not CHANNEL_ID_RE.match(channel_id):
            log.warning("ignoring malformed channel_id %r", channel_id)
            channel_id = None
        source = str(
            entry.get("url") or entry.get("handle") or channel_id or ""
        ).strip()
        if not source:
            return None
        name = str(entry.get("name") or "").strip() or _display_name(source)
        return Channel(source, name, channel_id)
    log.warning("ignoring unrecognised youtube_channels entry: %r", entry)
    return None


def channel_page_url(source: str) -> str:
    text = source.strip()
    if text.startswith("http"):
        return text
    if CHANNEL_ID_RE.match(text):
        return f"https://www.youtube.com/channel/{text}"
    if not text.startswith("@"):
        text = "@" + text
    return f"https://www.youtube.com/{text}"


def scrape_channel_id(source: str) -> tuple[str, str] | None:
    """Fetch a channel page and pull out (channel_id, display name)."""
    import html

    import requests

    url = channel_page_url(source)
    try:
        response = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not reach %s: %s", url, exc)
        return None
    if response.status_code != 200:
        log.warning("channel page %s returned HTTP %s", url, response.status_code)
        return None
    channel_id = None
    for pattern in _AUTHORITATIVE_ID_PATTERNS:
        match = pattern.search(response.text)
        if match:
            channel_id = match.group(1)
            break
    if not channel_id:
        log.warning(
            "no authoritative channel id on %s — refusing to guess from the page's "
            "other channel references", url,
        )
        return None
    title = _OG_TITLE.search(response.text)
    # og:title arrives HTML-escaped ("Let&#39;s Talk FPL").
    name = html.unescape(title.group(1)) if title else _display_name(source)
    return channel_id, name


def resolve_channel_id(conn: sqlite3.Connection | None, channel: Channel) -> str | None:
    """Turn a URL/handle into a UC... id, caching the result in SQLite.

    Resolution costs one page fetch, so it happens once per channel ever rather
    than on every refresh.
    """
    if channel.channel_id:
        return channel.channel_id

    if conn is not None:
        row = conn.execute(
            "SELECT channel_id, name FROM youtube_channels WHERE source = ?",
            (channel.source,),
        ).fetchone()
        if row and row["channel_id"]:
            channel.channel_id = row["channel_id"]
            if row["name"] and channel.name == _display_name(channel.source):
                channel.name = row["name"]
            return channel.channel_id

    found = scrape_channel_id(channel.source)
    if not found:
        return None
    channel_id, name = found
    channel.channel_id = channel_id
    if channel.name == _display_name(channel.source):
        channel.name = name
    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO youtube_channels (source, channel_id, name, resolved_at) "
            "VALUES (?,?,?,?)",
            (channel.source, channel_id, channel.name, utcnow()),
        )
    log.info("resolved %s -> %s (%s)", channel.source, channel_id, channel.name)
    return channel_id


def _channel_candidates(
    channel: Channel, cutoff: datetime, max_videos: int
) -> list[dict[str, str]]:
    """Recent videos for one channel, newest first, from its RSS feed."""
    import feedparser

    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.channel_id}"
    try:
        feed = feedparser.parse(feed_url)
    except Exception as exc:  # noqa: BLE001 - never let a source kill the refresh
        log.warning("YouTube feed %s failed: %s", channel.name, exc)
        return []

    out: list[dict[str, str]] = []
    for entry in list(feed.entries or []):
        if len(out) >= max_videos:
            break
        published = _entry_published(entry)
        if published and published < cutoff:
            continue
        video_id = getattr(entry, "yt_videoid", None) or _video_id_from_link(
            getattr(entry, "link", "")
        )
        if not video_id:
            continue
        out.append(
            {
                "video_id": video_id,
                "title": getattr(entry, "title", "") or video_id,
                "url": getattr(entry, "link", "") or f"https://youtu.be/{video_id}",
                "published": published.isoformat() if published else "",
            }
        )
    return out


def fetch_youtube_documents(
    conn: sqlite3.Connection | None,
    channels: Sequence[Any],
    lookback_days: int,
    max_videos: int = 4,
    max_transcripts: int = 12,
    delay_seconds: float = 2.0,
) -> tuple[list[Document], list[dict[str, Any]]]:
    """Transcripts from the configured creators, and a per-channel status report.

    Videos are taken **round-robin** across channels, not channel by channel, so
    one prolific creator can't consume the whole transcript budget and leave the
    others unheard. YouTube rate-limits transcript scraping hard, so requests are
    spaced out, capped, and abandoned entirely on the first sign of a block.
    """
    docs: list[Document] = []
    status: list[dict[str, Any]] = []
    try:
        import feedparser  # noqa: F401 - imported for the clear error if missing
    except ImportError:
        log.warning("feedparser missing — skipping YouTube.")
        return docs, status
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log.warning("youtube-transcript-api missing — skipping YouTube transcripts.")
        return docs, status

    cutoff = _recent_cutoff(lookback_days)

    # Phase 1 — resolve each channel and list its recent videos.
    queues: list[tuple[Channel, list[dict[str, str]]]] = []
    for position, entry in enumerate(channels, start=1):
        channel = normalise_channel(entry)
        if channel is None:
            continue
        channel.rank = position
        record: dict[str, Any] = {
            "name": channel.name, "source": channel.source, "rank": position,
        }
        if not resolve_channel_id(conn, channel):
            record.update(state="unresolved", videos=0, transcripts=0)
            status.append(record)
            log.warning("could not resolve YouTube channel %r", channel.source)
            continue
        record["name"] = channel.name
        record["channel_id"] = channel.channel_id
        candidates = _channel_candidates(channel, cutoff, max_videos)
        record.update(state="ok", videos=len(candidates), transcripts=0)
        status.append(record)
        if candidates:
            queues.append((channel, candidates))

    # Phase 2 — interleave so every creator is represented.
    ordered: list[tuple[Channel, dict[str, str]]] = []
    depth = max((len(q) for _, q in queues), default=0)
    for i in range(depth):
        for channel, candidates in queues:
            if i < len(candidates):
                ordered.append((channel, candidates[i]))

    # Phase 3 — fetch transcripts politely.
    by_name = {record.get("name"): record for record in status}
    attempts = 0
    for channel, video in ordered:
        if len(docs) >= max_transcripts or attempts >= max_transcripts * 2:
            break
        if attempts:
            time.sleep(delay_seconds)
        attempts += 1
        try:
            text = _fetch_transcript(YouTubeTranscriptApi, video["video_id"])
        except TranscriptsBlocked as exc:
            log.warning(
                "YouTube blocked transcript requests from this IP (%s) — stopping "
                "after %d transcripts. It normally clears within the hour.",
                exc, len(docs),
            )
            for record in status:
                record.setdefault("note", "stopped early: YouTube IP block")
            break
        if not text:
            continue
        docs.append(
            Document(
                source=channel.name,
                kind="youtube",
                title=video["title"],
                url=video["url"],
                published=video["published"],
                text=text,
                rank=channel.rank,
            )
        )
        record = by_name.get(channel.name)
        if record is not None:
            record["transcripts"] = record.get("transcripts", 0) + 1

    heard = sum(1 for r in status if r.get("transcripts"))
    log.info(
        "YouTube: %d transcripts from %d of %d configured creators",
        len(docs), heard, len(status),
    )
    for record in status:
        if record.get("state") == "ok" and not record.get("transcripts"):
            log.warning(
                "no transcript obtained from %s (%d recent videos)",
                record["name"], record.get("videos", 0),
            )
    return docs, status


BLOCK_MARKERS = ("ipblocked", "requestblocked", "too many requests", "blocking requests from your ip")


def _fetch_transcript(api_cls, video_id: str, languages: Sequence[str] = ("en", "en-GB", "en-US")) -> str:
    """Transcript text, across the several API shapes this library has shipped.

    Raises TranscriptsBlocked when YouTube is rate-limiting us, so the caller can
    stop rather than burn through the rest of the list against a closed door.
    """
    try:
        # 1.x instance API
        if hasattr(api_cls, "fetch") and not isinstance(getattr(api_cls, "fetch", None), staticmethod):
            try:
                fetched = api_cls().fetch(video_id, languages=list(languages))
                snippets = getattr(fetched, "snippets", fetched)
                return " ".join(
                    getattr(s, "text", None) or s.get("text", "") for s in snippets
                ).strip()
            except TypeError:
                pass
        # 0.x classmethod API
        if hasattr(api_cls, "get_transcript"):
            parts = api_cls.get_transcript(video_id, languages=list(languages))
            return " ".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:  # noqa: BLE001 - transcripts are frequently disabled
        blob = f"{type(exc).__name__} {exc}".lower()
        if any(marker in blob for marker in BLOCK_MARKERS):
            raise TranscriptsBlocked(type(exc).__name__) from exc
        log.debug("no transcript for %s: %s", video_id, exc)
    return ""


def _video_id_from_link(link: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", link or "")
    return match.group(1) if match else ""


def _entry_published(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_news_documents(
    feeds: Sequence[dict[str, str]], lookback_days: int, max_articles_per_feed: int = 8
) -> list[Document]:
    """RSS headlines plus article body text where it can be extracted."""
    docs: list[Document] = []
    try:
        import feedparser
    except ImportError:
        log.warning("feedparser missing — skipping news feeds.")
        return docs

    cutoff = _recent_cutoff(lookback_days)
    for feed_cfg in feeds:
        url = (feed_cfg or {}).get("url")
        name = (feed_cfg or {}).get("name") or url or "news"
        if not url:
            continue
        try:
            feed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS %s failed: %s", name, exc)
            continue
        count = 0
        for entry in list(feed.entries or []):
            if count >= max_articles_per_feed:
                break
            published = _entry_published(entry)
            if published and published < cutoff:
                continue
            link = getattr(entry, "link", "")
            summary = re.sub(r"<[^>]+>", " ", getattr(entry, "summary", "") or "")
            body = _extract_article(link)
            text = (body or summary or "").strip()
            if len(text) < 80:
                continue
            docs.append(
                Document(
                    source=name,
                    kind="news",
                    title=getattr(entry, "title", "") or link,
                    url=link,
                    published=published.isoformat() if published else "",
                    text=text,
                )
            )
            count += 1
    log.info("News: %d articles gathered", len(docs))
    return docs


def _extract_article(url: str) -> str:
    if not url:
        return ""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        return (trafilatura.extract(downloaded, include_comments=False) or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("article extraction failed for %s: %s", url, exc)
        return ""


def official_flag_documents(conn: sqlite3.Connection) -> list[Document]:
    """FPL's own injury/availability news — the highest-trust source we have."""
    lines = []
    for row in conn.execute(
        "SELECT p.web_name, p.first_name, p.second_name, t.name AS team, p.status, "
        "p.news, p.chance_of_playing_next_round AS chance "
        "FROM players p LEFT JOIN teams t ON t.id = p.team "
        "WHERE (p.news IS NOT NULL AND p.news <> '') OR p.status <> 'a'"
    ):
        chance = "" if row["chance"] is None else f" ({row['chance']}% chance of playing)"
        lines.append(
            f"- {row['first_name']} {row['second_name']} ({row['web_name']}, {row['team']}): "
            f"status={row['status']}{chance}. {row['news'] or ''}".strip()
        )
    if not lines:
        return []
    return [
        Document(
            source="Official FPL",
            kind="official",
            title="Official FPL availability flags",
            url="https://fantasy.premierleague.com/",
            published=utcnow(),
            text="\n".join(lines),
        )
    ]


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an information-extraction service for a Fantasy Premier League tool.

You do NOT pick teams, captains or transfers. You only extract what the supplied
sources SAY about players, as structured data. All FPL mathematics is done
elsewhere.

Return STRICT JSON matching exactly this schema, with no prose and no markdown
code fences:

{
  "players": [
    {
      "player_name": "string (spelled as written in the sources)",
      "fpl_id_guess": null,
      "availability": "fit | doubt | injured | suspended | unknown",
      "start_probability_hint": 0.0,
      "sentiment": 0.0,
      "predicted_role_change": "string or null",
      "reasons": ["short strings, each citing which source said what"],
      "confidence": 0.0
    }
  ],
  "general_notes": ["press conference nuggets, rotation hints, tactical notes"]
}

Rules:
- Only include a player if a source actually discusses them. Never invent players.
- "sentiment" is -1.0 (strongly negative) to 1.0 (strongly positive) about their
  short-term FPL prospects.
- "start_probability_hint" is 0.0-1.0: how likely the sources imply they start
  the next match. Use null if the sources say nothing about it.
- "confidence" is 0.0-1.0: how strong and consistent the evidence is.
- Treat the "Official FPL" source as authoritative on injuries and suspensions.
- Sources of kind "youtube" are FPL creators the user follows deliberately.
  Capture what they say about expected line-ups, rotation, set-piece duty, price
  moves and form — that analysis is the reason they were included. Where a
  creator's view conflicts with an official flag, report BOTH in "reasons" and
  let the confidence score reflect the disagreement.
- Each entry in "reasons" must name its source, e.g. "Let's Talk FPL: expected to
  be rotated after midweek".
- Output JSON only."""


def build_prompt(documents: Sequence[Document], max_chars_per_doc: int = 6000) -> str:
    """Assemble the batched prompt, declaring the creator trust order up front."""
    parts = ["Sources follow. Extract player intel as specified.\n"]

    ranked = sorted(
        {(d.rank, d.source) for d in documents if d.rank},
        key=lambda pair: pair[0],
    )
    if ranked:
        listing = "\n".join(f"  {rank}. {name}" for rank, name in ranked)
        parts.append(
            "\nThe user follows these FPL creators, listed most-trusted first:\n"
            f"{listing}\n"
            "When two creators disagree, lean toward the higher-ranked one, record "
            "BOTH views in 'reasons', and lower 'confidence' to reflect the "
            "disagreement. Official FPL still outranks every creator on injuries "
            "and suspensions.\n"
        )

    for i, doc in enumerate(documents, 1):
        text = doc.text[:max_chars_per_doc]
        rank = f" | creator rank {doc.rank}" if doc.rank else ""
        parts.append(
            f"\n--- SOURCE {i} | {doc.source} | {doc.kind}{rank} | {doc.published} ---\n"
            f"TITLE: {doc.title}\n{text}\n"
        )
    return "".join(parts)


def _strip_fences(text: str) -> str:
    """Gemini sometimes wraps JSON in ```json fences despite instructions."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> str:
    """Pull the outermost JSON value out of any surrounding prose."""
    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append((start, text[start : end + 1]))
    if not candidates:
        return text
    # Whichever structure starts first is the real payload.
    return min(candidates)[1]


def parse_response(text: str) -> "CrowdResponse":
    """Parse + validate Gemini output. Raises on unusable input.

    Tolerant of the three things models actually do wrong: markdown fences,
    a sentence of preamble, and returning the players array on its own.
    """
    if not PYDANTIC_AVAILABLE:
        raise RuntimeError("pydantic is required to validate crowd intel")
    cleaned = _strip_fences(text)
    try:
        payload = json.loads(cleaned)
    except ValueError:
        payload = json.loads(_extract_json(cleaned))
    if isinstance(payload, list):
        # A bare array is the players list — keep the data rather than drop it.
        payload = {"players": payload}
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object or array at the top level")
    return CrowdResponse.model_validate(payload)


def call_gemini(
    api_key: str, model: str, documents: Sequence[Document], timeout: float = 180.0
) -> tuple["CrowdResponse", str]:
    """One batched call, with a single 'fix your JSON' retry. Returns (parsed, raw)."""
    from google import genai
    from google.genai import types

    # HttpOptions.timeout is in milliseconds. Without it the client waits
    # indefinitely, which on a cron schedule means a hung call can still be
    # holding the job when tomorrow's run starts.
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(timeout * 1000)),
    )
    prompt = build_prompt(documents)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        temperature=0.1,
        max_output_tokens=32768,
    )

    response = client.models.generate_content(
        model=model, contents=prompt, config=config
    )
    raw = (response.text or "").strip()
    try:
        return parse_response(raw), raw
    except Exception as exc:  # noqa: BLE001 - retry once with the error attached
        # `exc` is unbound once the except block exits, so keep the message.
        error_message = f"{type(exc).__name__}: {exc}"
        log.warning("Gemini returned unparseable JSON (%s); retrying once.", error_message)

    repair = (
        "Your previous reply was not valid JSON for the required schema.\n"
        f"The error was: {error_message}\n\n"
        "Return ONLY the corrected JSON object — no prose, no markdown fences.\n\n"
        "Previous reply:\n" + raw[:20000]
    )
    response = client.models.generate_content(
        model=model, contents=repair, config=config
    )
    raw2 = (response.text or "").strip()
    return parse_response(raw2), raw2


# --------------------------------------------------------------------------
# Name resolution
# --------------------------------------------------------------------------
def build_name_index(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Candidate (name, player_id) pairs: web name, full name, and surname."""
    index: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT id, web_name, first_name, second_name FROM players"
    ):
        pid = row["id"]
        web = (row["web_name"] or "").strip()
        first = (row["first_name"] or "").strip()
        second = (row["second_name"] or "").strip()
        for candidate in {web, f"{first} {second}".strip(), second}:
            if candidate:
                index.append((candidate.lower(), pid))
    return index


def resolve_players(
    conn: sqlite3.Connection, intel: Iterable[Any]
) -> tuple[list[CrowdAdjustment], list[str]]:
    """Fuzzy-match reported names to FPL element ids.

    Returns (resolved adjustments, unresolved names). Multiple mentions of the
    same player are merged: worst availability wins, sentiment is averaged, and
    n_sources counts the distinct mentions (which gates the sentiment nudge).
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        log.warning("rapidfuzz missing — cannot resolve crowd intel to players.")
        return [], [str(getattr(i, "player_name", i)) for i in intel]

    index = build_name_index(conn)
    choices = [name for name, _ in index]
    valid_ids = {r["id"] for r in conn.execute("SELECT id FROM players")}

    merged: dict[int, CrowdAdjustment] = {}
    unresolved: list[str] = []
    severity = {"unknown": 0, "fit": 1, "doubt": 2, "injured": 3, "suspended": 3}

    for item in intel:
        name = (getattr(item, "player_name", "") or "").strip()
        if not name:
            continue
        pid = None
        score = 100.0
        guess = getattr(item, "fpl_id_guess", None)
        if guess and int(guess) in valid_ids:
            pid = int(guess)
        else:
            match = process.extractOne(name.lower(), choices, scorer=fuzz.WRatio)
            if match and match[1] >= FUZZY_MATCH_THRESHOLD:
                pid = index[match[2]][1]
                score = float(match[1])
        if pid is None:
            unresolved.append(name)
            continue

        existing = merged.get(pid)
        if existing is None:
            merged[pid] = CrowdAdjustment(
                player_id=pid,
                player_name=name,
                availability=item.availability,
                start_probability_hint=item.start_probability_hint,
                sentiment=float(item.sentiment or 0.0),
                confidence=float(item.confidence or 0.0),
                n_sources=1,
                predicted_role_change=item.predicted_role_change,
                reasons=list(item.reasons or []),
                match_score=score,
            )
            continue

        # Merge: the most pessimistic availability wins.
        if severity.get(item.availability, 0) > severity.get(existing.availability, 0):
            existing.availability = item.availability
        hint = item.start_probability_hint
        if hint is not None:
            existing.start_probability_hint = (
                hint if existing.start_probability_hint is None
                else min(existing.start_probability_hint, hint)
            )
        n = existing.n_sources
        existing.sentiment = (existing.sentiment * n + float(item.sentiment or 0.0)) / (n + 1)
        existing.confidence = max(existing.confidence, float(item.confidence or 0.0))
        existing.n_sources = n + 1
        existing.reasons.extend(item.reasons or [])
        existing.predicted_role_change = (
            existing.predicted_role_change or item.predicted_role_change
        )

    for adj in merged.values():
        adj.reasons = adj.reasons[:8]
    return list(merged.values()), unresolved


# --------------------------------------------------------------------------
# Persistence + orchestration
# --------------------------------------------------------------------------
def store_run(
    conn: sqlite3.Connection,
    model: str,
    documents: Sequence[Document],
    ok: bool,
    error: str = "",
    raw: str = "",
    general_notes: Sequence[str] = (),
) -> int:
    cur = conn.execute(
        "INSERT INTO crowd_intel_runs (run_at, model, n_documents, ok, error, raw_response, general_notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (utcnow(), model, len(documents), 1 if ok else 0, error[:2000],
         (raw or "")[:200000], jdump(list(general_notes))),
    )
    run_id = int(cur.lastrowid)
    for doc in documents:
        conn.execute(
            "INSERT INTO crowd_documents (run_id, source, kind, title, url, published, text) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, doc.source, doc.kind, doc.title[:500], doc.url[:1000],
             doc.published, doc.text[:100000]),
        )
    return run_id


def store_adjustments(
    conn: sqlite3.Connection, run_id: int, adjustments: Sequence[CrowdAdjustment],
    unresolved: Sequence[str],
) -> None:
    now = utcnow()
    for adj in adjustments:
        conn.execute(
            "INSERT INTO crowd_intel (run_id, player_id, player_name, match_score, "
            "availability, start_probability_hint, sentiment, predicted_role_change, "
            "confidence, n_sources, reasons, resolved, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (run_id, adj.player_id, adj.player_name, adj.match_score, adj.availability,
             adj.start_probability_hint, adj.sentiment, adj.predicted_role_change,
             adj.confidence, adj.n_sources, jdump(adj.reasons), now),
        )
    for name in unresolved:
        conn.execute(
            "INSERT INTO crowd_intel (run_id, player_id, player_name, availability, "
            "reasons, resolved, created_at) VALUES (?,NULL,?,'unknown','[]',0,?)",
            (run_id, name, now),
        )


def latest_adjustments(conn: sqlite3.Connection) -> dict[int, CrowdAdjustment]:
    """Adjustments from the most recent successful run, keyed by player id."""
    row = conn.execute(
        "SELECT id FROM crowd_intel_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    out: dict[int, CrowdAdjustment] = {}
    for r in conn.execute(
        "SELECT * FROM crowd_intel WHERE run_id = ? AND resolved = 1 AND player_id IS NOT NULL",
        (row["id"],),
    ):
        out[r["player_id"]] = CrowdAdjustment(
            player_id=r["player_id"],
            player_name=r["player_name"],
            availability=r["availability"] or "unknown",
            start_probability_hint=r["start_probability_hint"],
            sentiment=r["sentiment"] or 0.0,
            confidence=r["confidence"] or 0.0,
            n_sources=r["n_sources"] or 0,
            predicted_role_change=r["predicted_role_change"],
            reasons=json.loads(r["reasons"] or "[]"),
            match_score=r["match_score"] or 0.0,
        )
    return out


def gather_documents(conn: sqlite3.Connection, cfg) -> list[Document]:
    """Every source, in priority order.

    Order matters because `crowd_max_documents` truncates the tail: official
    flags first (highest trust), then the creators you actually follow, then
    general news to fill whatever budget is left. News must never be able to
    push your creators out of the prompt.
    """
    docs = official_flag_documents(conn)

    try:
        transcripts, status = fetch_youtube_documents(
            conn,
            cfg.youtube_channels,
            cfg.crowd_lookback_days,
            max_videos=cfg.crowd_max_videos_per_channel,
            max_transcripts=cfg.crowd_max_transcripts,
            delay_seconds=cfg.crowd_transcript_delay,
        )
        docs += transcripts
        set_meta(conn, "crowd_channel_status", jdump(status))
    except Exception as exc:  # noqa: BLE001
        log.warning("YouTube gathering failed: %s", exc)

    remaining = max(0, cfg.crowd_max_documents - len(docs))
    if remaining:
        try:
            docs += fetch_news_documents(cfg.news_rss_feeds, cfg.crowd_lookback_days)[:remaining]
        except Exception as exc:  # noqa: BLE001
            log.warning("news gathering failed: %s", exc)
    else:
        log.info("document budget filled by creators — skipping news this run")

    return docs[: cfg.crowd_max_documents]


def refresh_crowd_intel(conn: sqlite3.Connection, cfg) -> dict[str, Any]:
    """Full crowd-intel stage. Never raises — the engine runs without it."""
    result: dict[str, Any] = {"ok": False, "documents": 0, "players": 0, "unresolved": 0}
    api_key = cfg.gemini_api_key()
    if not api_key:
        result["error"] = "no Gemini API key configured"
        log.info("Crowd intel skipped: %s", result["error"])
        log_fetch(conn, "crowd", "gemini", False, result["error"])
        return result

    documents = gather_documents(conn, cfg)
    result["documents"] = len(documents)
    if not documents:
        result["error"] = "no source documents gathered"
        log.warning("Crowd intel skipped: %s", result["error"])
        store_run(conn, cfg.gemini_model, [], False, result["error"])
        log_fetch(conn, "crowd", "gemini", False, result["error"])
        return result

    try:
        parsed, raw = call_gemini(api_key, cfg.gemini_model, documents)
    except Exception as exc:  # noqa: BLE001 - optional layer, must not break refresh
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("Gemini call failed: %s", result["error"])
        store_run(conn, cfg.gemini_model, documents, False, result["error"])
        log_fetch(conn, "crowd", "gemini", False, result["error"])
        return result

    adjustments, unresolved = resolve_players(conn, parsed.players)
    run_id = store_run(
        conn, cfg.gemini_model, documents, True, "", raw, parsed.general_notes
    )
    store_adjustments(conn, run_id, adjustments, unresolved)
    result.update(
        ok=True, players=len(adjustments), unresolved=len(unresolved), run_id=run_id
    )
    log_fetch(
        conn, "crowd", "gemini", True,
        f"{len(documents)} docs, {len(adjustments)} players, {len(unresolved)} unresolved",
    )
    log.info(
        "Crowd intel: %d documents -> %d players (%d names unresolved)",
        len(documents), len(adjustments), len(unresolved),
    )
    return result
