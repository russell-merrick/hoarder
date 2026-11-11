"""SQLite catalog. Paths point at the original files."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hoarder.genres import classify as classify_genres
from hoarder.genres import known_genres
from hoarder.keys import parse_key
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
  musical_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_samples_root ON samples(root);
CREATE INDEX IF NOT EXISTS idx_samples_pack ON samples(pack);
CREATE INDEX IF NOT EXISTS idx_samples_duration ON samples(duration);
CREATE INDEX IF NOT EXISTS idx_samples_name ON samples(name);
"""

SORTS = {
    ("name", "asc"): "name COLLATE NOCASE ASC",
    ("name", "desc"): "name COLLATE NOCASE DESC",
    ("duration", "asc"): "duration IS NULL, duration ASC",
    ("duration", "desc"): "duration IS NULL, duration DESC",
    ("pack", "asc"): "pack COLLATE NOCASE ASC, name COLLATE NOCASE ASC",
    ("pack", "desc"): "pack COLLATE NOCASE DESC, name COLLATE NOCASE ASC",
    ("size", "asc"): "size ASC",
    ("size", "desc"): "size DESC",
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
        conn.commit()
    finally:
        conn.close()


def backfill_roles(db_path: Path) -> None:
    """Recompute sample tags and genre tags. Scan skips unchanged files, so this
    is what gets a rule change onto rows that are already in the catalog."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id, name, pack, folder FROM samples").fetchall()
        conn.executemany(
            "UPDATE samples SET roles = ?, genres = ?, musical_key = ? WHERE id = ?",
            [
                (
                    classify_roles(row["name"], row["folder"]),
                    classify_genres(row["pack"], row["folder"]),
                    parse_key(row["name"], row["folder"]),
                    row["id"],
                )
                for row in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def upsert_many(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    prepared = []
    for row in rows:
        item = dict(row)
        if item.get("roles") is None:
            item["roles"] = classify_roles(str(item.get("name") or ""), str(item.get("folder") or ""))
        if item.get("genres") is None:
            item["genres"] = classify_genres(str(item.get("pack") or ""), str(item.get("folder") or ""))
        if item.get("musical_key") is None:
            item["musical_key"] = parse_key(str(item.get("name") or ""), str(item.get("folder") or ""))
        prepared.append(item)
    conn.executemany(
        """
        INSERT INTO samples (
          path, name, ext, root, pack, folder, size, mtime,
          duration, sample_rate, channels, bit_depth, roles, genres, musical_key
        ) VALUES (
          :path, :name, :ext, :root, :pack, :folder, :size, :mtime,
          :duration, :sample_rate, :channels, :bit_depth, :roles, :genres, :musical_key
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
          musical_key=excluded.musical_key
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
    roles: str = "",
    genres: str = "",
    exclude_genres: str = "",
    unique_names: bool = False,
    sort: str = "name",
    direction: str = "asc",
    limit: int = 80,
    offset: int = 0,
) -> tuple[int, list[sqlite3.Row]]:
    where, params = _where(q, root, pack, kind, min_sec, max_sec, roles, genres, exclude_genres)
    order = SORTS.get((sort, direction), SORTS[("name", "asc")])
    source = "samples"
    if unique_names:
        # Same name and size is the same file saved twice. Keep the organized copy.
        source = f"""(
          SELECT id, name, ext, root, pack, folder, path, size, duration,
                 sample_rate, channels, bit_depth, roles, genres, musical_key
          FROM (
            SELECT samples.*, ROW_NUMBER() OVER (
              PARTITION BY name COLLATE NOCASE, size
              ORDER BY
                CASE WHEN root = 'user_library' AND folder = 'Splice' THEN 1 ELSE 0 END,
                CASE root WHEN 'splice' THEN 0 WHEN 'curated' THEN 1 ELSE 2 END,
                length(folder) DESC,
                id
            ) AS __rn
            FROM samples
            WHERE {where}
          ) AS ranked
          WHERE __rn = 1
        ) AS chosen"""
    where_sql = "" if unique_names else f"WHERE {where}"
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM {source} {where_sql}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT id, name, ext, root, pack, folder, path, size, duration,
               sample_rate, channels, bit_depth, roles, genres, musical_key
        FROM {source}
        {where_sql}
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
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if q.strip():
        like = _like(q.strip())
        clauses.append(
            "(name LIKE ? ESCAPE '\\' OR folder LIKE ? ESCAPE '\\' "
            "OR pack LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like, like])
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
