from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import connect, init_db, tx
from .htmlwork import download_html, extract_nodes, save_original, save_translated, slug_for, validate_habr_url, xpath_for
from .worker import start_worker, stop_worker

STATIC = Path(__file__).parent / "static"
PUBLIC = Path(__file__).parent.parent / "public"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_worker()
    yield
    stop_worker()


app = FastAPI(title="Habr MADLAD + OpenAI PWA", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/public", StaticFiles(directory=PUBLIC), name="public")


class JobInput(BaseModel):
    url: str


def serialize(row):
    item = dict(row)
    total = max(item["total_nodes"], 1)
    item["translation_progress"] = 100 if item["total_nodes"] == 0 else round((item["done_nodes"] / total) * 100)
    item["refinement_progress"] = 100 if item["total_nodes"] == 0 else round((item.get("refined_nodes", 0) / total) * 100)
    # Kept for compatibility with older cached frontends.
    item["progress"] = item["translation_progress"]
    item["phase"] = item.get("status", "pending")
    return item


@app.get("/api/jobs")
def list_jobs(status: str | None = None):
    # Always read the same canonical result set, then filter in Python. This
    # avoids status-query mismatches between cached PWAs and newer backends.
    with connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    items = [serialize(r) for r in rows]
    normalized = (status or "").strip().lower()
    if normalized == "complete":
        items = [item for item in items if item.get("status") == "complete"]
    elif normalized == "pending":
        items = [item for item in items if item.get("status") != "complete"]
    return JSONResponse(items, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/api/debug/jobs")
def debug_jobs():
    with connect() as conn:
        rows = conn.execute("SELECT id,url,status,total_nodes,done_nodes,error,created_at,updated_at FROM jobs ORDER BY id DESC LIMIT 20").fetchall()
    return JSONResponse([dict(r) for r in rows], headers={"Cache-Control": "no-store"})


@app.post("/api/jobs")
def create_job(payload: JobInput):
    try:
        url = validate_habr_url(payload.url)
        slug = slug_for(url)
        with connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE slug=?", (slug,)).fetchone()
        if existing:
            # Duplicate submissions are common on mobile/PWA. Do not pretend that a
            # new pending job was created. Return the real current state so the UI
            # can open the correct tab. Failed jobs are safely re-queued while
            # preserving already completed translation/polishing work.
            if existing["status"] == "error":
                with tx() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (existing["id"],),
                    )
                    conn.execute(
                        "UPDATE nodes SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('running','error')",
                        (existing["id"],),
                    )
                    conn.execute(
                        "UPDATE nodes SET refine_status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND refine_status IN ('running','error')",
                        (existing["id"],),
                    )
                    existing = conn.execute("SELECT * FROM jobs WHERE id=?", (existing["id"],)).fetchone()
            item = serialize(existing)
            item["was_existing"] = True
            return item

        html = download_html(url)
        soup, nodes = extract_nodes(html)
        original_path = save_original(slug, html)
        with tx() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(url,slug,status,original_path,total_nodes) VALUES(?,?,?,?,?)",
                (url, slug, "pending", str(original_path), len(nodes)),
            )
            job_id = cur.lastrowid
            for index, node in enumerate(nodes):
                conn.execute(
                    "INSERT INTO nodes(job_id,node_index,xpath,source_text,status) VALUES(?,?,?,?, 'pending')",
                    (job_id, index, xpath_for(node), str(node)),
                )
            if not nodes:
                translated = save_translated(slug, soup)
                conn.execute("UPDATE jobs SET status='complete', translated_path=? WHERE id=?", (str(translated), job_id))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        item = serialize(row)
        item["was_existing"] = False
        return item
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"HTML 다운로드 실패: {exc}")


@app.get("/api/jobs/{job_id}/original")
def original(job_id: int):
    with connect() as conn:
        row = conn.execute("SELECT original_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row["original_path"]:
        raise HTTPException(404)
    return FileResponse(row["original_path"], media_type="text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/translated")
def translated(job_id: int):
    with connect() as conn:
        row = conn.execute("SELECT translated_path,status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "complete" or not row["translated_path"]:
        raise HTTPException(404)
    return FileResponse(
        row["translated_path"],
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'self' 'unsafe-inline' https:; "
                "img-src 'self' data: blob: https:; "
                "font-src 'self' data: https:; "
                "media-src 'self' data: https:; "
                "script-src 'none'; connect-src 'none'; "
                "frame-src 'none'; object-src 'none'; worker-src 'none'; "
                "base-uri 'none'; form-action 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
