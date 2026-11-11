"""Sound tags from the folder path and the file name.

A file can carry more than one tag. Stored form is " kick loop " so a filter
can require every selected tag with LIKE '% kick %'.
"""

from __future__ import annotations

import re

ROLE_ORDER: tuple[str, ...] = (
    "kick",
    "hat",
    "snare",
    "shaker",
    "bass",
    "lead",
    "arp",
    "synth",
    "vocals",
    "chops",
    "fx",
    "riser",
    "build",
    "noise",
    "loop",
)

ROLE_LABELS: dict[str, str] = {
    "kick": "Kick",
    "hat": "Hat",
    "snare": "Snare",
    "shaker": "Shaker",
    "bass": "Bass",
    "lead": "Lead",
    "arp": "Arp",
    "synth": "Synth",
    "vocals": "Vocals",
    "chops": "Chops",
    "fx": "FX",
    "riser": "Riser",
    "build": "Build",
    "noise": "White noise",
    "loop": "Loop",
}

ROLE_SET = frozenset(ROLE_ORDER)

# "sub kick" and "bass drum" are kicks. "Drum & Bass" is a pack genre, not a bass sound.
# Bare "effects" is a pack title, not an fx tag.
_BASS = re.compile(r"\bbass\b(?!\s+drums?\b)|\bsub(?!\s+kicks?\b)s?\b|\breese\b")
_DRUM_AND_BASS = re.compile(r"\bdrum\s+and\s+bass\b|\bdrum\s+bass\b")
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("kick", re.compile(r"\bkicks?\b|\bbd\b|\bbass ?drums?\b")),
    ("hat", re.compile(r"\bhi ?hats?\b|\bhihats?\b|\bhats?\b|\bclosed hats?\b|\bopen hats?\b")),
    ("snare", re.compile(r"\bsnares?\b|\bsnr\b")),
    ("shaker", re.compile(r"\bshakers?\b")),
    ("bass", _BASS),
    ("lead", re.compile(r"\bleads?\b")),
    ("arp", re.compile(r"\barps?\b|\barpeggios?\b|\barpeggiators?\b")),
    ("synth", re.compile(r"\bsynths?\b")),
    ("vocals", re.compile(r"\bvocals?\b|\bvox\b|\bvoices?\b|\bchoirs?\b")),
    ("chops", re.compile(r"\bchops?\b")),
    ("fx", re.compile(r"\bfx\b|\bsfx\b|\bimpacts?\b|\bwhooshes?\b")),
    ("riser", re.compile(r"\brisers?\b|\buplifters?\b|\bdownlifters?\b|\bsweeps?\b")),
    ("build", re.compile(r"\bbuild ?ups?\b|\bbuilds?\b")),
    ("noise", re.compile(r"\bwhite ?noise\b")),
    ("loop", re.compile(r"\bloops?\b")),
)

_CAMEL = re.compile(r"([a-z])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_GLUED = (
    ("whitenoises", "white noises"),
    ("whitenoise", "white noise"),
    ("synthloops", "synth loops"),
    ("synthloop", "synth loop"),
    ("bassloops", "bass loops"),
    ("bassloop", "bass loop"),
    ("drumloops", "drum loops"),
    ("drumloop", "drum loop"),
    ("vocalchops", "vocal chops"),
    ("vocalchop", "vocal chop"),
)


def haystack(name: str, folder: str) -> str:
    text = f"{folder} {name}"
    text = _CAMEL.sub(r"\1 \2", text).lower()
    text = _NON_ALNUM.sub(" ", text)
    for src, dst in _GLUED:
        text = text.replace(src, dst)
    return f" {text.strip()} "


def classify(name: str, folder: str) -> str:
    text = haystack(name, folder)
    found: list[str] = []
    for role, pattern in _RULES:
        hay = _DRUM_AND_BASS.sub(" ", text) if role == "bass" else text
        if pattern.search(hay):
            found.append(role)
    return encode(found)


def encode(roles: list[str] | tuple[str, ...]) -> str:
    chosen = set(roles)
    ordered = [role for role in ROLE_ORDER if role in chosen]
    if not ordered:
        return ""
    return " " + " ".join(ordered) + " "


def decode(stored: str | None) -> list[str]:
    found = set((stored or "").split())
    return [role for role in ROLE_ORDER if role in found]


def known_roles(raw: str) -> list[str]:
    chosen: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        role = part.strip().lower()
        if role in ROLE_SET and role not in seen:
            seen.add(role)
            chosen.append(role)
    return chosen
