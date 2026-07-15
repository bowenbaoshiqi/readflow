"""书舟 v0.1 - FastAPI 入口。

启动: uv run uvicorn app.main:app --port 8765 --reload
"""
from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, ingest, watcher
from .config import LIBRARY_DIR, STATIC_DIR
from .routes import knowledge as knowledge_module, library, reader, settings

# 字体 MIME:Debian slim 的 mime 数据库不认 .ttf/.otf/.woff(.woff2),
# StaticFiles 会回退 application/octet-stream;Chrome 对 @font-face 的 src
# 严格校验 MIME,octet-stream 会被拒 → 字体不生效(显示默认 serif)。
# macOS 本地 mime 库有 .ttf,故本地正常、Docker 内失效。显式补上。
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    w = watcher.start_watcher(LIBRARY_DIR)
    print(f"[readflow] 监听书库: {LIBRARY_DIR}")

    # 每日凌晨批处理(知识卡片 + 推荐)
    from .jobs import run_daily_job
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_daily_job, "cron", hour=2, minute=0, id="daily_cards")
    scheduler.start()
    print("[readflow] 每日知识卡片批处理已注册 (02:00)")

    yield
    scheduler.shutdown(wait=False)
    w.stop()


app = FastAPI(title="书舟", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(library.router)
app.include_router(library.pages)
app.include_router(reader.router)
app.include_router(settings.router)
app.include_router(knowledge_module.router)


@app.get("/api/health")
def health():
    """健康检查:Docker HEALTHCHECK + 外部探活用。

    不查 DB(watcher/ingest 异常不应让整个服务判死);
    只确认进程存活、能响应 HTTP。
    """
    return {"ok": True}


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request):
    """知识卡片流页面:按时间倒序展示 knowledge_cards + 筛选 + 搜索。"""
    base = str(request.base_url).rstrip("/")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识卡片 — 书舟</title>
<link rel="stylesheet" href="{base}/static/css/index.css">
<style>
.filters {{ display:flex; gap:8px; padding:12px; align-items:center; flex-wrap:wrap; }}
.filters input, .filters select {{ padding:6px 10px; font-size:14px; border:1px solid #ccc; border-radius:4px; }}
.card-stream {{ max-width:700px; margin:0 auto; padding:0 12px 24px; }}
.kc-card {{ background:#fff; border-radius:8px; padding:16px; margin-bottom:12px;
  box-shadow:0 1px 3px rgba(0,0,0,.1); }}
.kc-card .kc-type {{ font-size:12px; color:#888; margin-bottom:4px; }}
.kc-card .kc-title {{ font-size:16px; font-weight:bold; margin-bottom:8px; }}
.kc-card .kc-body {{ font-size:14px; line-height:1.6; color:#333; }}
.kc-card .kc-meta {{ font-size:12px; color:#999; margin-top:8px; }}
.kc-card .kc-recbook {{ font-size:14px; font-weight:bold; color:#1a73e8; margin-bottom:4px; }}
.kc-card .kc-parent {{ display:inline-block; font-size:13px; color:#b0006e;
  background:#fce4ec; padding:2px 8px; border-radius:10px; }}
</style>
</head><body>
<header>
  <h1><a href="/" style="color:inherit;text-decoration:none">书舟</a></h1>
  <nav>
    <a href="/">书库</a>
    <a href="/knowledge" style="font-weight:bold">知识卡片</a>
  </nav>
</header>
<div class="filters">
  <select id="type-filter" onchange="loadCards()">
    <option value="">全部</option>
    <option value="knowledge">知识点</option>
    <option value="blind_spot">盲点</option>
    <option value="recommendation">推荐</option>
  </select>
  <input id="search-input" type="search" placeholder="搜索卡片…" oninput="loadCards()">
</div>
<div class="card-stream" id="card-stream">加载中…</div>
<script>
async function loadCards() {{
  var type = document.getElementById('type-filter').value;
  var q = document.getElementById('search-input').value;
  var params = new URLSearchParams();
  if (type) params.set('card_type', type);
  if (q) params.set('q', q);
  try {{
    var r = await fetch('{base}/api/knowledge/cards?' + params);
    var cards = await r.json();
  }} catch(e) {{ document.getElementById('card-stream').textContent = '加载失败'; return; }}
  if (!cards.length) {{ document.getElementById('card-stream').innerHTML = '<p style="color:#888;text-align:center;padding:40px">暂无卡片</p>'; return; }}
  var labels = {{'knowledge':'知识点','blind_spot':'盲点','recommendation':'推荐'}};
  var html = '';
  for (var c of cards) {{
    var typeLabel = labels[c.card_type] || c.card_type;
    var meta = '';
    if (c.recommend_book) {{
      try {{
        var rb = JSON.parse(c.recommend_book);
        // 显著标注:推进书名 + 关联的盲点/知识点标题
        var bookName = rb.title || '';
        var parentTag = c.parent_title
          ? '<span class="kc-parent">← 推进盲点/知识点：' + c.parent_title + '</span>'
          : '';
        meta = '<div class="kc-recbook">📖 推进书：' + bookName
          + (rb.author ? ' / ' + rb.author : '') + '</div>' + parentTag;
      }} catch(e) {{}}
    }}
    html += '<div class="kc-card">' +
      '<div class="kc-type">' + typeLabel + '</div>' +
      '<div class="kc-title">' + (c.title || '') + '</div>' +
      '<div class="kc-body">' + (c.body || '').slice(0, 300) + '</div>' +
      (meta ? '<div class="kc-meta">' + meta + '</div>' : '') +
      '</div>';
  }}
  document.getElementById('card-stream').innerHTML = html;
}}
loadCards();
</script>
</body></html>"""


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
<header>
  <h1>书舟</h1>
  <nav>
    <a href="/">书库</a>
    <a href="/knowledge">知识卡片</a>
  </nav>
  <span class="hint">把 epub 放入 books-library/ 自动入库</span>
</header>
<main class="grid">{''.join(cards)}</main>
</body></html>"""
