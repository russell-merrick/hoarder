"""Read duration and PCM facts from sample headers. Never writes the file."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AudioMeta:
    duration: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bit_depth: int | None = None


def probe(path: Path) -> AudioMeta:
    ext = path.suffix.lower()
    try:
        if ext == ".wav":
            return _probe_wav(path) or AudioMeta()
        if ext in {".aif", ".aiff"}:
            return _probe_aiff(path) or AudioMeta()
        return _probe_mutagen(path)
    except OSError:
        return AudioMeta()


def repitch_wav(data: bytes, semitones: int) -> bytes:
    """Varispeed a WAV by rewriting the sample-rate header. The PCM is unchanged.

    Players that honor the header, including Ableton on import, play the same
    samples faster or slower, so pitch and length move together.
    """
    if semitones == 0:
        return data
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    buf = bytearray(data)
    pos = 12
    while pos + 8 <= len(buf):
        cid = bytes(buf[pos : pos + 4])
        size = int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if cid == b"fmt " and size >= 16:
            channels = int.from_bytes(buf[pos + 10 : pos + 12], "little")
            bits = int.from_bytes(buf[pos + 22 : pos + 24], "little")
            rate = int.from_bytes(buf[pos + 12 : pos + 16], "little")
            if channels < 1 or rate < 1 or bits < 8:
                raise ValueError("unsupported WAV layout")
            shifted = int(round(rate * (2 ** (semitones / 12))))
            shifted = max(1000, min(384000, shifted))
            block = channels * (bits // 8)
            struct.pack_into("<I", buf, pos + 12, shifted)
            struct.pack_into("<I", buf, pos + 16, shifted * block)
            return bytes(buf)
        pos += 8 + size + (size & 1)
    raise ValueError("WAV is missing fmt")


def _probe_wav(path: Path) -> AudioMeta | None:
    with path.open("rb") as handle:
        head = handle.read(12)
        if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        rate = channels = bits = None
        byte_rate = None
        data_size = None
        while True:
            hdr = handle.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt " and size >= 16:
                fmt = handle.read(16)
                if len(fmt) < 16:
                    return None
                _fmt, channels, rate, byte_rate, _block, bits = struct.unpack("<HHIIHH", fmt)
                rest = size - 16
                if rest:
                    handle.seek(rest, 1)
                if size & 1:
                    handle.seek(1, 1)
            elif cid == b"data":
                data_size = None if size == 0xFFFFFFFF else size
                break
            else:
                handle.seek(size + (size & 1), 1)
        duration = None
        if data_size is not None and byte_rate:
            duration = data_size / byte_rate
        if rate is None:
            return None
        return AudioMeta(duration, int(rate), int(channels or 0) or None, int(bits or 0) or None)


def _probe_aiff(path: Path) -> AudioMeta | None:
    with path.open("rb") as handle:
        head = handle.read(12)
        if len(head) < 12 or head[0:4] != b"FORM" or head[8:12] not in {b"AIFF", b"AIFC"}:
            return None
        while True:
            hdr = handle.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[0:4]
            size = int.from_bytes(hdr[4:8], "big")
            if cid == b"COMM" and size >= 18:
                comm = handle.read(18)
                if len(comm) < 18:
                    return None
                channels = int.from_bytes(comm[0:2], "big")
                frames = int.from_bytes(comm[2:6], "big")
                bits = int.from_bytes(comm[6:8], "big")
                rate = int(round(_extended80(comm[8:18])))
                duration = (frames / rate) if rate else None
                return AudioMeta(duration, rate or None, channels or None, bits or None)
            handle.seek(size + (size & 1), 1)
    return None


def _probe_mutagen(path: Path) -> AudioMeta:
    try:
        from mutagen import File
    except ImportError:
        return AudioMeta()
    try:
        audio = File(path)
    except Exception:
        return AudioMeta()
    if audio is None or audio.info is None:
        return AudioMeta()
    info = audio.info
    duration = getattr(info, "length", None)
    rate = getattr(info, "sample_rate", None)
    channels = getattr(info, "channels", None)
    bits = getattr(info, "bits_per_sample", None)
    if duration is not None and duration < 0:
        duration = None
    return AudioMeta(
        float(duration) if duration is not None else None,
        int(rate) if rate else None,
        int(channels) if channels else None,
        int(bits) if bits else None,
    )


def _extended80(raw: bytes) -> float:
    """80-bit SANE float used by AIFF for the sample rate."""
    exp_field = int.from_bytes(raw[0:2], "big")
    sign = exp_field >> 15
    exp = exp_field & 0x7FFF
    frac = int.from_bytes(raw[2:10], "big")
    if exp == 0 and frac == 0:
        return 0.0
    if exp == 0x7FFF:
        return float("nan")
    # The integer bit is explicit in the top bit of the fraction.
    value = (frac / float(1 << 63)) * (2.0 ** (exp - 16383))
    return -value if sign else value
