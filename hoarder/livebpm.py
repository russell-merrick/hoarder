"""Project tempo from the Ableton set that is open, and BPM tags on sample names."""

from __future__ import annotations

import gzip
import os
import re
import subprocess
from pathlib import Path

_BPM = re.compile(r"(?i)(?<!\d)(\d{2,3}(?:\.\d+)?)(?=\s*bpm(?:[^a-z]|$))")
_LOAD = re.compile(r'Loading document "([^"]+\.als)"', re.IGNORECASE)
_TEMPO = re.compile(r'<Tempo>.*?<Manual Value="([0-9.]+)"', re.DOTALL)

_status_cache: tuple[tuple, dict] | None = None
_bpm_cache: dict[str, tuple[float, float | None]] = {}


def parse_bpm(name: str) -> float | None:
    match = _BPM.search(name or "")
    if not match:
        return None
    value = float(match.group(1))
    if value < 40 or value > 250:
        return None
    return value


def tempo_ratio(sample_bpm: float, project_bpm: float) -> float:
    """How much to speed the sample up so it lands on the project tempo.

    A ratio outside 0.5–2 is folded by octaves. A 90 BPM loop in a 180 project
    plays at double time, which is the nearest musical match.
    """
    ratio = project_bpm / sample_bpm
    while ratio > 2:
        ratio *= 0.5
    while ratio < 0.5:
        ratio *= 2
    return ratio


def read_set_bpm(path: Path) -> float | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    match = _TEMPO.search(text)
    if not match:
        return None
    value = float(match.group(1))
    if value < 20 or value > 400:
        return None
    return value


def live_is_running() -> bool:
    try:
        output = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in output.splitlines():
        name = line.split(",", 1)[0].strip('"').lower()
        if name.startswith("ableton live"):
            return True
    return False


def newest_log() -> Path | None:
    root = Path(os.environ.get("APPDATA", "")) / "Ableton"
    if not root.is_dir():
        return None
    logs = [path for path in root.glob("Live */Preferences/Log.txt") if path.is_file()]
    if not logs:
        return None
    return max(logs, key=lambda path: path.stat().st_mtime)


def latest_song(log_text: str) -> Path | None:
    found = ""
    for match in _LOAD.finditer(log_text):
        raw = match.group(1)
        if _is_song(raw):
            found = raw
    return Path(found) if found else None


def project_status(running: bool | None = None, log_path: Path | None = None) -> dict:
    """BPM of the set Ableton last loaded, when Live is running.

    The number is the tempo saved in the set. It updates after a save.
    """
    global _status_cache
    detect = running is None and log_path is None
    if detect:
        running = live_is_running()
        log_path = newest_log()
        stamp = log_path.stat().st_mtime if log_path and log_path.is_file() else None
        key = (bool(running), str(log_path) if log_path else "", stamp)
        if _status_cache and _status_cache[0] == key:
            return _status_cache[1]
        result = _status(bool(running), log_path)
        _status_cache = (key, result)
        return result
    return _status(bool(running), log_path)


def _status(running: bool, log_path: Path | None) -> dict:
    empty = {"running": running, "bpm": None, "name": "", "path": ""}
    if not running or log_path is None or not log_path.is_file():
        return empty
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return empty
    song = latest_song(text)
    if song is None or not song.is_file():
        return empty
    return {
        "running": True,
        "bpm": _cached_bpm(song),
        "name": song.stem,
        "path": str(song),
    }


def _cached_bpm(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    hit = _bpm_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    bpm = read_set_bpm(path)
    _bpm_cache[key] = (mtime, bpm)
    return bpm


def _is_song(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    if not lowered.endswith(".als"):
        return False
    if "/defaults/" in lowered or "/builtin/" in lowered:
        return False
    return True
