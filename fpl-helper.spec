# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the downloadable build.

The bundled file list is an **allowlist**, built by naming each thing that
ships. It is deliberately not `('.', '.')`, not `('static', 'static')` and not
anything else that sweeps a directory, because the project directory of anyone
who has actually run this app contains `config.yaml` (real FPL entry ids),
`secrets/gemini_key.txt` (a live API key) and `data/` (their database). A
sweep would publish all three inside the binary.

`tools/check_bundle.py` enforces the same rule independently, before and after
the build. This file is the intent; that script is the check.
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH)

# --- what ships ------------------------------------------------------------
# Enumerated rather than globbed wholesale, and printed into the build log so
# a surprising entry is visible in CI output rather than only inside the exe.
datas = []

for html in sorted((PROJECT / "templates").glob("*.html")):
    datas.append((str(html), "templates"))

for asset in ("style.css", "app.js"):
    path = PROJECT / "static" / asset
    if not path.exists():
        raise SystemExit(f"spec: expected {path} to exist")
    datas.append((str(path), "static"))

# The setup page generates a real config from this, so it has to ship. Its
# team ids are documented placeholders (1234567 / 7654321), not anyone's.
datas.append((str(PROJECT / "config.example.yaml"), "."))

# static/kits and static/crests are deliberately absent: the refresh job
# downloads them into the user's own state directory on first run.

print("spec: bundling %d data files" % len(datas))
for src, dest in datas:
    print("  %s -> %s" % (Path(src).name, dest))

# --- imports PyInstaller cannot see ----------------------------------------
# Reached by string name or lazily, so the import graph misses them.
hiddenimports = [
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.executors.pool",
    "tzlocal",
    "waitress",
    "yaml",
    "feedparser",
    "rapidfuzz",
    "youtube_transcript_api",
    "google.genai",
    "trafilatura",
    "pydantic",
    "pydantic.deprecated.decorator",
]

# --- things that must never be collected -----------------------------------
excludes = [
    "tkinter",
    "pytest",
    "_pytest",
    "IPython",
    "matplotlib",
    "numpy.testing",
]


a = Analysis(
    ["desktop.py"],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="fpl-helper" + (".exe" if sys.platform == "win32" else ""),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX makes antivirus false positives noticeably more likely, and this is
    # already an unsigned binary asking people to trust it.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
