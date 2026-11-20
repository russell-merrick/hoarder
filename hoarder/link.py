"""Localhost link between Hoarder and one plugin instance.

The socket is 127.0.0.1 only. A new connection replaces the previous one.
The page can disarm the link without unloading the plugin: previews then
stay in the browser, and the plugin is sent ``clear``.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from websockets.asyncio.server import serve

from hoarder.livebpm import tempo_ratio
from hoarder.stretch import browser_wav, match_sample

HOST = "127.0.0.1"
FORMATS = frozenset({"vst3", "au", "stand-in"})
STRETCHES = frozenset({0.5, 1.0, 2.0})
REPEATS = frozenset({"1/16", "1/8", "1/4", "1/2", "1bar", "2bar", "4bar"})


def repeat_seconds(rate: str | None, bpm: float | None, meter: int) -> float | None:
    """Seconds of audio one repeat cycle lasts at this tempo. None means the whole file."""
    if rate == "1/16":
        beats = 0.25
    elif rate == "1/8":
        beats = 0.5
    elif rate == "1/4":
        beats = 1.0
    elif rate == "1/2":
        beats = 2.0
    elif rate == "1bar":
        beats = float(max(1, meter))
    elif rate == "2bar":
        beats = 2.0 * max(1, meter)
    elif rate == "4bar":
        beats = 4.0 * max(1, meter)
    else:
        return None
    if not bpm or bpm <= 0:
        return None
    return beats * 60.0 / float(bpm)
_KEEP_RENDERS = 8
_RENDER_NAME = re.compile(r"^[0-9a-f]{32}\.wav$", re.IGNORECASE)


class LinkDown(RuntimeError):
    """The plugin socket is absent, or the page has disarmed it."""


def empty_snapshot() -> dict:
    return {
        "connected": False,
        "armed": False,
        "format": "",
        "host": "",
        "session": "",
        "bpm": None,
        "ppq": 0.0,
        "playing": False,
        "recording": False,
        "meter": [4, 4],
        "sample_rate": None,
    }


class LinkServer:
    def __init__(self, port: int = 8771) -> None:
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._conn = None
        self._lock = threading.Lock()
        self._snap = empty_snapshot()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._server = await serve(
            self._handler,
            HOST,
            self.port,
            process_request=self._local_only,
            max_size=8192,
            compression=None,
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        connection = self._take_socket()
        if connection is not None:
            await connection.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._snap)
            snap["meter"] = list(self._snap["meter"])
            updated = snap.pop("updated", None)
        if updated is None:
            snap["age_ms"] = 0.0
        else:
            snap["age_ms"] = max(0.0, (time.monotonic() - updated) * 1000.0)
        return snap

    async def arm(self) -> None:
        with self._lock:
            if not self._snap["connected"] or self._conn is None:
                raise LinkDown("plugin is not connected")
            self._snap["armed"] = True

    async def disarm(self) -> None:
        connection = None
        with self._lock:
            if not self._snap["connected"] or self._conn is None:
                raise LinkDown("plugin is not connected")
            self._snap["armed"] = False
            connection = self._conn
        await connection.send(json.dumps({"type": "clear"}))

    async def stop_preview(self) -> None:
        await self._send({"type": "stop"}, armed=False)

    async def publish_preview(self, message: dict) -> None:
        await self._send(message, armed=True)

    async def _send(self, message: dict, *, armed: bool) -> None:
        with self._lock:
            if not self._snap["connected"] or self._conn is None:
                raise LinkDown("plugin is not connected")
            if armed and not self._snap["armed"]:
                raise LinkDown("link is off")
            connection = self._conn
        await connection.send(json.dumps(message))

    def _take_socket(self):
        with self._lock:
            connection = self._conn
            self._conn = None
            self._snap = empty_snapshot()
            return connection

    async def _handler(self, connection) -> None:
        previous = None
        with self._lock:
            previous = self._conn
            self._conn = connection
            self._snap = empty_snapshot()
        if previous is not None and previous is not connection:
            await previous.close(1000, "replaced")
        try:
            hello = False
            async for raw in connection:
                if not isinstance(raw, str):
                    await connection.close(1003, "text only")
                    return
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await connection.close(1008, "bad json")
                    return
                if not isinstance(message, dict):
                    await connection.close(1008, "bad message")
                    return
                kind = message.get("type")
                if not hello:
                    if not self._accept_hello(message):
                        await connection.close(1008, "hello required")
                        return
                    hello = True
                    continue
                if kind == "hello":
                    if not self._accept_hello(message):
                        await connection.close(1008, "bad hello")
                        return
                elif kind == "host":
                    if not self._accept_host(message):
                        await connection.close(1008, "bad host")
                        return
                elif kind == "bye":
                    await connection.close(1000, "bye")
                    return
                else:
                    await connection.close(1008, "unknown message")
                    return
        finally:
            with self._lock:
                if self._conn is connection:
                    self._conn = None
                    self._snap = empty_snapshot()

    def _accept_hello(self, message: dict) -> bool:
        if message.get("type") != "hello":
            return False
        fmt = message.get("format")
        host = message.get("host")
        session = message.get("session")
        if not isinstance(fmt, str) or fmt.lower() not in FORMATS:
            return False
        if not isinstance(host, str) or not host.strip() or len(host) > 64:
            return False
        if not isinstance(session, str) or not session.strip() or len(session) > 64:
            return False
        with self._lock:
            if self._conn is None:
                return False
            self._snap["connected"] = True
            self._snap["armed"] = True
            self._snap["format"] = fmt.lower()
            self._snap["host"] = host.strip()
            self._snap["session"] = session.strip()
        return True

    def _accept_host(self, message: dict) -> bool:
        bpm = _number(message.get("bpm"), 1, 999)
        ppq = _number(message.get("ppq"), -1_000_000, 1_000_000)
        rate = _number(message.get("sample_rate"), 8000, 384000)
        meter = message.get("meter")
        playing = message.get("playing")
        recording = message.get("recording")
        if bpm is None or ppq is None or rate is None:
            return False
        if not isinstance(playing, bool) or not isinstance(recording, bool):
            return False
        if (
            not isinstance(meter, list)
            or len(meter) != 2
            or not all(isinstance(part, int) and not isinstance(part, bool) for part in meter)
            or not 1 <= meter[0] <= 32
            or meter[1] not in {1, 2, 4, 8, 16, 32}
        ):
            return False
        with self._lock:
            if not self._snap["connected"]:
                return False
            self._snap["bpm"] = bpm
            self._snap["ppq"] = ppq
            self._snap["playing"] = playing
            self._snap["recording"] = recording
            self._snap["meter"] = [meter[0], meter[1]]
            self._snap["sample_rate"] = rate
            self._snap["updated"] = time.monotonic()
        return True

    @staticmethod
    def _local_only(connection, _request):
        peer = connection.remote_address[0] if connection.remote_address else ""
        if peer not in {"127.0.0.1", "::1"}:
            return connection.respond(403, "local only")
        return None


def compose_preview(
    source: Path,
    *,
    semitones: int,
    project_bpm: float | None,
    sample_bpm: float | None,
    duration: float | None,
    loop: bool,
    stretch: float,
    repeat: str | None,
) -> dict:
    """WAV path plus the facts the plugin needs to place it on the host clock."""
    if stretch not in STRETCHES:
        raise ValueError("stretch must be 0.5, 1, or 2")
    if repeat is not None and repeat not in REPEATS:
        raise ValueError("unknown repeat rate")
    path = _materialize(source, semitones, project_bpm, sample_bpm, stretch)
    return {
        "type": "preview",
        "path": str(path),
        "loop": bool(loop),
        "beats": _beats(duration, sample_bpm, project_bpm, stretch),
        "bpm": project_bpm or sample_bpm,
        "repeat": repeat,
    }


def _materialize(
    source: Path,
    semitones: int,
    project_bpm: float | None,
    sample_bpm: float | None,
    stretch: float,
) -> Path:
    rendered = match_sample(source, semitones, project_bpm, sample_bpm, stretch)
    if rendered is None and source.suffix.lower() == ".wav":
        return source
    if rendered is None:
        rendered = browser_wav(source)
    directory = Path(tempfile.gettempdir()) / "hoarder-link"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{uuid.uuid4().hex}.wav"
    dest.write_bytes(rendered)
    leftovers = sorted(
        (item for item in directory.glob("*.wav") if _RENDER_NAME.match(item.name)),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in leftovers[_KEEP_RENDERS:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _beats(
    duration: float | None,
    sample_bpm: float | None,
    project_bpm: float | None,
    stretch: float,
) -> float | None:
    if not duration or duration <= 0:
        return None
    tempo = float(stretch)
    if project_bpm and sample_bpm:
        tempo = tempo_ratio(float(sample_bpm), float(project_bpm)) * float(stretch)
    if tempo <= 0:
        return None
    bpm = project_bpm or sample_bpm
    if not bpm:
        return None
    return round(float(duration) / tempo * float(bpm) / 60.0, 6)


def _number(value, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number < low or number > high:
        return None
    return number
