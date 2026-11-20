"""Local sample browser. Serves the catalog and the original files."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hoarder.clipboard import copy_file
from hoarder.dragdrop import drag_file
from hoarder.db import (
    backfill_roles,
    connect,
    counts_by_root,
    get_sample,
    init_db,
    query_samples,
    row_dict,
    set_sample_tags,
    visible_ids,
)
from hoarder.genres import GENRE_ORDER
from hoarder.genres import GENRE_SET
from hoarder.genres import decode as decode_genres
from hoarder.link import REPEATS, STRETCHES, LinkDown, LinkServer, compose_preview, empty_snapshot, repeat_seconds
from hoarder.livebpm import project_status
from hoarder.roles import ROLE_ORDER
from hoarder.roles import ROLE_SET
from hoarder.roles import decode as decode_roles
from hoarder.stretch import browser_wav, match_sample
from hoarder.roots import Root, is_under_root, load_roots
from hoarder.scan import start_scan, state

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "library.db"

def reveal_command(path: Path) -> str | list[str]:
    """Command that shows this file in the system file manager."""
    if sys.platform == "darwin":
        return ["open", "-R", str(path)]
    # Quote only the path. Quoting "/select,path" as one token makes Explorer
    # ignore the switch and open Documents whenever the path has a space.
    return f'explorer.exe /select,"{path}"'


def reveal_file(path: Path) -> None:
    subprocess.Popen(reveal_command(path))


MIME = {
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".caf": "audio/x-caf",
    ".rex": "application/octet-stream",
    ".rx2": "application/octet-stream",
}


def create_app(
    db_path: Path | None = None,
    roots: tuple[Root, ...] | list[Root] | None = None,
    *,
    scan_on_start: bool = True,
    link_port: int | None = None,
) -> FastAPI:
    database = Path(db_path) if db_path is not None else DEFAULT_DB
    library_roots = tuple(roots) if roots is not None else load_roots()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(database)
        backfill_roles(database)
        link = None
        if link_port is not None:
            link = LinkServer(port=link_port)
            await link.start()
            _app.state.link = link
        if scan_on_start:
            start_scan(database, library_roots)
        try:
            yield
        finally:
            if link is not None:
                await link.stop()

    app = FastAPI(title="hoarder", lifespan=lifespan)
    app.state.db_path = database
    app.state.roots = library_roots

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status() -> dict:
        with state.lock:
            scanning = state.scanning
            seen = state.seen
            updated = state.updated
            errors = state.errors
            error = state.error
            offline = list(state.offline)
        conn = connect(database)
        try:
            by_root = counts_by_root(conn)
            active = conn.execute("SELECT COUNT(*) AS n FROM samples WHERE dupe = 0").fetchone()["n"]
        finally:
            conn.close()
        return {
            "scanning": scanning,
            "seen": seen,
            "updated": updated,
            "errors": errors,
            "error": error,
            "offline": offline,
            "samples": sum(by_root.values()),
            "active": active,
            "by_root": by_root,
            "roots": [
                {
                    "id": root.id,
                    "label": root.label,
                    "path": str(root.path),
                    "online": root.id not in offline and root.path.is_dir(),
                    "count": by_root.get(root.id, 0),
                }
                for root in library_roots
            ],
        }

    @app.post("/api/rescan")
    def rescan() -> dict:
        if not start_scan(database, library_roots):
            raise HTTPException(409, "scan already running")
        return {"scanning": True}

    @app.get("/api/samples")
    def samples(
        q: str = "",
        root: str = "",
        pack: str = "",
        kind: str = "",
        roles: str = "",
        genres: str = "",
        exclude_genres: str = "",
        unique: bool = False,
        min_sec: float | None = None,
        max_sec: float | None = None,
        min_bpm: float | None = None,
        max_bpm: float | None = None,
        key: str = "",
        sort: str = "name",
        direction: str = "asc",
        limit: int = Query(80, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        if min_sec is not None and min_sec < 0:
            raise HTTPException(400, "min_sec must be >= 0")
        if max_sec is not None and max_sec < 0:
            raise HTTPException(400, "max_sec must be >= 0")
        if min_bpm is not None and min_bpm < 0:
            raise HTTPException(400, "min_bpm must be >= 0")
        if max_bpm is not None and max_bpm < 0:
            raise HTTPException(400, "max_bpm must be >= 0")
        if direction not in {"asc", "desc"}:
            direction = "asc"
        if min_sec is not None and max_sec is not None and min_sec > max_sec:
            return {"total": 0, "offset": offset, "limit": limit, "items": []}
        conn = connect(database)
        try:
            total, rows = query_samples(
                conn,
                q=q,
                root=root,
                pack=pack,
                kind=kind,
                min_sec=min_sec,
                max_sec=max_sec,
                min_bpm=min_bpm,
                max_bpm=max_bpm,
                key=key,
                roles=roles,
                genres=genres,
                exclude_genres=exclude_genres,
                unique_names=unique,
                sort=sort,
                direction=direction,
                limit=limit,
                offset=offset,
            )
        finally:
            conn.close()
        items = []
        for row in rows:
            items.append(_public_sample(row_dict(row)))
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    @app.get("/api/project")
    def project(request: Request) -> dict:
        status = dict(project_status())
        link = getattr(request.app.state, "link", None)
        snap = link.snapshot() if link is not None else empty_snapshot()
        status["link"] = snap
        if snap["connected"] and snap["bpm"]:
            status["running"] = True
            status["bpm"] = snap["bpm"]
            status["name"] = snap["host"] or status["name"]
        return status

    @app.get("/api/link")
    def link_status(request: Request) -> dict:
        link = getattr(request.app.state, "link", None)
        if link is None:
            return empty_snapshot()
        return link.snapshot()

    @app.post("/api/link/arm")
    async def link_arm(request: Request, body: LinkArm) -> dict:
        link = _link(request)
        try:
            if body.on:
                await link.arm()
            else:
                await link.disarm()
        except LinkDown as exc:
            raise HTTPException(409, str(exc)) from exc
        return link.snapshot()

    @app.post("/api/link/stop")
    async def link_stop(request: Request) -> dict:
        link = _link(request)
        try:
            await link.stop_preview()
        except LinkDown as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/link/preview")
    async def link_preview(request: Request, body: LinkPreview) -> dict:
        link = _link(request)
        if body.stretch not in {0.5, 1, 2}:
            raise HTTPException(400, "stretch must be 0.5, 1, or 2")
        snap = link.snapshot()
        if not snap["connected"] or not snap["armed"]:
            raise HTTPException(409, "link is off")
        row, path = _load(request, body.id)
        try:
            message = await asyncio.to_thread(
                compose_preview,
                path,
                semitones=body.semitones,
                project_bpm=body.bpm,
                sample_bpm=row["bpm"],
                duration=row["duration"],
                loop=_loops(row),
                stretch=float(body.stretch),
                repeat=body.repeat,
            )
            await link.publish_preview(message)
        except LinkDown as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "path": message["path"],
            "loop": message["loop"],
            "beats": message["beats"],
            "bpm": message.get("bpm"),
            "repeat": message.get("repeat"),
        }

    @app.get("/api/samples/{sample_id}/file")
    def sample_file(
        sample_id: int,
        request: Request,
        semitones: int = Query(0, ge=-12, le=12),
        bpm: float | None = Query(None, ge=40, le=250),
    ):
        row, path = _load(request, sample_id)
        matched = _matched(path, semitones, bpm, row["bpm"])
        if matched is None:
            return FileResponse(
                path,
                media_type=MIME.get(path.suffix.lower(), "application/octet-stream"),
                filename=path.name,
                content_disposition_type="inline",
            )
        filename = _matched_name(path, semitones, bpm, row["bpm"])
        return Response(
            content=matched,
            media_type="audio/wav",
            headers={"Content-Disposition": _disposition(filename)},
        )

    @app.post("/api/samples/{sample_id}/reveal")
    def reveal_sample(sample_id: int, request: Request) -> dict:
        _row, path = _load(request, sample_id)
        reveal_file(path)
        return {"ok": True}

    @app.post("/api/samples/{sample_id}/tags")
    def sample_tags(sample_id: int, request: Request, body: SampleTags) -> dict:
        _known_tags(body.roles, ROLE_SET)
        _known_tags(body.genres, GENRE_SET)
        conn = connect(request.app.state.db_path)
        try:
            saved = set_sample_tags(conn, sample_id, body.roles, body.genres)
        finally:
            conn.close()
        if saved is None:
            raise HTTPException(404, "sample not in the catalog")
        return saved

    @app.post("/api/samples/{sample_id}/copy")
    async def copy_sample(sample_id: int, request: Request, body: SampleCopy) -> dict:
        try:
            dest = await asyncio.to_thread(_export_sample, request, sample_id, body)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        try:
            await asyncio.to_thread(copy_file, dest)
        except OSError as exc:
            code = 501 if sys.platform != "win32" else 500
            raise HTTPException(code, "couldn't put the file on the clipboard") from exc
        return {"ok": True, "path": str(dest), "name": dest.name}

    @app.post("/api/samples/{sample_id}/drag")
    async def drag_sample(sample_id: int, request: Request, body: SampleCopy) -> dict:
        if sys.platform != "win32":
            raise HTTPException(501, "file drag is only available on Windows")
        try:
            dest, effect = await asyncio.to_thread(_drag_sample, request, sample_id, body)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "couldn't drag the file") from exc
        return {"ok": True, "path": str(dest), "name": dest.name, "effect": effect}

    @app.get("/api/samples/{sample_id}/preview")
    def sample_preview(
        sample_id: int,
        request: Request,
        semitones: int = Query(0, ge=-12, le=12),
        bpm: float | None = Query(None, ge=40, le=250),
    ):
        row, path = _load(request, sample_id)
        matched = _matched(path, semitones, bpm, row["bpm"])
        if matched is not None:
            return Response(
                content=matched,
                media_type="audio/wav",
                headers={"Content-Disposition": 'inline; filename="preview.wav"'},
            )
        ext = path.suffix.lower()
        if ext in {".aif", ".aiff"}:
            try:
                wav = browser_wav(path)
            except ValueError as exc:
                raise HTTPException(415, str(exc)) from exc
            return Response(
                content=wav,
                media_type="audio/wav",
                headers={"Content-Disposition": 'inline; filename="preview.wav"'},
            )
        return FileResponse(
            path,
            media_type=MIME.get(ext, "application/octet-stream"),
            filename=path.name,
            content_disposition_type="inline",
        )

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _matched(path: Path, semitones: int, bpm: float | None, sample_bpm: float | None) -> bytes | None:
    try:
        return match_sample(path, semitones, bpm, sample_bpm)
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc


def _matched_name(path: Path, semitones: int, bpm: float | None, sample_bpm: float | None) -> str:
    bits = [path.stem]
    if semitones:
        bits.append(f"{'+' if semitones > 0 else ''}{semitones}")
    if bpm and sample_bpm:
        bits.append(f"{bpm:g}bpm")
    return f"{' '.join(bits)}.wav"


_REPEAT_NAME = {
    "1/16": "16th",
    "1/8": "8th",
    "1/4": "quarter",
    "1/2": "half-note",
    "1bar": "1bar",
    "2bar": "2bar",
    "4bar": "4bar",
}


def _copy_name(
    path: Path,
    semitones: int,
    bpm: float | None,
    sample_bpm: float | None,
    stretch: float,
    repeat: str | None = None,
) -> str:
    stem = _matched_name(path, semitones, bpm, sample_bpm)[:-4]
    label = _REPEAT_NAME.get(repeat or "")
    if label:
        stem = f"{stem} {label}"
    if stretch == 0.5:
        stem = f"{stem} half"
    elif stretch == 2:
        stem = f"{stem} 2x"
    cleaned = "".join(ch if ch not in '<>:"/\\|?*' else " " for ch in stem)
    cleaned = " ".join(cleaned.split())[:80] or "sample"
    return f"{cleaned}.wav"


def _trim_wav(path: Path, seconds: float) -> Path:
    """A new wav of the first `seconds`, or the same path when it is already shorter."""
    with wave.open(str(path), "rb") as src:
        rate = src.getframerate() or 1
        frames = src.getnframes()
        keep = max(1, int(round(seconds * rate)))
        if keep >= frames:
            return path
        channels = src.getnchannels()
        width = src.getsampwidth()
        rate = src.getframerate() or 1
        data = src.readframes(keep)
    directory = Path(tempfile.gettempdir()) / "hoarder-link"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{uuid.uuid4().hex}.wav"
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(data)
    return dest


def _export_sample(request: Request, sample_id: int, body: SampleCopy) -> Path:
    if body.stretch not in STRETCHES:
        raise HTTPException(400, "stretch must be 0.5, 1, or 2")
    repeat = body.repeat or None
    if repeat is not None and repeat not in REPEATS:
        raise HTTPException(400, "unknown repeat rate")
    row, source = _load(request, sample_id)
    message = compose_preview(
        source,
        semitones=body.semitones,
        project_bpm=body.bpm,
        sample_bpm=row["bpm"],
        duration=row["duration"],
        loop=False,
        stretch=float(body.stretch),
        repeat=repeat,
    )
    rendered = Path(message["path"])
    seconds = repeat_seconds(repeat, message.get("bpm"), body.meter)
    if seconds:
        rendered = _trim_wav(rendered, seconds)
    name = _copy_name(source, body.semitones, body.bpm, row["bpm"], float(body.stretch), repeat)
    return _place_copy(rendered, source, name)


def _drag_sample(request: Request, sample_id: int, body: SampleCopy) -> tuple[Path, int]:
    dest = _export_sample(request, sample_id, body)
    return dest, drag_file(dest)


def _place_copy(rendered: Path, source: Path, name: str) -> Path:
    """Keep an unmodified sample where it is. A render gets a readable temp name."""
    if rendered.resolve() == source.resolve():
        return source
    directory = Path(tempfile.gettempdir()) / "hoarder-link"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / name
    if dest.resolve() != rendered.resolve():
        dest.write_bytes(rendered.read_bytes())
    return dest


def _disposition(filename: str) -> str:
    """Content-Disposition for any filename. A plain header can't carry "–" or
    non-Latin text, so those go in the UTF-8 form, as FileResponse does."""
    encoded = quote(filename)
    if encoded == filename:
        return f'inline; filename="{filename}"'
    return f"inline; filename*=utf-8''{encoded}"


class LinkArm(BaseModel):
    on: bool


class LinkPreview(BaseModel):
    id: int
    semitones: int = Field(0, ge=-12, le=12)
    bpm: float | None = Field(None, ge=1, le=999)
    stretch: float = 1
    repeat: str | None = None


class SampleTags(BaseModel):
    roles: list[str] = []
    genres: list[str] = []


class SampleCopy(BaseModel):
    semitones: int = Field(0, ge=-12, le=12)
    bpm: float | None = Field(None, ge=1, le=999)
    stretch: float = 1
    repeat: str | None = None
    meter: int = Field(4, ge=1, le=32)


def _link(request: Request) -> LinkServer:
    link = getattr(request.app.state, "link", None)
    if link is None:
        raise HTTPException(503, "link is not running")
    return link


def _public_sample(item: dict) -> dict:
    """Tags the row shows: the guess, plus additions, minus removals."""
    item["roles"] = visible_ids(
        item.get("roles") or "",
        item.get("role_add") or "",
        item.get("role_remove") or "",
        decode_roles,
        ROLE_ORDER,
    )
    item["genres"] = visible_ids(
        item.get("genres") or "",
        item.get("genre_add") or "",
        item.get("genre_remove") or "",
        decode_genres,
        GENRE_ORDER,
    )
    for column in ("role_add", "role_remove", "genre_add", "genre_remove"):
        item.pop(column, None)
    item["key"] = item.pop("musical_key", None) or ""
    return item


def _loops(row: dict) -> bool:
    return "loop" in visible_ids(
        row.get("roles") or "",
        row.get("role_add") or "",
        row.get("role_remove") or "",
        decode_roles,
        ROLE_ORDER,
    )


def _known_tags(raw: list[str], allowed: frozenset[str]) -> list[str]:
    chosen: list[str] = []
    for item in raw:
        tag = str(item).strip().lower()
        if tag not in allowed:
            raise HTTPException(400, "unknown tag")
        if tag not in chosen:
            chosen.append(tag)
    return chosen


def _load(request: Request, sample_id: int) -> tuple[dict, Path]:
    app = request.app
    conn = connect(app.state.db_path)
    try:
        row = get_sample(conn, sample_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "sample not in the catalog")
    path = Path(row["path"])
    if not is_under_root(path, app.state.roots):
        raise HTTPException(403, "path is outside the library")
    if not path.is_file():
        raise HTTPException(404, "sample file is missing")
    return row_dict(row), path


app = create_app(link_port=8771)
