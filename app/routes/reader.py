"""阅读器:页面 + epub 文件服务 + 进度 + 划线。

定位模型(采纳 MRD 评审):
- 阅读进度:spine_index + cfi(foliate-js relocate 事件返回的 CFI)
- 划线:start_cfi/end_cfi + 原文,稳定不随排版漂移
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .. import db

router = APIRouter(tags=["reader"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------- 阅读器页面 ----------
@router.get("/read/{book_id}", response_class=HTMLResponse)
def reader_page(book_id: int, request: Request):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, author FROM books WHERE id=?", (book_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")
    base = str(request.base_url).rstrip("/")
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{row['title']} - 书舟</title>
<link rel="stylesheet" href="{base}/static/css/reader.css">
</head>
<body>
<div id="toolbar">
  <button id="back" title="返回">←</button>
  <span id="title">{row['title']}</span>
  <span id="progress">0%</span>
  <button id="toc-btn" title="目录">☰</button>
</div>
<div id="viewer" data-book-id="{book_id}" data-base="{base}"></div>
<div id="bottom-bar" hidden>
  <button data-act="highlight">划线</button>
  <button data-act="copy">复制</button>
</div>

<!-- 全局错误捕获:把异常显示到页面,方便不用 devtools 也能看到 -->
<script>
window.addEventListener('error', e => {{
  const d = document.createElement('pre');
  d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#c00;color:#fff;padding:12px;font-size:12px;white-space:pre-wrap;z-index:999';
  d.textContent = '[error] ' + (e.error?.stack || e.message);
  document.body.append(d);
}});
window.addEventListener('unhandledrejection', e => {{
  const d = document.createElement('pre');
  d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#c00;color:#fff;padding:12px;font-size:12px;white-space:pre-wrap;z-index:999';
  d.textContent = '[promise] ' + (e.reason?.stack || e.reason);
  document.body.append(d);
}});
</script>
<script type="module" src="{base}/static/reader.js"></script>
</body>
</html>"""


# ---------- epub 文件服务 ----------
@router.get("/api/books/{book_id}/file")
def get_epub_file(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT epub_path, title FROM books WHERE id=? AND ingest_status='ready'",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not available")
    p = Path(row["epub_path"])
    if not p.is_file():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(str(p), media_type="application/epub+zip",
                        filename=f"{row['title']}.epub")


# ---------- 阅读进度 ----------
class ProgressIn(BaseModel):
    spine_index: int
    cfi: str | None = None
    percent: float


@router.get("/api/books/{book_id}/progress")
def get_progress(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT spine_index, cfi, percent FROM reading_progress WHERE book_id=?",
            (book_id,),
        ).fetchone()
    return dict(row) if row else {"spine_index": 0, "cfi": None, "percent": 0.0}


@router.put("/api/books/{book_id}/progress")
def put_progress(book_id: int, body: ProgressIn):
    with db.db() as conn:
        conn.execute(
            """INSERT INTO reading_progress(book_id, spine_index, cfi, percent, updated_at)
               VALUES(?,?,?,?,datetime('now'))
               ON CONFLICT(book_id) DO UPDATE SET
                 spine_index=excluded.spine_index,
                 cfi=excluded.cfi,
                 percent=excluded.percent,
                 updated_at=datetime('now')""",
            (book_id, body.spine_index, body.cfi, body.percent),
        )
    return {"ok": True}


# ---------- 划线 ----------
class HighlightIn(BaseModel):
    spine_index: int
    start_cfi: str
    end_cfi: str
    text: str
    color: str = "yellow"


@router.get("/api/books/{book_id}/highlights")
def list_highlights(book_id: int):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, spine_index, start_cfi, end_cfi, text, color, created_at "
            "FROM highlights WHERE book_id=? ORDER BY created_at",
            (book_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/books/{book_id}/highlights")
def add_highlight(book_id: int, body: HighlightIn):
    with db.db() as conn:
        cur = conn.execute(
            """INSERT INTO highlights(book_id, spine_index, start_cfi, end_cfi, text, color)
               VALUES(?,?,?,?,?,?)""",
            (book_id, body.spine_index, body.start_cfi, body.end_cfi, body.text, body.color),
        )
        hid = cur.lastrowid
    return {"id": hid, "ok": True}


@router.delete("/api/highlights/{hid}")
def del_highlight(hid: int):
    with db.db() as conn:
        conn.execute("DELETE FROM highlights WHERE id=?", (hid,))
    return {"ok": True}
