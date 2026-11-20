"""Pitch-shift, time-stretch and AIFF preview with ffmpeg.

Pitch and tempo are independent. The old header rewrite sped the file up
to change its key, which also changed the tempo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from hoarder.livebpm import tempo_ratio

# Render cap. The original file is only read; this limits the in-memory WAV.
MAX_PREVIEW_BYTES = 80 * 1024 * 1024

_CACHE: dict[tuple, bytes] = {}
_CACHE_ORDER: list[tuple] = []
_CACHE_MAX = 6


def match_sample(
    path: Path,
    semitones: int,
    project_bpm: float | None,
    sample_bpm: float | None,
    stretch: float = 1.0,
) -> bytes | None:
    """Wav bytes for a pitch and/or tempo change. None means play the file as stored.

    stretch is 1, 1/2, or 2 on top of the tempo match.
    """
    tempo = stretch
    if project_bpm and sample_bpm:
        tempo = tempo_ratio(sample_bpm, project_bpm) * stretch
    if semitones == 0 and abs(tempo - 1) < 0.005:
        return None
    return _cached(path, semitones, tempo)


def browser_wav(path: Path) -> bytes:
    """The file unchanged, as a WAV. Chrome can't play AIFF."""
    return _cached(path, 0, 1.0)


def _cached(path: Path, semitones: int, tempo: float) -> bytes:
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        raise ValueError("file is too large to render")
    key = (str(path), path.stat().st_mtime, semitones, round(tempo, 4))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    rendered = _render(path, semitones, tempo)
    _CACHE[key] = rendered
    _CACHE_ORDER.append(key)
    while len(_CACHE_ORDER) > _CACHE_MAX:
        _CACHE.pop(_CACHE_ORDER.pop(0), None)
    return rendered


def _render(path: Path, semitones: int, tempo: float) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is not installed")
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if semitones or tempo != 1.0:
        pitch = 2 ** (semitones / 12)
        args += ["-af", f"rubberband=tempo={tempo:.6f}:pitch={pitch:.6f}"]
    # 24-bit so a 24-bit source keeps its depth.
    args += ["-f", "wav", "-acodec", "pcm_s24le", "pipe:1"]
    proc = subprocess.run(
        args,
        capture_output=True,
        timeout=45,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"RIFF"):
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        message = detail[-1] if detail else "couldn't render the sample"
        raise ValueError(message)
    return _finish_wav(proc.stdout)


def _finish_wav(data: bytes) -> bytes:
    """A piped WAV leaves the chunk sizes as unknown. Fill them in."""
    if len(data) < 12:
        return data
    buf = bytearray(data)
    buf[4:8] = (len(buf) - 8).to_bytes(4, "little")
    pos = 12
    while pos + 8 <= len(buf):
        cid = bytes(buf[pos : pos + 4])
        size = int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if cid == b"data" or size == 0xFFFFFFFF:
            buf[pos + 4 : pos + 8] = (len(buf) - (pos + 8)).to_bytes(4, "little")
            break
        pos += 8 + size + (size & 1)
    return bytes(buf)
