"""Configuration loading.

Reads config.yaml (falling back to config.example.yaml so a fresh checkout can
still boot), resolves relative paths against the project root, and reads the
Gemini key out of its own file so it never lands in the repo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


@dataclass
class Config:
    team_ids: list[int] = field(default_factory=list)
    team_labels: dict[int, str] = field(default_factory=dict)
    gemini_api_key_file: Path = PROJECT_ROOT / "secrets" / "gemini_key.txt"
    gemini_model: str = "gemini-3.5-flash-lite"
    # None disables the in-process scheduler entirely — cron owns the schedule.
    refresh_hour: int | None = 6
    db_path: Path = PROJECT_ROOT / "data" / "fpl.db"
    historical_repo_dir: Path = PROJECT_ROOT / "data" / "Fantasy-Premier-League"
    horizon_gws: int = 6
    historical_seasons: list[str] = field(default_factory=lambda: ["2025-26", "2024-25"])
    youtube_channels: list[dict[str, str]] = field(default_factory=list)
    news_rss_feeds: list[dict[str, str]] = field(default_factory=list)
    crowd_lookback_days: int = 3
    crowd_max_documents: int = 40
    # YouTube rate-limits transcript scraping; these keep a daily run under the wire.
    crowd_max_transcripts: int = 12
    crowd_max_videos_per_channel: int = 4
    crowd_transcript_delay: float = 2.0

    def gemini_api_key(self) -> str | None:
        """Read the key at call time so rotating the file needs no restart.

        Takes the first non-empty, non-comment line and ignores the rest. A key
        file that has picked up extra lines would otherwise be sent verbatim as
        an HTTP header, which fails with an opaque "Illegal header value" from
        the transport rather than anything that points at the real problem.
        """
        path = self.gemini_api_key_file
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            log.info("No Gemini key file at %s; crowd intel disabled.", path)
            return None

        lines = [line.strip() for line in raw.splitlines()]
        candidates = [line for line in lines if line and not line.startswith("#")]
        if not candidates:
            log.warning("Gemini key file %s is empty; crowd intel disabled.", path)
            return None

        key = candidates[0]
        if len(candidates) > 1:
            log.warning(
                "Gemini key file %s has %d extra line(s) after the key; using the "
                "first line only. Remove the rest to silence this.",
                path, len(candidates) - 1,
            )
        if any(ch.isspace() for ch in key):
            log.error(
                "The first line of %s contains whitespace, so it is not a valid "
                "API key; crowd intel disabled.", path,
            )
            return None
        return key

    def label_for(self, team_id: int) -> str | None:
        return self.team_labels.get(int(team_id))


def _resolve(value: Any, default: Path) -> Path:
    if value in (None, ""):
        return default
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _optional_hour(value: Any) -> int | None:
    """An hour 0-23, or None when the in-process scheduler should stay off.

    `refresh_hour: null` (or false/"off"/"cron") is how you say "cron runs the
    refresh, not the app". Anything unparseable is treated the same way, loudly,
    rather than silently scheduling at an hour you did not ask for.
    """
    if value is None or value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "off", "cron", "false"}:
        return None
    try:
        hour = int(value)
    except (TypeError, ValueError):
        log.warning("refresh_hour %r is not an hour; disabling the in-process refresh.", value)
        return None
    if not 0 <= hour <= 23:
        log.warning("refresh_hour %s is out of range; disabling the in-process refresh.", hour)
        return None
    return hour


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config.yaml, falling back to config.example.yaml."""
    if path is not None:
        cfg_path = Path(path)
    else:
        cfg_path = PROJECT_ROOT / "config.yaml"
        if not cfg_path.exists():
            example = PROJECT_ROOT / "config.example.yaml"
            if example.exists():
                log.warning(
                    "config.yaml not found — using %s. Copy it and fill in your team IDs.",
                    example.name,
                )
                cfg_path = example

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    else:
        log.warning("No config file found at %s; using defaults.", cfg_path)

    defaults = Config()
    labels_raw = raw.get("team_labels") or {}
    return Config(
        team_ids=[int(t) for t in (raw.get("team_ids") or [])],
        team_labels={int(k): str(v) for k, v in labels_raw.items()},
        gemini_api_key_file=_resolve(
            raw.get("gemini_api_key_file"), defaults.gemini_api_key_file
        ),
        gemini_model=str(raw.get("gemini_model") or defaults.gemini_model),
        refresh_hour=_optional_hour(raw.get("refresh_hour", defaults.refresh_hour)),
        db_path=_resolve(raw.get("db_path"), defaults.db_path),
        historical_repo_dir=_resolve(
            raw.get("historical_repo_dir"), defaults.historical_repo_dir
        ),
        horizon_gws=int(raw.get("horizon_gws", defaults.horizon_gws)),
        historical_seasons=[str(s) for s in (raw.get("historical_seasons") or defaults.historical_seasons)],
        youtube_channels=list(raw.get("youtube_channels") or []),
        news_rss_feeds=list(raw.get("news_rss_feeds") or []),
        crowd_lookback_days=int(raw.get("crowd_lookback_days", defaults.crowd_lookback_days)),
        crowd_max_documents=int(raw.get("crowd_max_documents", defaults.crowd_max_documents)),
        crowd_max_transcripts=int(raw.get("crowd_max_transcripts", defaults.crowd_max_transcripts)),
        crowd_max_videos_per_channel=int(
            raw.get("crowd_max_videos_per_channel", defaults.crowd_max_videos_per_channel)
        ),
        crowd_transcript_delay=float(
            raw.get("crowd_transcript_delay", defaults.crowd_transcript_delay)
        ),
    )
