"""SQLite storage: one connection per thread, WAL mode."""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .decide import TRIED_REASONS
from .models import FileRow, Library, Run

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    level TEXT NOT NULL,
    english_only INTEGER NOT NULL DEFAULT 0,
    shrink_h265 INTEGER NOT NULL DEFAULT 0,
    remove_commentary INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    codec TEXT,
    container TEXT,
    resolution TEXT,
    was_status TEXT,
    was_reason TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status, size);
CREATE INDEX IF NOT EXISTS idx_files_library ON files(library_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    library_id INTEGER,
    file_path TEXT,
    state TEXT NOT NULL,
    step TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    was_size INTEGER,
    now_size INTEGER,
    was_format TEXT,
    now_format TEXT,
    now_path TEXT,
    was_bitrate INTEGER,
    now_bitrate INTEGER,
    why TEXT,
    tracks_json TEXT,
    checks_json TEXT,
    swap_json TEXT,
    error TEXT,
    log_tail TEXT,
    convert_seconds REAL,
    saved_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_file ON runs(file_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {"holding_enabled": "1"}

LIBRARY_FIELDS = {"name", "path", "level", "english_only", "shrink_h265", "remove_commentary",
                  "status"}
RUN_FIELDS = {
    "state", "step", "started_at", "finished_at", "was_size", "now_size",
    "was_format", "now_format", "now_path", "was_bitrate", "now_bitrate", "why",
    "tracks_json", "checks_json", "swap_json", "error", "log_tail", "convert_seconds",
    "saved_bytes",
}
RUN_COLUMNS = ["id", "file_id", *sorted(RUN_FIELDS)]
HELD_VOLUMES = "held_volumes"
QUEUE_ORDER = "size DESC, id"  # biggest first, as they save the most
RUNLESS_HISTORY_STATUSES = ("skipped", "failed", "missing", "waiting_in_use")
# What a run's row in the history says: a run a restart ended is not a failure of the file.
_RUN_ROW_STATUS = (
    "CASE WHEN r.state='interrupted' AND (f.id IS NULL OR f.status='failed') THEN 'interrupted'"
    " WHEN f.id IS NOT NULL THEN f.status ELSE r.state END"
)


NOT_OWNED = (
    "files.status NOT IN ('working','waiting_in_use') AND NOT EXISTS ("
    "SELECT 1 FROM runs WHERE runs.file_id=files.id"
    " AND runs.state IN ('swapping','waiting_in_use'))"
)


def _library(row: sqlite3.Row) -> Library:
    return Library(
        id=row["id"], name=row["name"], path=row["path"], level=row["level"],
        english_only=bool(row["english_only"]), status=row["status"],
        shrink_h265=bool(row["shrink_h265"]),
        remove_commentary=bool(row["remove_commentary"]),
    )


def _file(row: sqlite3.Row) -> FileRow:
    return FileRow(**{k: row[k] for k in FileRow.__dataclass_fields__})


def _run(row: sqlite3.Row) -> Run:
    return Run(**{k: row[k] for k in Run.__dataclass_fields__})


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)
        self._add_missing_columns()
        self._keep_private()

    def _keep_private(self) -> None:
        """Make the database readable by its owner only (Linux and macOS).

        Why: it holds the MCP token in plain text (the settings page has to show it again, so it
        cannot be hashed), the password hash and the sign-in session hashes. A default umask of
        022, as the Docker image uses, would let every other account on the machine, or every
        other container that mounts the same appdata folder, read the token and use it. The
        -wal and -shm side files copy the main file's mode when SQLite creates them, so only
        files that already exist need changing. Windows ignores these mode bits, and a failure
        here (a share that refuses chmod) must not stop ShelfSaver from starting.
        """
        if os.name == "nt":
            return
        for suffix in ("", "-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            try:
                os.chmod(side, 0o600)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Could not make %s private: %s", side.name, exc)

    def _add_missing_columns(self) -> None:
        """Bring a database from an earlier version up to date."""
        columns = {row["name"] for row in self._query("PRAGMA table_info(runs)")}
        if "convert_seconds" not in columns:
            self._exec("ALTER TABLE runs ADD COLUMN convert_seconds REAL")
        columns = {row["name"] for row in self._query("PRAGMA table_info(files)")}
        for column in ("was_status", "was_reason"):
            if column not in columns:
                self._exec(f"ALTER TABLE files ADD COLUMN {column} TEXT")
        columns = {row["name"] for row in self._query("PRAGMA table_info(libraries)")}
        if "shrink_h265" not in columns:
            self._exec("ALTER TABLE libraries ADD COLUMN shrink_h265 INTEGER NOT NULL DEFAULT 0")
        if "remove_commentary" not in columns:
            # Off for every library that already exists: nothing is removed unless asked for.
            self._exec("ALTER TABLE libraries ADD COLUMN remove_commentary INTEGER NOT NULL DEFAULT 0")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # FULL: a committed swap record must survive a power cut, because files move next.
            conn.execute("PRAGMA synchronous=FULL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        conn = self._conn()
        with conn:
            return conn.execute(sql, tuple(args))

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(args)).fetchall()

    # libraries
    def add_library(self, name: str, path: str, level: str, english_only: bool,
                    shrink_h265: bool = False, remove_commentary: bool = False) -> Library:
        cur = self._exec(
            "INSERT INTO libraries(name, path, level, english_only, shrink_h265, remove_commentary)"
            " VALUES (?,?,?,?,?,?)",
            (name, path, level, int(english_only), int(shrink_h265), int(remove_commentary)),
        )
        return self._get_library(cur.lastrowid)

    def _get_library(self, library_id: int) -> Library:
        return _library(self._query("SELECT * FROM libraries WHERE id=?", (library_id,))[0])

    def update_library(self, id: int, **fields: Any) -> None:
        bad = set(fields) - LIBRARY_FIELDS
        if bad:
            raise ValueError(f"unknown library fields: {sorted(bad)}")
        if not fields:
            return
        for flag in ("english_only", "shrink_h265", "remove_commentary"):
            if flag in fields:
                fields[flag] = int(fields[flag])
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE libraries SET {sets} WHERE id=?", (*fields.values(), id))

    def forget_saving_results(self, library_id: int) -> None:
        """Let H.265 files that showed too little saving be sampled again, at a new level."""
        marks = ",".join("?" * len(TRIED_REASONS))
        self._exec(f"UPDATE files SET reason='Already H.265' WHERE library_id=? AND codec='hevc'"
                   f" AND status='skipped' AND reason IN ({marks})", (library_id, *TRIED_REASONS))

    def remove_library(self, id: int) -> None:
        """Delete the library and its queue rows. Run history is kept."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM files WHERE library_id=?", (id,))
            conn.execute("DELETE FROM libraries WHERE id=?", (id,))

    def list_libraries(self) -> list[Library]:
        return [_library(r) for r in self._query("SELECT * FROM libraries ORDER BY id")]

    # settings
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        if rows:
            return rows[0]["value"]
        if default is None:
            return DEFAULT_SETTINGS.get(key)
        return default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def set_setting_if_absent(self, key: str, value: str) -> bool:
        """Store a setting only if there is none yet. Returns whether this call stored it."""
        cur = self._exec(
            "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
        return cur.rowcount > 0

    def held_volumes(self) -> list[str]:
        """Every volume that has held a file or had a library, even if its library is gone."""
        try:
            volumes = json.loads(self.get_setting(HELD_VOLUMES, "[]"))
        except ValueError:
            return []
        return [v for v in volumes if isinstance(v, str)] if isinstance(volumes, list) else []

    def remember_held_volume(self, volume: str) -> None:
        conn = self._conn()
        with conn:
            # Taken before reading, so two threads can't each overwrite the other's addition.
            conn.execute("BEGIN IMMEDIATE")
            volumes = self.held_volumes()
            if os.path.normcase(volume) in map(os.path.normcase, volumes):
                return
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (HELD_VOLUMES, json.dumps([*volumes, volume])),
            )

    # sessions: only a hash of each token is stored
    def add_session(self, token_hash: str, expires_at: float) -> None:
        self._exec("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        self._exec("INSERT OR REPLACE INTO sessions(token_hash, expires_at) VALUES (?,?)",
                   (token_hash, expires_at))

    def session_expiry(self, token_hash: str) -> Optional[float]:
        rows = self._query("SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,))
        return rows[0]["expires_at"] if rows else None

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    # files
    def upsert_file(
        self, library_id: int, path: str, size: int, mtime: float, status: str,
        reason: Optional[str] = None, codec: Optional[str] = None,
        container: Optional[str] = None, resolution: Optional[str] = None,
    ) -> FileRow:
        self._exec(
            "INSERT INTO files(library_id, path, size, mtime, status, reason, codec,"
            " container, resolution, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET library_id=excluded.library_id,"
            " size=excluded.size, mtime=excluded.mtime, status=excluded.status,"
            " reason=excluded.reason, codec=COALESCE(excluded.codec, codec),"
            " container=COALESCE(excluded.container, container),"
            " resolution=COALESCE(excluded.resolution, resolution),"
            " updated_at=excluded.updated_at",
            (library_id, path, size, mtime, status, reason, codec, container,
             resolution, time.time()),
        )
        return self.get_file_by_path(path)

    def upsert_file_unless_owned(
        self, library_id: int, path: str, size: int, mtime: float, status: str,
        reason: Optional[str] = None, codec: Optional[str] = None,
        container: Optional[str] = None, resolution: Optional[str] = None,
    ) -> bool:
        """Like upsert_file, but one statement that changes nothing a worker owns.

        Returns whether the row was written.
        """
        cur = self._exec(
            "INSERT INTO files(library_id, path, size, mtime, status, reason, codec,"
            " container, resolution, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET library_id=excluded.library_id,"
            " size=excluded.size, mtime=excluded.mtime, status=excluded.status,"
            " reason=excluded.reason, codec=COALESCE(excluded.codec, codec),"
            " container=COALESCE(excluded.container, container),"
            " resolution=COALESCE(excluded.resolution, resolution),"
            " updated_at=excluded.updated_at WHERE " + NOT_OWNED,
            (library_id, path, size, mtime, status, reason, codec, container,
             resolution, time.time()),
        )
        return cur.rowcount > 0

    def delete_file(self, id: int) -> None:
        """Forget a file's row. Its runs stay, as the record of what was tried."""
        self._exec("DELETE FROM files WHERE id=?", (id,))

    def set_missing_unless_owned(self, id: int) -> bool:
        """Mark a file missing, remembering its status in case it was only renamed."""
        cur = self._exec(
            "UPDATE files SET was_status=status, was_reason=reason, status='missing',"
            " reason=NULL, updated_at=? WHERE id=? AND " + NOT_OWNED, (time.time(), id))
        return cur.rowcount > 0

    def files_with_size_and_time(self, library_id: int, size: int, mtime: float) -> list[FileRow]:
        rows = self._query("SELECT * FROM files WHERE library_id=? AND size=? AND mtime=?",
                           (library_id, size, mtime))
        return [_file(row) for row in rows]

    def follow_rename(self, id: int, path: str) -> Optional[FileRow]:
        """Give a file its new name and bring its history along. A file marked missing gets its
        remembered status back, if it has one. None if the worker owns the file."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE files SET path=?,"
                " reason=CASE WHEN status='missing' AND was_status IS NOT NULL"
                " THEN was_reason ELSE reason END,"
                " status=CASE WHEN status='missing' AND was_status IS NOT NULL"
                " THEN was_status ELSE status END,"
                " was_status=NULL, was_reason=NULL, updated_at=? WHERE id=? AND " + NOT_OWNED,
                (path, time.time(), id))
            if cur.rowcount == 0:
                return None
            conn.execute(
                "UPDATE runs SET file_path=?,"
                " now_path=CASE WHEN now_path IS NULL THEN NULL ELSE ? END WHERE file_id=?",
                (path, path, id))
        return self.get_file(id)

    def claim_file(
        self, id: int, expected_size: int, expected_mtime: float, size: int, mtime: float,
        codec: Optional[str] = None, container: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> bool:
        """Mark a queued row as working, only if it is still queued and unchanged."""
        cur = self._exec(
            "UPDATE files SET status='working', reason=NULL, size=?, mtime=?,"
            " codec=COALESCE(?, codec), container=COALESCE(?, container),"
            " resolution=COALESCE(?, resolution), updated_at=?"
            " WHERE id=? AND status='queued' AND size=? AND mtime=?",
            (size, mtime, codec, container, resolution, time.time(), id,
             expected_size, expected_mtime))
        return cur.rowcount > 0

    def get_file(self, id: int) -> Optional[FileRow]:
        rows = self._query("SELECT * FROM files WHERE id=?", (id,))
        return _file(rows[0]) if rows else None

    def get_file_by_path(self, path: str) -> Optional[FileRow]:
        rows = self._query("SELECT * FROM files WHERE path=?", (path,))
        return _file(rows[0]) if rows else None

    def set_file_status(self, id: int, status: str, reason: Optional[str] = None) -> None:
        self._exec(
            "UPDATE files SET status=?, reason=?, updated_at=? WHERE id=?",
            (status, reason, time.time(), id),
        )

    def next_queued(self, skip_libraries: Iterable[int] = ()) -> Optional[FileRow]:
        skip = list(skip_libraries)
        not_in = f" AND library_id NOT IN ({','.join('?' * len(skip))})" if skip else ""
        rows = self._query(
            f"SELECT * FROM files WHERE status='queued'{not_in} ORDER BY {QUEUE_ORDER} LIMIT 1",
            skip,
        )
        return _file(rows[0]) if rows else None

    def queued_files(self, limit: int, offset: int = 0) -> list[FileRow]:
        return [_file(r) for r in self._query(
            f"SELECT * FROM files WHERE status='queued' ORDER BY {QUEUE_ORDER} LIMIT ? OFFSET ?",
            (limit, offset))]

    def queue_totals(self) -> dict:
        row = self._query("SELECT COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes FROM files"
                          " WHERE status='queued'")[0]
        return {"files": row["files"], "bytes": row["bytes"]}

    def files_with_status(self, status: str) -> list[FileRow]:
        return [_file(r) for r in self._query(
            "SELECT * FROM files WHERE status=? ORDER BY id", (status,))]

    def list_files(self, status: Optional[str] = None, library_id: Optional[int] = None,
                   limit: int = 100) -> list[FileRow]:
        where, args = [], []
        if status is not None:
            where.append("status=?")
            args.append(status)
        if library_id is not None:
            where.append("library_id=?")
            args.append(library_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return [_file(r) for r in self._query(
            f"SELECT * FROM files{clause} ORDER BY id LIMIT ?", [*args, limit])]

    def files_in_library(self, library_id: int) -> list[FileRow]:
        return [_file(r) for r in self._query(
            "SELECT * FROM files WHERE library_id=? ORDER BY id", (library_id,))]

    def record_converted(
        self, id: int, path: str, size: int, mtime: float, codec: Optional[str],
        container: Optional[str], resolution: Optional[str],
    ) -> None:
        """Point a file's row at its new version. Any other row for that path is replaced."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM files WHERE path=? AND id<>?", (path, id))
            conn.execute(
                "UPDATE files SET path=?, size=?, mtime=?, status='converted', reason=NULL,"
                " codec=?, container=?, resolution=?, updated_at=? WHERE id=?",
                (path, size, mtime, codec, container, resolution, time.time(), id),
            )

    def requeue(self, ids: Iterable[int]) -> None:
        conn = self._conn()
        with conn:
            conn.executemany(
                "UPDATE files SET status='queued', reason=NULL, updated_at=? WHERE id=?",
                [(time.time(), i) for i in ids],
            )

    # runs
    def start_run(
        self, file_id: int, was_size: int, was_format: str, was_bitrate: Optional[int],
        why: str,
    ) -> Run:
        f = self.get_file(file_id)
        cur = self._exec(
            "INSERT INTO runs(file_id, library_id, file_path, state, step, started_at,"
            " was_size, was_format, was_bitrate, why) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (file_id, f.library_id if f else None, f.path if f else None, "running",
             "starting", time.time(), was_size, was_format, was_bitrate, why),
        )
        return self._get_run(cur.lastrowid)

    def was_converted_from(self, file_id: int, size: int) -> bool:
        """True if a finished run on this file started from a file of exactly this size."""
        return bool(self._query(
            "SELECT 1 FROM runs WHERE file_id=? AND state='done' AND was_size=? LIMIT 1",
            (file_id, size)))

    def run_record(self, run_id: int) -> Optional[dict]:
        """Every column of a run, including the path it started from."""
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def _get_run(self, run_id: int) -> Run:
        return _run(self._query("SELECT * FROM runs WHERE id=?", (run_id,))[0])

    def update_run(self, id: int, **fields: Any) -> None:
        bad = set(fields) - RUN_FIELDS
        if bad:
            raise ValueError(f"unknown run fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), id))

    def active_runs(self, states: Iterable[str]) -> list[Run]:
        states = list(states)
        if not states:
            return []
        marks = ",".join("?" * len(states))
        rows = self._query(
            f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY id", states
        )
        return [_run(r) for r in rows]

    def history(
        self, library_id: Optional[int] = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        where, args = "", []
        if library_id is not None:
            where = "WHERE library_id=?"
            args.append(library_id)
        rows = self._query(
            f"SELECT * FROM runs {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [dict(r) for r in rows]

    def timeline(
        self, library_id: Optional[int] = None, limit: int = 100, offset: int = 0,
        kind: Optional[str] = None,
    ) -> list[dict]:
        """Runs, and files that never had one, most recently updated first.

        Each item has kind ("run" or "file"), id, status and at. Filtering happens here so that
        paging reaches every match, however many other rows there are.
        """
        marks = ",".join("?" * len(RUNLESS_HISTORY_STATUSES))
        where, args = [], [*RUNLESS_HISTORY_STATUSES]
        if library_id is not None:
            where.append("library_id=?")
            args.append(library_id)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._query(
            "SELECT kind, id, status, at FROM ("
            f" SELECT 'run' AS kind, r.id AS id, {_RUN_ROW_STATUS} AS status,"
            "  COALESCE(r.finished_at, r.started_at) AS at, r.library_id AS library_id"
            "  FROM runs r LEFT JOIN files f ON f.id=r.file_id"
            " UNION ALL"
            " SELECT 'file', f.id, f.status, f.updated_at, f.library_id FROM files f"
            f"  WHERE f.status IN ({marks})"
            "  AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.file_id=f.id)"
            f"){clause} ORDER BY at DESC, kind DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [dict(r) for r in rows]

    def file_details(self, file_id: int) -> dict:
        f = self.get_file(file_id)
        runs = self._query(
            "SELECT * FROM runs WHERE file_id=? ORDER BY started_at DESC, id DESC",
            (file_id,),
        )
        return {"file": dict(f.__dict__) if f else None, "runs": [dict(r) for r in runs]}

    def stats(self, library_id: Optional[int] = None) -> dict:
        run_where, file_where, args = "", "", []
        if library_id is not None:
            run_where = " AND library_id=?"
            file_where = " AND library_id=?"
            args.append(library_id)
        saved = self._query(
            "SELECT COALESCE(SUM(saved_bytes),0) AS n FROM runs WHERE state='done'"
            + run_where, args)[0]["n"]
        converted = self._query(
            "SELECT COUNT(*) AS n FROM runs WHERE state='done'" + run_where, args)[0]["n"]
        remaining = self._query(
            "SELECT COUNT(*) AS n FROM files WHERE status IN ('queued','working',"
            "'waiting_in_use')" + file_where, args)[0]["n"]
        total = self._query(
            "SELECT COUNT(*) AS n FROM files WHERE status<>'missing'" + file_where, args)[0]["n"]
        return {"saved_bytes": saved, "converted": converted, "remaining": remaining,
                "total": total}


def ensure_mcp_token(db: Database) -> str:
    """Return the token that guards the MCP endpoint, creating it on first use."""
    db.set_setting_if_absent("mcp_token", secrets.token_urlsafe(32))
    return db.get_setting("mcp_token")
