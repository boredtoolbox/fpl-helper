"""Configuration loading.

Reads config.yaml (falling back to config.example.yaml so a fresh checkout can
still boot), resolves relative paths against the project root, and reads the
Gemini key out of its own file so it never lands in the repo.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def _bundle_root() -> Path:
    """Read-only resources: templates, the shipped CSS/JS, config.example.yaml.

    Under PyInstaller these are unpacked into a temporary directory that
    `sys._MEIPASS` points at. From a source checkout they are simply the
    checkout.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent.parent


def _state_root() -> Path:
    """Everything writable: config.yaml, the database, logs, secrets, images.

    A source checkout keeps its state inside the checkout, exactly as it always
    has. That is not a stylistic choice — the systemd unit grants write access
    to `<checkout>/data` and `<checkout>/logs` and nothing else, and the cron
    jobs `cd` into the checkout, so moving state out from under a server
    install would break both.

    Only a frozen build relocates, because the directory it runs from is a
    temporary extraction that is deleted when the process exits.
    """
    override = os.environ.get("FPL_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fpl-helper"


BUNDLE_ROOT = _bundle_root()
STATE_ROOT = _state_root()

# Kits and crests are downloaded by the refresh job, which makes them state
# rather than bundled assets — but they are served from the same /static URL
# space as the CSS. In a source checkout this is the very same directory, so
# the two only actually diverge inside a frozen build.
IMAGE_ROOT = STATE_ROOT / "static"

# The old name for the writable root, kept because relative paths in config.yaml
# have always resolved against it.
PROJECT_ROOT = STATE_ROOT


@dataclass
class Config:
    team_ids: list[int] = field(default_factory=list)
    team_labels: dict[int, str] = field(default_factory=dict)
    gemini_api_key_file: Path = STATE_ROOT / "secrets" / "gemini_key.txt"
    gemini_model: str = "gemini-3.5-flash-lite"
    # None disables the in-process scheduler entirely — cron owns the schedule.
    refresh_hour: int | None = 6
    db_path: Path = STATE_ROOT / "data" / "fpl.db"
    historical_data_dir: Path = STATE_ROOT / "data" / "Fantasy-Premier-League"
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
    # Where this came from, and whether it was the shipped example rather than
    # the user's own file. Both drive first-run setup; neither is a setting.
    config_path: Path | None = None
    from_example: bool = False

    @property
    def needs_setup(self) -> bool:
        """True when nobody has said which FPL team this install is for.

        The example config carries placeholder entry ids, so falling back to it
        is not "configured" — it is an install that would render empty pages
        for somebody else's team numbers.
        """
        return self.from_example or not self.team_ids

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


class ConfigError(Exception):
    """A config file that exists but cannot be used, with a message for humans."""


def _resolve(value: Any, default: Path) -> Path:
    if value in (None, ""):
        return default
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (STATE_ROOT / path).resolve()


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


def _yaml_error_message(cfg_path: Path, exc: yaml.YAMLError) -> str:
    """Turn a YAML traceback into something you can act on.

    Nearly every report of this is indentation: a `key: value` pair nested one
    space under a top-level key, usually from uncommenting a template line and
    leaving the leading space behind. Point at the offending line and say so.
    """
    lines = [f"Could not parse {cfg_path}:"]
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or "invalid YAML"
    if mark is not None:
        source = cfg_path.read_text(encoding="utf-8").splitlines()
        lineno = mark.line + 1
        lines.append(f"  line {lineno}, column {mark.column + 1}: {problem}")
        if 0 <= mark.line < len(source):
            lines.append(f"    {source[mark.line]}")
            lines.append(f"    {' ' * mark.column}^")
    else:
        lines.append(f"  {problem}")
    lines.append(
        "Check the indentation on that line. Top-level keys (team_ids, "
        "team_labels, ...) start at column 1 with no leading spaces; entries "
        "underneath them are indented by exactly two spaces. Note that "
        "`team_labels: {}` is an empty mapping -- to add labels, drop the `{}` "
        "and put each `id: \"name\"` on its own two-space-indented line below. "
        "config.example.yaml is a working reference."
    )
    return "\n".join(lines)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config.yaml, falling back to config.example.yaml."""
    from_example = False
    if path is not None:
        cfg_path = Path(path)
    else:
        cfg_path = STATE_ROOT / "config.yaml"
        if not cfg_path.exists():
            example = BUNDLE_ROOT / "config.example.yaml"
            if example.exists():
                log.warning(
                    "config.yaml not found — using %s. Copy it and fill in your team IDs.",
                    example.name,
                )
                cfg_path = example
                from_example = True

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        # Open the file rather than pass its text, so YAML's error marks name
        # the actual path instead of "<unicode string>".
        try:
            with cfg_path.open(encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise ConfigError(_yaml_error_message(cfg_path, exc)) from exc
        if isinstance(loaded, dict):
            raw = loaded
        elif loaded is not None:
            raise ConfigError(
                f"{cfg_path} should be a set of `key: value` settings, but it "
                f"parsed as {type(loaded).__name__}. Compare it with "
                f"config.example.yaml."
            )
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
        historical_data_dir=_resolve(
            # `historical_repo_dir` is the old name, from when this was a git
            # checkout rather than a handful of downloaded CSVs. Existing
            # config files keep working.
            raw.get("historical_data_dir", raw.get("historical_repo_dir")),
            defaults.historical_data_dir,
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
        config_path=cfg_path,
        from_example=from_example,
    )
