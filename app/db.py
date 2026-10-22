from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("/data/app.db")
DB_VERSION = 8


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with connect() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending',
          error TEXT,
          original_path TEXT,
          translated_path TEXT,
          total_nodes INTEGER NOT NULL DEFAULT 0,
          done_nodes INTEGER NOT NULL DEFAULT 0,
          refined_nodes INTEGER NOT NULL DEFAULT 0,
          fallback_nodes INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS nodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          node_index INTEGER NOT NULL,
          xpath TEXT NOT NULL,
          source_text TEXT NOT NULL,
          translated_text TEXT,
          refined_text TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          refine_status TEXT NOT NULL DEFAULT 'pending',
          error TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(job_id, node_index)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
        CREATE INDEX IF NOT EXISTS idx_nodes_job_status ON nodes(job_id, status, node_index);
        """)

        # Add columns before creating indexes that reference them.
        # This ordering is required when upgrading an existing v7 database.
        job_columns = _columns(conn, "jobs")
        node_columns = _columns(conn, "nodes")
        if "refined_nodes" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN refined_nodes INTEGER NOT NULL DEFAULT 0")
        if "fallback_nodes" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN fallback_nodes INTEGER NOT NULL DEFAULT 0")
        if "refined_text" not in node_columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN refined_text TEXT")
        if "refine_status" not in node_columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN refine_status TEXT NOT NULL DEFAULT 'pending'")

        # Recover interrupted work. Existing completed v7 jobs remain complete and
        # are not silently sent to a paid API. New/incomplete jobs continue normally.
        conn.execute("UPDATE nodes SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE status IN ('running','error')")
        conn.execute("UPDATE nodes SET refine_status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE refine_status IN ('running','error')")
        conn.execute("UPDATE jobs SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE status IN ('running','translating','refining','polishing','error')")

        conn.execute(
            "UPDATE jobs SET done_nodes=(SELECT COUNT(*) FROM nodes WHERE nodes.job_id=jobs.id AND status='done')"
        )
        conn.execute(
            "UPDATE jobs SET refined_nodes=(SELECT COUNT(*) FROM nodes WHERE nodes.job_id=jobs.id AND refine_status IN ('done','fallback'))"
        )
        conn.execute(
            "UPDATE jobs SET fallback_nodes=(SELECT COUNT(*) FROM nodes WHERE nodes.job_id=jobs.id AND refine_status='fallback')"
        )

        conn.execute(f"PRAGMA user_version={DB_VERSION}")
        conn.commit()


@contextmanager
def tx():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
