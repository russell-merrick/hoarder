"""SQLite catalog. Paths point at the original files."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from hoarder import genres as genre_rules
from hoarder import keys as key_rules
from hoarder import livebpm as bpm_rules
from hoarder import roles as role_rules
from hoarder.genres import classify as classify_genres
from hoarder.genres import known_genres
from hoarder.keys import parse_key, spellings
from hoarder.livebpm import parse_bpm
from hoarder.roles import classify as classify_roles
from hoarder.roles import known_roles

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  ext TEXT NOT NULL,
  root TEXT NOT NULL,
  pack TEXT NOT NULL,
  folder TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  duration REAL,
  sample_rate INTEGER,
  channels INTEGER,
  bit_depth INTEGER,
  roles TEXT NOT NULL DEFAULT '',
  genres TEXT NOT NULL DEFAULT '',
  musical_key TEXT NOT NULL DEFAULT '',
  bpm REAL,
  dupe INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_root ON samples(root);
CREATE INDEX IF NOT EXISTS idx_samples_pack ON samples(pack);
CREATE INDEX IF NOT EXISTS idx_samples_duration ON samples(duration);
CREATE INDEX IF NOT EXISTS idx_samples_name ON samples(name);
"""

# Indexes on columns that older catalogs gain in init_db.
LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_samples_bpm ON samples(bpm);
CREATE INDEX IF NOT EXISTS idx_samples_key ON samples(musical_key);
"""

# Pitch, then major before minor. The leading musical_key = '' keeps blanks last.
_KEY_PITCH = """(
  CASE substr(musical_key, 1, 1)
    WHEN 'C' THEN 0 WHEN 'D' THEN 2 WHEN 'E' THEN 4 WHEN 'F' THEN 5
    WHEN 'G' THEN 7 WHEN 'A' THEN 9 WHEN 'B' THEN 11 ELSE 0 END
  + CASE
      WHEN instr(musical_key, '#') > 0 THEN 1
      WHEN instr(musical_key, 'b') > 0 THEN -1
      ELSE 0 END
)"""
_KEY_MINOR = "CASE WHEN musical_key LIKE '%m' THEN 1 ELSE 0 END"

SORTS = {
    ("name", "asc"): "name COLLATE NOCASE ASC",
    ("name", "desc"): "name COLLATE NOCASE DESC",
    ("duration", "asc"): "duration IS NULL, duration ASC",
    ("duration", "desc"): "duration IS NULL, duration DESC",
    ("pack", "asc"): "pack COLLATE NOCASE ASC, name COLLATE NOCASE ASC",
    ("pack", "desc"): "pack COLLATE NOCASE DESC, name COLLATE NOCASE ASC",
    ("size", "asc"): "size ASC",
    ("size", "desc"): "size DESC",
    ("bpm", "asc"): "bpm IS NULL, bpm ASC, name COLLATE NOCASE ASC",
    ("bpm", "desc"): "bpm IS NULL, bpm DESC, name COLLATE NOCASE ASC",
    ("key", "asc"): f"musical_key = '', {_KEY_PITCH} ASC, {_KEY_MINOR} ASC, name COLLATE NOCASE ASC",
    ("key", "desc"): f"musical_key = '', {_KEY_PITCH} DESC, {_KEY_MINOR} DESC, name COLLATE NOCASE ASC",
}

TYPE_EXTS = {
    "wav": (".wav",),
    "aif": (".aif", ".aiff"),
    "flac": (".flac",),
    "mp3": (".mp3",),
    "ogg": (".ogg", ".opus"),
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        if "roles" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN roles TEXT NOT NULL DEFAULT ''")
        if "genres" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN genres TEXT NOT NULL DEFAULT ''")
        if "musical_key" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN musical_key TEXT NOT NULL DEFAULT ''")
        if "bpm" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN bpm REAL")
        if "dupe" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN dupe INTEGER NOT NULL DEFAULT 0")
        conn.executescript(LATE_INDEXES)
        conn.commit()
    finally:
        conn.close()


def backfill_roles(db_path: Path) -> None:
    """Recompute tags, key, tempo and duplicates when the rules have changed.

    Scan skips unchanged files, so this is what gets a rule change onto rows
    that are already in the catalog. With no change it returns at once.
    """
    stamp = _rules_stamp()
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'rules'").fetchone()
        if row is not None and row["value"] == stamp:
            return
        rows = conn.execute("SELECT id, name, pack, folder FROM samples").fetchall()
        conn.executemany(
            "UPDATE samples SET roles = ?, genres = ?, musical_key = ?, bpm = ? WHERE id = ?",
            [
                (
                    *_derived(row["name"], row["pack"], row["folder"]).values(),
                    row["id"],
                )
                for row in rows
            ],
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('rules', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (stamp,),
        )
        conn.commit()
        mark_duplicates(conn)
    finally:
        conn.close()


def _rules_stamp() -> str:
    """Changes whenever a tag, key or tempo rule changes."""
    digest = hashlib.sha1()
    for module in (role_rules, genre_rules, key_rules, bpm_rules):
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


def _derived(name: str, pack: str, folder: str) -> dict[str, Any]:
    """Columns worked out from the name and folder. Order matches backfill's UPDATE."""
    return {
        "roles": classify_roles(name, folder),
        "genres": classify_genres(pack, folder),
        "musical_key": parse_key(name, folder),
        "bpm": parse_bpm(name, folder),
    }


def mark_duplicates(conn: sqlite3.Connection) -> None:
    """Flag every copy but one of a file saved twice: same name and size.

    The kept copy is the organized one. "Hide duplicates" shows only kept copies.
    """
    conn.execute("UPDATE samples SET dupe = 0 WHERE dupe != 0")
    conn.execute(
        """
        UPDATE samples SET dupe = 1 WHERE id IN (
          SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
              PARTITION BY name COLLATE NOCASE, size
              ORDER BY
                CASE root WHEN 'downloads' THEN 0 WHEN 'curated' THEN 1 ELSE 2 END,
                length(folder) DESC,
                id
            ) AS rn
            FROM samples
          )
          WHERE rn > 1
        )
        """
    )
    conn.commit()


def upsert_many(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    prepared = []
    for row in rows:
        item = dict(row)
        derived = _derived(str(item.get("name") or ""), str(item.get("pack") or ""), str(item.get("folder") or ""))
        for column, value in derived.items():
            if item.get(column) is None:
                item[column] = value
        prepared.append(item)
    conn.executemany(
        """
        INSERT INTO samples (
          path, name, ext, root, pack, folder, size, mtime,
          duration, sample_rate, channels, bit_depth, roles, genres, musical_key, bpm
        ) VALUES (
          :path, :name, :ext, :root, :pack, :folder, :size, :mtime,
          :duration, :sample_rate, :channels, :bit_depth, :roles, :genres, :musical_key, :bpm
        )
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name,
          ext=excluded.ext,
          root=excluded.root,
          pack=excluded.pack,
          folder=excluded.folder,
          size=excluded.size,
          mtime=excluded.mtime,
          duration=excluded.duration,
          sample_rate=excluded.sample_rate,
          channels=excluded.channels,
          bit_depth=excluded.bit_depth,
          roles=excluded.roles,
          genres=excluded.genres,
          musical_key=excluded.musical_key,
          bpm=excluded.bpm
        """,
        prepared,
    )
    conn.commit()


def existing_stamp(conn: sqlite3.Connection, root_id: str) -> dict[str, tuple[int, float]]:
    rows = conn.execute(
        "SELECT path, size, mtime FROM samples WHERE root = ?",
        (root_id,),
    ).fetchall()
    return {row["path"]: (row["size"], row["mtime"]) for row in rows}


def delete_missing(conn: sqlite3.Connection, root_id: str, seen: set[str]) -> None:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_paths (path TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM seen_paths")
    conn.executemany("INSERT INTO seen_paths (path) VALUES (?)", ((p,) for p in seen))
    conn.execute(
        """
        DELETE FROM samples
        WHERE root = ?
          AND path NOT IN (SELECT path FROM seen_paths)
        """,
        (root_id,),
    )
    conn.commit()


def get_sample(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()


def counts_by_root(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT root, COUNT(*) AS n FROM samples GROUP BY root").fetchall()
    return {row["root"]: row["n"] for row in rows}


def query_samples(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    root: str = "",
    pack: str = "",
    kind: str = "",
    min_sec: float | None = None,
    max_sec: float | None = None,
    min_bpm: float | None = None,
    max_bpm: float | None = None,
    key: str = "",
    roles: str = "",
    genres: str = "",
    exclude_genres: str = "",
    unique_names: bool = False,
    sort: str = "name",
    direction: str = "asc",
    limit: int = 80,
    offset: int = 0,
) -> tuple[int, list[sqlite3.Row]]:
    where, params = _where(
        q, root, pack, kind, min_sec, max_sec, roles, genres, exclude_genres, min_bpm, max_bpm, key
    )
    if unique_names:
        where += " AND dupe = 0"
    order = SORTS.get((sort, direction), SORTS[("name", "asc")])
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM samples WHERE {where}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT id, name, ext, root, pack, folder, path, size, duration,
               sample_rate, channels, bit_depth, roles, genres, musical_key, bpm
        FROM samples
        WHERE {where}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return int(total), rows


def _where(
    q: str,
    root: str,
    pack: str,
    kind: str,
    min_sec: float | None,
    max_sec: float | None,
    roles: str = "",
    genres: str = "",
    exclude_genres: str = "",
    min_bpm: float | None = None,
    max_bpm: float | None = None,
    key: str = "",
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    # Every word must appear somewhere in the name, folder or pack, in any order.
    # The full path is left out: its drive and root folders match every file.
    for word in q.split():
        like = _like(word)
        clauses.append("(name LIKE ? ESCAPE '\\' OR folder LIKE ? ESCAPE '\\' OR pack LIKE ? ESCAPE '\\')")
        params.extend([like, like, like])
    if root:
        clauses.append("root = ?")
        params.append(root)
    if pack:
        clauses.append("pack = ?")
        params.append(pack)
    if kind in TYPE_EXTS:
        marks = ",".join("?" for _ in TYPE_EXTS[kind])
        clauses.append(f"ext IN ({marks})")
        params.extend(TYPE_EXTS[kind])
    if min_sec is not None:
        clauses.append("duration >= ?")
        params.append(min_sec)
    if max_sec is not None:
        clauses.append("duration <= ?")
        params.append(max_sec)
    if min_bpm is not None:
        clauses.append("bpm >= ?")
        params.append(min_bpm)
    if max_bpm is not None:
        clauses.append("bpm <= ?")
        params.append(max_bpm)
    names = spellings(key)
    if names:
        clauses.append(f"musical_key IN ({','.join('?' for _ in names)})")
        params.extend(names)
    for role in known_roles(roles):
        clauses.append("roles LIKE ?")
        params.append(f"% {role} %")
    for genre in known_genres(genres):
        clauses.append("genres LIKE ?")
        params.append(f"% {genre} %")
    for genre in known_genres(exclude_genres):
        clauses.append("genres NOT LIKE ?")
        params.append(f"% {genre} %")
    return " AND ".join(clauses), params


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
