"""Walk the library roots and refresh the catalog. Read-only on the audio."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from hoarder.db import (
    connect,
    delete_missing,
    existing_stamp,
    init_db,
    upsert_many,
)
from hoarder.probe import probe
from hoarder.roots import AUDIO_EXT, Root

BATCH = 200


@dataclass
class ScanState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    scanning: bool = False
    seen: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    error: str | None = None
    offline: list[str] = field(default_factory=list)


state = ScanState()


def start_scan(db_path: Path, roots: list[Root] | tuple[Root, ...]) -> bool:
    with state.lock:
        if state.scanning:
            return False
        state.scanning = True
        state.seen = 0
        state.updated = 0
        state.removed = 0
        state.errors = 0
        state.error = None
        state.offline = []
    thread = threading.Thread(
        target=_run,
        args=(db_path, tuple(roots)),
        name="hoarder-scan",
        daemon=True,
    )
    thread.start()
    return True


def pack_and_folder(root: Path, file: Path) -> tuple[str, str]:
    rel = file.parent.relative_to(root)
    parts = [part for part in rel.parts if part not in {".", ""}]
    if parts and parts[0].lower() == "packs":
        parts = parts[1:]
    if not parts:
        return "(loose)", ""
    folder = os.path.join(*parts)
    return parts[0], folder


def _run(db_path: Path, roots: tuple[Root, ...]) -> None:
    with state.lock:
        state.seen = 0
        state.updated = 0
        state.removed = 0
        state.errors = 0
        state.error = None
        state.offline = []
    try:
        init_db(db_path)
        conn = connect(db_path)
        try:
            for root in roots:
                if not root.path.is_dir():
                    with state.lock:
                        state.offline.append(root.id)
                    continue
                _scan_root(conn, root)
        finally:
            conn.close()
    except Exception as exc:
        with state.lock:
            state.error = str(exc)
    finally:
        with state.lock:
            state.scanning = False


def _scan_root(conn, root: Root) -> None:
    stamps = existing_stamp(conn, root.id)
    seen: set[str] = set()
    batch: list[dict] = []
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE root = ?",
        (root.id,),
    ).fetchone()["n"]

    for dirpath, dirnames, filenames in os.walk(root.path):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            if filename.startswith(".") or filename.startswith("._"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            full = os.path.join(dirpath, filename)
            path = Path(full)
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                with state.lock:
                    state.errors += 1
                continue
            seen.add(full)
            with state.lock:
                state.seen += 1
            previous = stamps.get(full)
            if previous is not None and previous[0] == size and abs(previous[1] - mtime) < 1e-3:
                continue
            meta = probe(path)
            try:
                pack, folder = pack_and_folder(root.path, path)
            except ValueError:
                pack, folder = "(loose)", ""
            batch.append(
                {
                    "path": full,
                    "name": filename,
                    "ext": ext,
                    "root": root.id,
                    "pack": pack,
                    "folder": folder,
                    "size": size,
                    "mtime": mtime,
                    "duration": meta.duration,
                    "sample_rate": meta.sample_rate,
                    "channels": meta.channels,
                    "bit_depth": meta.bit_depth,
                }
            )
            if len(batch) >= BATCH:
                upsert_many(conn, batch)
                with state.lock:
                    state.updated += len(batch)
                batch.clear()
    if batch:
        upsert_many(conn, batch)
        with state.lock:
            state.updated += len(batch)
    delete_missing(conn, root.id, seen)
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE root = ?",
        (root.id,),
    ).fetchone()["n"]
    with state.lock:
        state.removed += max(0, before - after)
