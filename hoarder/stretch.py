"""Pitch-shift and time-stretch with ffmpeg's rubberband filter.

Pitch and tempo are independent. The old header rewrite sped the file up
to change its key, which also changed the tempo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hoarder.livebpm import parse_bpm, tempo_ratio
from hoarder.probe import MAX_PREVIEW_BYTES

_CACHE: dict[tuple, bytes] = {}
_CACHE_ORDER: list[tuple] = []
_CACHE_MAX = 6


def match_sample(path: Path, semitones: int, project_bpm: float | None) -> bytes | None:
    """Wav bytes for a pitch and/or tempo change. None means play the file as stored."""
    sample_bpm = parse_bpm(path.name)
    tempo = 1.0
    if project_bpm and sample_bpm:
        tempo = tempo_ratio(sample_bpm, project_bpm)
    if semitones == 0 and abs(tempo - 1) < 0.005:
        return None
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        raise ValueError("file is too large to match")
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
    pitch = 2 ** (semitones / 12)
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            f"rubberband=tempo={tempo:.6f}:pitch={pitch:.6f}",
            "-f",
            "wav",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ],
        capture_output=True,
        timeout=45,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"RIFF"):
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        message = detail[-1] if detail else "couldn't match the sample"
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
