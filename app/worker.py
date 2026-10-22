from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .db import connect, tx
from .htmlwork import extract_nodes, save_translated
from .openai_editor import chunks, refine_batch
from .translator import translate_text, unload_engine

_stop = threading.Event()
_thread: threading.Thread | None = None
log = logging.getLogger("habr-worker")


def _claim_job():
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('pending','running','translating','refining','polishing') ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        next_status = "refining" if row["total_nodes"] > 0 and row["done_nodes"] >= row["total_nodes"] else "translating"
        conn.execute(
            "UPDATE jobs SET status=?, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (next_status, row["id"]),
        )
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())


def _load_document(job: dict):
    html = Path(job["original_path"]).read_text(encoding="utf-8")
    soup, dom_nodes = extract_nodes(html)
    with connect() as conn:
        db_nodes = conn.execute(
            "SELECT * FROM nodes WHERE job_id=? ORDER BY node_index", (job["id"],)
        ).fetchall()
    if len(dom_nodes) != len(db_nodes):
        raise RuntimeError("저장된 번역 노드와 HTML 구조가 일치하지 않습니다.")
    return soup, dom_nodes, db_nodes


def _refresh_counts(conn, job_id: int) -> None:
    conn.execute(
        """UPDATE jobs
              SET done_nodes=(SELECT COUNT(*) FROM nodes WHERE job_id=? AND status='done'),
                  refined_nodes=(SELECT COUNT(*) FROM nodes WHERE job_id=? AND refine_status IN ('done','fallback')),
                  fallback_nodes=(SELECT COUNT(*) FROM nodes WHERE job_id=? AND refine_status='fallback'),
                  updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (job_id, job_id, job_id, job_id),
    )


def _translate(job: dict) -> None:
    log.warning("job %s: MADLAD translation started", job["id"])
    _soup, _dom_nodes, rows = _load_document(job)
    for row in rows:
        if _stop.is_set():
            return
        if row["status"] == "done" and row["translated_text"] is not None:
            continue
        with tx() as conn:
            conn.execute("UPDATE nodes SET status='running', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        translated = translate_text(row["source_text"])
        with tx() as conn:
            conn.execute(
                "UPDATE nodes SET translated_text=?, status='done', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (translated, row["id"]),
            )
            _refresh_counts(conn, job["id"])

    if not _stop.is_set():
        with tx() as conn:
            conn.execute("UPDATE jobs SET status='refining', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
    log.warning("job %s: MADLAD translation complete", job["id"])


def _refine(job: dict) -> None:
    log.warning("job %s: OpenAI refinement started", job["id"])
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE job_id=? AND status='done' AND refine_status NOT IN ('done','fallback') ORDER BY node_index",
            (job["id"],),
        ).fetchall()

    for batch_rows in chunks(rows):
        if _stop.is_set():
            return
        node_ids = [row["id"] for row in batch_rows]
        with tx() as conn:
            conn.executemany(
                "UPDATE nodes SET refine_status='running', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [(node_id,) for node_id in node_ids],
            )

        items = [
            {"id": row["id"], "source": row["source_text"].strip(), "draft": row["translated_text"].strip()}
            for row in batch_rows
        ]
        successful, failed = refine_batch(items)

        with tx() as conn:
            for row in batch_rows:
                node_id = row["id"]
                if node_id in successful:
                    # Preserve whitespace that belonged to the original HTML text node.
                    source = row["source_text"]
                    prefix = source[: len(source) - len(source.lstrip())]
                    suffix = source[len(source.rstrip()) :]
                    final = prefix + successful[node_id].strip() + suffix
                    conn.execute(
                        "UPDATE nodes SET refined_text=?, refine_status='done', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (final, node_id),
                    )
                else:
                    conn.execute(
                        "UPDATE nodes SET refined_text=NULL, refine_status='fallback', error='OpenAI 후편집 실패: MADLAD 결과 유지', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (node_id,),
                    )
            _refresh_counts(conn, job["id"])

    log.warning("job %s: OpenAI refinement complete", job["id"])


def _finalize(job: dict) -> None:
    soup, dom_nodes, rows = _load_document(job)
    for row, dom_node in zip(rows, dom_nodes):
        final_text = row["refined_text"] or row["translated_text"] or row["source_text"]
        dom_node.replace_with(final_text)
    translated_path = save_translated(job["slug"], soup)
    with tx() as conn:
        _refresh_counts(conn, job["id"])
        conn.execute(
            "UPDATE jobs SET status='complete', translated_path=?, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(translated_path), job["id"]),
        )
    log.warning("job %s: complete", job["id"])


def _process_job(job: dict) -> None:
    with connect() as conn:
        current = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
    if current["done_nodes"] < current["total_nodes"]:
        _translate(current)
    if _stop.is_set():
        return
    _refine(current)
    if not _stop.is_set():
        _finalize(current)


def _loop():
    while not _stop.is_set():
        job = _claim_job()
        if not job:
            _stop.wait(1.0)
            continue
        try:
            _process_job(job)
        except Exception as exc:
            log.exception("job %s failed", job["id"])
            unload_engine()
            with tx() as conn:
                conn.execute("UPDATE nodes SET status='pending' WHERE job_id=? AND status='running'", (job["id"],))
                conn.execute("UPDATE nodes SET refine_status='pending' WHERE job_id=? AND refine_status='running'", (job["id"],))
                conn.execute(
                    "UPDATE jobs SET status='error', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(exc), job["id"]),
                )
        time.sleep(0.2)


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="translation-worker", daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
