#!/usr/bin/env python3
"""Look up the channel id behind a YouTube URL or @handle.

You normally don't need this — `config.yaml` accepts channel URLs directly and
resolves them itself. Use it to check a link before adding it, or to see why a
channel isn't producing transcripts.

    python tools/find_channel_id.py @LetsTalkFPL
    python tools/find_channel_id.py https://www.youtube.com/@FPLHarry
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.crowd_intel import (  # noqa: E402
    channel_page_url, normalise_channel, scrape_channel_id,
)

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def recent_video_count(channel_id: str) -> int:
    """How many videos the RSS feed lists — 0 means it won't be usable."""
    try:
        import feedparser

        return len(feedparser.parse(FEED.format(channel_id)).entries or [])
    except Exception:  # noqa: BLE001
        return -1


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    failures = 0
    print("youtube_channels:")
    for argument in argv:
        channel = normalise_channel(argument)
        if channel is None:
            print(f"  # unrecognised: {argument}", file=sys.stderr)
            failures += 1
            continue

        channel_id = channel.channel_id
        name = channel.name
        if not channel_id:
            found = scrape_channel_id(channel.source)
            if not found:
                print(
                    f"  # could not resolve {argument} "
                    f"(tried {channel_page_url(channel.source)})",
                    file=sys.stderr,
                )
                failures += 1
                continue
            channel_id, name = found

        count = recent_video_count(channel_id)
        note = f"  # {name}, {count} recent videos" if count > 0 else "  # WARNING: feed is empty"
        # The URL form is what config.yaml wants; the id is shown for reference.
        print(f'  - "{channel.source}"{note}')
        print(f"  #   resolves to {channel_id}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
