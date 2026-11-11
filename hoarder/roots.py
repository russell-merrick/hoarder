"""Sample roots Hoarder is allowed to read.

Projects are not a root. Hoarder indexes these folders in place and never
moves, renames, or deletes the audio files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Root:
    id: str
    label: str
    path: Path


DEFAULT_ROOTS: tuple[Root, ...] = (
    Root("curated", "Samples", Path(r"G:\Other computers\Snowy\music\samples")),
    Root("splice", "Splice", Path(r"C:\Users\russe\Documents\Splice\Samples")),
    Root(
        "user_library",
        "User Library",
        Path(r"G:\Other computers\Snowy\User Library\Samples"),
    ),
)

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
