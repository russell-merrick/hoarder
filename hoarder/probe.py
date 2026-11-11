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


# Preview transcode cap. The original file is not copied; this only limits
# the in-memory AIFF → WAV conversion used for browser playback.
MAX_PREVIEW_BYTES = 80 * 1024 * 1024


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


def aiff_to_wav_bytes(path: Path) -> bytes:
    """Uncompressed AIFF/AIFF-C PCM as a 16-bit WAV byte string.

    Used so the browser can preview Ableton's .aif samples. The source file
    is only read.
    """
    size = path.stat().st_size
    if size > MAX_PREVIEW_BYTES:
        raise ValueError("file is too large to preview")
    data = path.read_bytes()
    if len(data) < 12 or data[0:4] != b"FORM" or data[8:12] not in {b"AIFF", b"AIFC"}:
        raise ValueError("not an AIFF file")
    is_aifc = data[8:12] == b"AIFC"
    comm = ssnd = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        chunk = int.from_bytes(data[pos + 4 : pos + 8], "big")
        start = pos + 8
        body = data[start : start + chunk]
        if cid == b"COMM":
            comm = body
        elif cid == b"SSND":
            ssnd = body
        pos = start + chunk + (chunk & 1)
    if not comm or ssnd is None or len(comm) < 18:
        raise ValueError("AIFF is missing COMM or SSND")
    channels = int.from_bytes(comm[0:2], "big")
    frames = int.from_bytes(comm[2:6], "big")
    bits = int.from_bytes(comm[6:8], "big")
    rate = int(round(_extended80(comm[8:18])))
    comp = b"NONE"
    if is_aifc and len(comm) >= 22:
        comp = comm[18:22]
    if comp not in {b"NONE", b"twos", b"raw ", b"sowt"}:
        raise ValueError("compressed AIFF can't be previewed")
    if channels < 1 or rate < 1 or bits not in {8, 16, 24, 32}:
        raise ValueError("unsupported AIFF PCM layout")
    offset = int.from_bytes(ssnd[0:4], "big") if len(ssnd) >= 8 else 0
    pcm = ssnd[8 + offset :]
    width = bits // 8
    need = frames * channels * width
    pcm = pcm[:need]
    little = comp == b"sowt"
    pcm16 = _to_s16le(pcm, width, little=little)
    return _wav_bytes(pcm16, channels, rate, 16)


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


def _to_s16le(pcm: bytes, width: int, *, little: bool) -> bytes:
    if width == 1:
        out = bytearray(len(pcm) * 2)
        for i, byte in enumerate(pcm):
            sample = byte - 128 if not little else (byte if byte < 128 else byte - 256)
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, sample << 8)))
        return bytes(out)
    count = len(pcm) // width
    out = bytearray(count * 2)
    for i in range(count):
        chunk = pcm[i * width : (i + 1) * width]
        if not little:
            chunk = chunk[::-1]
        if width >= 2:
            sample = int.from_bytes(chunk[-2:], "little", signed=True)
        else:
            sample = 0
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def _wav_bytes(pcm: bytes, channels: int, rate: int, bits: int) -> bytes:
    width = bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        rate * channels * width,
        channels * width,
        bits,
        b"data",
        len(pcm),
    )
    return header + pcm
