"""Local sample browser. Serves the catalog and the original files."""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from hoarder.db import backfill_roles, connect, counts_by_root, get_sample, init_db, query_samples, row_dict
from hoarder.genres import decode as decode_genres
from hoarder.livebpm import project_status
from hoarder.roles import decode as decode_roles
from hoarder.stretch import browser_wav, match_sample
from hoarder.roots import Root, is_under_root, load_roots
from hoarder.scan import start_scan, state

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "library.db"

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
) -> FastAPI:
    database = Path(db_path) if db_path is not None else DEFAULT_DB
    library_roots = tuple(roots) if roots is not None else load_roots()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(database)
        backfill_roles(database)
        if scan_on_start:
            start_scan(database, library_roots)
        yield

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
            item = row_dict(row)
            item["roles"] = decode_roles(item.get("roles"))
            item["genres"] = decode_genres(item.get("genres"))
            item["key"] = item.pop("musical_key", None) or ""
            items.append(item)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    @app.get("/api/project")
    def project() -> dict:
        return project_status()

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
        # Quote only the path. Quoting "/select,path" as one token makes Explorer
        # ignore the switch and open Documents whenever the path has a space.
        subprocess.Popen(f'explorer.exe /select,"{path}"')
        return {"ok": True}

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


def _disposition(filename: str) -> str:
    """Content-Disposition for any filename. A plain header can't carry "–" or
    non-Latin text, so those go in the UTF-8 form, as FileResponse does."""
    encoded = quote(filename)
    if encoded == filename:
        return f'inline; filename="{filename}"'
    return f"inline; filename*=utf-8''{encoded}"


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


app = create_app()
