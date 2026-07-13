"""书舟 v0.1 - FastAPI 入口。

启动: uv run uvicorn app.main:app --port 8765 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db, ingest, watcher
from .routes import library, reader, settings

LIBRARY_DIR = Path(__file__).resolve().parent.parent / "books-library"
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    w = watcher.start_watcher(LIBRARY_DIR)
    print(f"[readflow] 监听书库: {LIBRARY_DIR}")
    yield
    w.stop()


app = FastAPI(title="书舟", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(library.router)
app.include_router(library.pages)
app.include_router(reader.router)
app.include_router(settings.router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """书库首页:书籍网格。点击进详情页 /book/{id}。"""
    books = library.list_books()
    base = str(request.base_url).rstrip("/")
    cards = []
    for b in books:
        cover = (
            f'<img src="{base}/api/library/{b["id"]}/cover">'
            if b["cover_path"] else
            '<div class="cover-placeholder">无封面</div>'
        )
        status = (
            '<span class="badge bad">不支持</span>'
            if b["ingest_status"] == "unsupported" else ""
        )
        # 评分(有才显示)
        rating = (
            f'<span class="rating">★ {float(b["rating"]):.1f}</span>'
            if b.get("rating") is not None else ""
        )
        prog = int((b["progress"] or 0) * 100)
        cards.append(f"""
        <a class="card" href="/book/{b['id']}">
          {cover}
          <div class="meta">
            <div class="title">{b['title'] or '无标题'}{status}</div>
            <div class="author">{b['author'] or ''}</div>
            <div class="card-rating">{rating}</div>
            <div class="progress-bar"><div style="width:{prog}%"></div></div>
          </div>
        </a>""")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>书舟</title><link rel="stylesheet" href="{base}/static/css/index.css">
</head><body>
<header><h1>书舟</h1><span class="hint">把 epub 放入 books-library/ 自动入库</span></header>
<main class="grid">{''.join(cards)}</main>
</body></html>"""
