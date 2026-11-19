"""Sample roots Hoarder is allowed to read.

Projects are not a root. Hoarder indexes these folders in place and never
moves, renames, or deletes the audio files. The list lives in roots.json,
which is not in the repo; roots.example.json shows the format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOTS_FILE = Path(__file__).resolve().parent.parent / "roots.json"


@dataclass(frozen=True)
class Root:
    id: str
    label: str
    path: Path


def load_roots(path: Path = ROOTS_FILE) -> tuple[Root, ...]:
    """Library folders from roots.json. No file means no folders."""
    if not path.is_file():
        return ()
    entries = json.loads(path.read_text(encoding="utf-8"))
    return tuple(Root(str(entry["id"]), str(entry["label"]), Path(entry["path"])) for entry in entries)

AUDIO_EXT = frozenset({
    ".wav",
    ".aif",
    ".aiff",
    ".flac",
    ".mp3",
    ".ogg",
    ".opus",
    ".caf",
    ".rex",
    ".rx2",
})


def is_under_root(path: Path, roots: tuple[Root, ...] | list[Root]) -> bool:
    """True when path resolves to a file inside one of the library roots."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            base = root.path.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False
