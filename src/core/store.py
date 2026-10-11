"""Dubarr's database: what's in each file, what was changed, and what can be restored.

One SQLite file in CONFIG_DIR. Three tables:
  files    every video file seen, its tracks and what Dubarr decided about it
  history  every file Dubarr changed, and where the original went
  runs     each batch of changes, so a whole batch can be undone at once
"""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    library TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    links INTEGER NOT NULL DEFAULT 1,
    info TEXT,               -- the tracks, as JSON, so changed settings don't mean reading the file again
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    plan TEXT,
    saved_bytes INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,   -- restored files are left alone until you say otherwise
    checked_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    trigger TEXT NOT NULL,
    changed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    saved_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    path TEXT NOT NULL,
    backup_path TEXT,        -- where the original is: next to the file during the swap, then in the recycle folder
    original_size INTEGER NOT NULL DEFAULT 0,
    new_size INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    removed TEXT,
    state TEXT NOT NULL,     -- swapping, done, deleted, purged, restored, failed
    note TEXT NOT NULL DEFAULT '',
    changed_at REAL NOT NULL,
    restored_at REAL
);
CREATE INDEX IF NOT EXISTS history_run ON history(run_id);
"""

RESTORABLE = "done"


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # Files

    def file(self, path):
        row = self._one("SELECT * FROM files WHERE path = ?", (path,))
        return _decode_file(row) if row else None

    def save_file(self, path, library, size, mtime, links, info, status, message, plan=None, saved_bytes=0):
        self._run(
            """INSERT INTO files (path, library, size, mtime, links, info, status, message, plan, saved_bytes, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET library=excluded.library, size=excluded.size, mtime=excluded.mtime,
                 links=excluded.links, info=excluded.info, status=excluded.status, message=excluded.message,
                 plan=excluded.plan, saved_bytes=excluded.saved_bytes, checked_at=excluded.checked_at""",
            (path, library, size, mtime, links, json.dumps(info) if info is not None else None, status, message,
             json.dumps(plan) if plan is not None else None, saved_bytes, time.time()),
        )

    def set_ignored(self, path, ignored: bool):
        self._run("UPDATE files SET ignored = ? WHERE path = ?", (1 if ignored else 0, path))

    def forget_missing(self, library, present: set):
        """Drop files under a library that weren't found in the latest scan."""
        known = [r["path"] for r in self._all("SELECT path FROM files WHERE library = ?", (library,))]
        gone = [p for p in known if p not in present]
        with self._lock:
            self._db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
        return len(gone)

    def forget_libraries(self, keep: list):
        marks = ",".join("?" * len(keep)) or "''"
        self._run(f"DELETE FROM files WHERE library NOT IN ({marks})", tuple(keep))

    def files(self, statuses=None, ignored=None, search="", limit=500, offset=0):
        sql, args = "SELECT * FROM files WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if ignored is not None:
            sql += " AND ignored = ?"
            args.append(1 if ignored else 0)
        if search:
            sql += " AND path LIKE ? ESCAPE '\\'"
            args.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        count = self._one(sql.replace("SELECT *", "SELECT COUNT(*) AS n", 1), args)["n"]
        sql += " ORDER BY path LIMIT ? OFFSET ?"
        return count, [_decode_file(r) for r in self._all(sql, args + [limit, offset])]

    def counts(self):
        rows = self._all("""SELECT CASE WHEN ignored = 1 THEN 'ignored' ELSE status END AS status,
                                   COUNT(*) AS n, SUM(saved_bytes) AS saved
                            FROM files GROUP BY 1""")
        return {r["status"]: {"files": r["n"], "saved_bytes": r["saved"] or 0} for r in rows}

    # Runs

    def start_run(self, trigger):
        return self._run("INSERT INTO runs (started, trigger) VALUES (?, ?)", (time.time(), trigger)).lastrowid

    def finish_run(self, run_id, changed, failed, saved_bytes):
        self._run("UPDATE runs SET finished = ?, changed = ?, failed = ?, saved_bytes = ? WHERE id = ?",
                  (time.time(), changed, failed, saved_bytes, run_id))

    def runs(self, limit=20):
        return self._all("SELECT * FROM runs WHERE changed > 0 OR failed > 0 ORDER BY id DESC LIMIT ?", (limit,))

    def last_run_with_restorable(self):
        row = self._one("SELECT run_id FROM history WHERE state = ? AND run_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (RESTORABLE,))
        return row["run_id"] if row else None

    # History

    def add_history(self, run_id, path, backup_path, original_size, message, removed):
        return self._run(
            """INSERT INTO history (run_id, path, backup_path, original_size, message, removed, state, changed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'swapping', ?)""",
            (run_id, path, backup_path, original_size, message, json.dumps(removed), time.time()),
        ).lastrowid

    def update_history(self, history_id, **fields):
        if not fields:
            return
        keys = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE history SET {keys} WHERE id = ?", (*fields.values(), history_id))

    def history_entry(self, history_id):
        row = self._one("SELECT * FROM history WHERE id = ?", (history_id,))
        return _decode_history(row) if row else None

    def history(self, limit=200, offset=0, search="", run_id=None):
        sql, args = "SELECT * FROM history WHERE 1=1", []
        if search:
            sql += " AND path LIKE ? ESCAPE '\\'"
            args.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        count = self._one(sql.replace("SELECT *", "SELECT COUNT(*) AS n", 1), args)["n"]
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return count, [_decode_history(r) for r in self._all(sql, args + [limit, offset])]

    def history_in_state(self, *states):
        marks = ",".join("?" * len(states))
        return [_decode_history(r) for r in self._all(f"SELECT * FROM history WHERE state IN ({marks}) ORDER BY id", states)]

    def total_saved(self):
        row = self._one("SELECT SUM(original_size - new_size) AS saved, COUNT(*) AS n FROM history "
                        "WHERE state IN ('done', 'deleted', 'purged')")
        return {"files": row["n"] or 0, "saved_bytes": row["saved"] or 0}


def _decode_file(row):
    row = dict(row)
    row["info"] = json.loads(row["info"]) if row.get("info") else None
    row["plan"] = json.loads(row["plan"]) if row.get("plan") else None
    row["ignored"] = bool(row["ignored"])
    return row


def _decode_history(row):
    row = dict(row)
    row["removed"] = json.loads(row["removed"]) if row.get("removed") else []
    return row
