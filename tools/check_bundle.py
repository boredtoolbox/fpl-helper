#!/usr/bin/env python3
"""Refuse to ship a build that carries the maintainer's own data.

The repository is public and every private thing in it is private only because
`.gitignore` says so. A build does not consult `.gitignore`. Left alone,
PyInstaller would happily sweep `config.yaml` (real FPL entry ids) and
`secrets/gemini_key.txt` (a live API key) into a binary published on the
releases page, and there is no taking that back once someone has downloaded it.

Two modes, run either side of the build:

  --source <dir>    before: the build tree must not contain the private files
                    at all. CI builds from a fresh `git clone`, so this should
                    pass trivially; it fails loudly if somebody ever builds
                    from a working tree instead.

  --binary <file>   after: scan the finished artefact for the byte patterns of
                    a real config. Catches anything that got in by a route the
                    source check did not anticipate.

Neither check knows the maintainer's actual id or key — hard-coding those
would put them in the public repo, which is the thing being prevented. They
work on shape instead: file names for the source check, and for the binary a
search for the marker strings a real config or key file would bring with it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Files that must never be inside a build tree or a bundle. Directories end
# with a slash. These are exactly the entries .gitignore protects.
FORBIDDEN = (
    "config.yaml",
    "context.md",
    "secrets/",
    "data/",
    "logs/",
    ".flask_secret",
    ".env",
)

# Allowed despite matching above: the example config is meant to ship, and its
# placeholder ids are documented fakes.
ALLOWED = ("config.example.yaml",)

PLACEHOLDER_IDS = {"1234567", "7654321"}

# What a leaked artefact would actually look like inside the binary.
BINARY_PATTERNS = (
    # A generated or hand-written config announces itself with these keys, and
    # they should only ever appear in config.example.yaml.
    (rb"# Written by FPL Helper's first-run setup", "a generated config.yaml"),
    (rb"AIza[0-9A-Za-z_\-]{30,}", "a Google API key"),
    (rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.(?:com|org|net|io|uk|me)", "an email address"),
)


def check_source(root: Path) -> list[str]:
    problems = []
    for name in FORBIDDEN:
        target = root / name.rstrip("/")
        if not target.exists():
            continue
        if any(target.name == a for a in ALLOWED):
            continue
        problems.append(
            f"{name} is present in the build tree ({target}). Build from a "
            f"clean `git clone`, not a working tree."
        )
    return problems


def check_binary(path: Path) -> list[str]:
    problems = []
    blob = path.read_bytes()
    for pattern, description in BINARY_PATTERNS:
        for match in re.finditer(pattern, blob):
            found = match.group(0).decode("utf-8", "replace")
            # The example config's placeholders are fine, and so are the
            # example email-ish strings in bundled third-party licences.
            if any(pid in found for pid in PLACEHOLDER_IDS):
                continue
            if description == "an email address" and _is_vendor_email(found, blob, match.start()):
                continue
            problems.append(f"{path.name} contains what looks like {description}: {found!r}")
    return problems


def _is_vendor_email(found: str, blob: bytes, offset: int) -> bool:
    """Dependency metadata is full of maintainer addresses; those are theirs.

    Only an address in a context that looks like this project's own data is
    interesting, so anything sitting inside a packaging metadata block is not.
    """
    window = blob[max(0, offset - 300) : offset + 100].lower()
    return any(
        marker in window
        for marker in (b"author-email", b"maintainer", b"copyright", b"license", b"pypi")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="build tree to check before building")
    parser.add_argument("--binary", type=Path, help="built artefact to check afterwards")
    args = parser.parse_args(argv)

    if not args.source and not args.binary:
        parser.error("give --source, --binary, or both")

    problems: list[str] = []
    if args.source:
        print(f"checking build tree: {args.source}")
        problems += check_source(args.source)
    if args.binary:
        if not args.binary.exists():
            problems.append(f"{args.binary} does not exist")
        else:
            size_mb = args.binary.stat().st_size / (1024 * 1024)
            print(f"checking artefact:   {args.binary} ({size_mb:.0f} MB)")
            problems += check_binary(args.binary)

    if problems:
        print("\nREFUSING TO SHIP:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("clean - no private data found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
