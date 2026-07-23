"""阅读器:页面 + epub 文件服务 + 进度 + 划线。

定位模型(采纳 MRD 评审):
- 阅读进度:spine_index + cfi(foliate-js relocate 事件返回的 CFI)
- 划线:start_cfi/end_cfi + 原文,稳定不随排版漂移
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .. import db
from ..epub_text import (
    EpubTextError,
    InvalidSpineRange,
    extract_spine_text,
    extract_text,
)

router = APIRouter(tags=["reader"])
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _require_book(book_id: int, conn) -> None:
    """写入前校验 book 存在,否则 404。

    避免依赖外键约束触发 IntegrityError 冒泡成 500(前端对已删书籍的残留请求常见此路径)。
    """
    row = conn.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "book not found")


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
    html = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{row['title']} - 书舟</title>
<link rel="stylesheet" href="{base}/static/css/reader.css?v=v2.5.1">
<script>
// Kindle(实验性浏览器)不支持 ES module,foliate-js 依赖 module 无法加载。
// Kindle 重定向到 /read-html/{book_id}(服务端渲染 HTML,Kindle 友好);
// 其他设备走 reader.js(原生 module,foliate 作者推荐方式)。
(function() {{
  var ua = navigator.userAgent;
  if (/Kindle|Silk/.test(ua)) {{
    location.replace('{base}/read-html/{book_id}');
    return;
  }}
  var s = document.createElement('script');
  s.type = 'module';
  s.src = '{base}/static/reader.js?v=v2.5.1';
  document.head.appendChild(s);
}})();
</script>
</head>
<body>
<div id="toolbar">
  <button id="back" title="返回">←</button>
  <span id="title">{row['title']}</span>
  <span id="progress">0%</span>
  <button id="toc-btn" title="目录">☰</button>
  <button id="prev-ch" title="上一章">‹</button>
  <button id="next-ch" title="下一章">›</button>
  <button id="typo-btn" title="排版">文</button>
  <button data-act="highlight" id="hl-btn" hidden title="划线">划线</button>
  <button data-act="copy" id="cp-btn" hidden title="复制">复制</button>
</div>
<div id="viewer" data-book-id="{book_id}" data-base="{base}"></div>
<aside id="typo-panel" hidden>
  <div class="typo-head">
    <span class="typo-title">排版</span>
    <button id="typo-close" title="收起">✕</button>
  </div>
  <div class="typo-body">
    <label class="typo-row">
      <span class="typo-label">字号</span>
      <input id="typo-size" type="range" min="12" max="28" step="1" value="16">
      <span class="typo-val" id="typo-size-val">16</span>
      <div id="typo-size-preview">山色空蒙雨亦奇</div>
    </label>
    <label class="typo-row">
      <span class="typo-label">行距</span>
      <input id="typo-spacing" type="range" min="1.2" max="2.8" step="0.1" value="1.6">
      <span class="typo-val" id="typo-spacing-val">1.6</span>
    </label>
    <div class="typo-row">
      <span class="typo-label">边距</span>
      <div class="typo-chips" id="typo-margin-chips">
        <button data-margin="narrow">窄</button>
        <button data-margin="medium">中</button>
        <button data-margin="wide">宽</button>
      </div>
    </div>
    <div class="typo-row">
      <span class="typo-label">字体</span>
      <div class="typo-chips" id="typo-font-chips"></div>
    </div>
  </div>
</aside>

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
</body>
</html>"""
    return html


# ---------- Kindle/旧浏览器:服务端渲染 HTML 阅读 ----------
# foliate-js 依赖 ES module + 现代解压 API(DecompressionStream),Kindle 实验性
# 浏览器都不支持,bundle polyfill 链过深(已验证:Blob.arrayBuffer→DecompressionStream
# 连锁缺失)。故此路径在服务端用 ebooklib 解析 epub,把章节 XHTML 转成静态 HTML,
# 浏览器只看 HTML + 极少普通 JS(翻页/字号)。Kindle 友好,手机也可用。
#
# 翻页:CSS columns 每栏一屏 = 翻页书体验(整页切换,不滚动)。JS 用 px 设
# column-width/height(Kindle 不认 vw/vh)。进度存章节 index(字符级 CFI 在静态 HTML 无意义)。
@router.get("/read-html/{book_id}", response_class=HTMLResponse)
def reader_html_page(book_id: int, request: Request, ch: int = 0):
    """Kindle 友好的 HTML 阅读页:columns 分栏翻页 + 字号 + 霞鹜文楷字体。

    ch:spine 章节序号(0 起)。无内容章节(空白扉页)自动跳过到下一个。
    """
    import re
    import ebooklib
    from ebooklib import epub

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT original_path, epub_path, title FROM books WHERE id=? AND ingest_status='ready'",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not available")
    epub_path = Path(row["original_path"])
    if not epub_path.is_file() and row["epub_path"]:
        epub_path = Path(row["epub_path"])
    if not epub_path.is_file():
        raise HTTPException(404, "file missing on disk")

    try:
        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    except Exception as e:
        raise HTTPException(500, f"epub 解析失败: {e}")

    # 预扫描 spine:为每个章节算出"纯文本长度",用于跳过无内容扉页 + 翻页时定位。
    # 章节正文提取:body inner → 去 script/style → 保留段落标签。
    def _chapter_body(item):
        content = item.get_content().decode("utf-8", errors="ignore")
        body = re.search(r"<body[^>]*>(.*?)</body>", content, re.S)
        inner = body.group(1) if body else content
        inner = re.sub(r"<script[^>]*>.*?</script>", "", inner, flags=re.S)
        inner = re.sub(r"<style[^>]*>.*?</style>", "", inner, flags=re.S)
        return inner

    chapters = []  # [(spine_idx, item_id, name, body_html, text_len)]
    for sidx, (item_id, _linear) in enumerate(book.spine):
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        inner = _chapter_body(item)
        text_len = len(re.sub(r"<[^>]+>", "", inner).strip())
        chapters.append((sidx, item_id, item.get_name(), inner, text_len))

    if not chapters:
        raise HTTPException(500, "epub 无可读章节")

    # 有内容(非空白)的章节序号列表,用于翻页跳过扉页
    substantial = [i for i, c in enumerate(chapters) if c[4] > 20]
    if not substantial:
        substantial = [0]

    # 定位当前章节:从 ch 开始找第一个有内容的
    cur = next((i for i in substantial if i >= ch), substantial[0])
    # 上下章(在有内容章节间跳)
    cur_pos = substantial.index(cur)
    prev_ch = chapters[substantial[cur_pos - 1]][0] if cur_pos > 0 else None
    next_ch = chapters[substantial[cur_pos + 1]][0] if cur_pos < len(substantial) - 1 else None

    _, _, chapter_name, chapter_html, text_len = chapters[cur]

    base = str(request.base_url).rstrip("/")
    title = row["title"] or "无标题"
    progress_pct = int((cur_pos + 1) / len(substantial) * 100)

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - 书舟</title>
<script>
// 字号恢复:在 head 最早执行,渲染前应用,避免"先用默认字号渲染再切换"的视觉跳变。
try {{
  var s = localStorage.getItem('readflow:html-fs');
  if (s) document.documentElement.style.setProperty('--fs', s + 'px');
}} catch(e) {{}}
</script>
<style>
@font-face {{
  font-family: 'LXGW';
  src: url('{base}/static/fonts/lxgw.ttf') format('truetype');
  font-weight: normal; font-style: normal;
}}
:root {{ --fs: 18px; --lh: 1.8; --mg: 16px; }}
* {{ box-sizing: border-box; }}
html,body {{ margin:0; padding:0; height:100%; overflow:hidden; background:#fff; color:#222; font-family:'LXGW', serif; }}
/* 阅读区:CSS columns 分栏,每栏一屏 = 翻页书。JS 用 px 设 column-width(Kindle 不认 vw)。
   关键:content 自身无 padding(否则 padding 撑大总宽,最后一栏溢出视口右侧漏字)。
   文字边距用内层 .text 的 padding,在栏内,不溢出。 */
#reader {{ height: 100vh; overflow: hidden; }}
#content {{
  font-size: var(--fs); line-height: var(--lh);
  font-family: 'LXGW', serif;
  height: 100vh; overflow: hidden;
  column-gap: 0; column-fill: auto;
  /* column-width 由 JS 设 px = innerWidth */
}}
#content .text {{ padding: 8px var(--mg) 8px var(--mg); }}
#content p {{ margin: 0 0 1em 0; text-indent: 2em; break-inside: avoid; }}
#content h1,#content h2,#content h3 {{ line-height: 1.4; break-inside: avoid; }}
/* 顶部工具栏:浮动,默认隐藏,点顶部中间区域唤出 */
#bar {{
  position: fixed; top:0; left:0; right:0; height:48px;
  background:#f0f0f0; border-bottom:1px solid #ccc;
  align-items:center; font-size:14px; padding:0 12px; gap:8px;
  z-index:20; display:none;
}}
#bar.show {{ display:flex; }}
#bar button {{ font-size:16px; padding:4px 10px; }}
#bar a {{ text-decoration:none; }}
/* 翻页点击层:左 40% 上一页,右 60% 下一页,顶部中间条唤工具栏 */
#tap-left {{ position:fixed; top:0; left:0; width:40vw; height:100vh; z-index:6; }}
#tap-right {{ position:fixed; top:0; right:0; width:60vw; height:100vh; z-index:7; }}
#tap-mid {{ position:fixed; top:0; left:35vw; width:30vw; height:10vh; z-index:8; }}
</style>
</head>
<body>
<div id="bar">
  <a href="/">←书库</a>
  <span style="flex:1">{title}</span>
  <span style="color:#666" id="pct">{progress_pct}%</span>
  <button onclick="adj(-1)">A−</button>
  <button onclick="adj(1)">A+</button>
  <a href="{'/read-html/' + str(book_id) + '?ch=' + str(prev_ch) if prev_ch is not None else '#'}" id="prev-ch-link" style="margin-left:8px{';color:#999' if prev_ch is None else ''}">上一章</a>
  <a href="{'/read-html/' + str(book_id) + '?ch=' + str(next_ch) if next_ch is not None else '#'}" id="next-ch-link"{' style="color:#999"' if next_ch is None else ''}>下一章</a>
</div>
<div id="reader"><div id="content"><div class="text">{chapter_html}</div></div></div>
<div id="tap-left"></div>
<div id="tap-right"></div>
<div id="tap-mid"></div>
<script>
// 普通脚本(非 module),Kindle 能跑。
// 翻页书:CSS columns 每栏一屏,JS 用 px 设 column-width(Kindle 不认 vw),translateX 翻页。
var reader = document.getElementById('reader');
var content = document.getElementById('content');
var page = 0;
// 用 px 设 column-width + height,避开 Kindle 对 vw/vh 的不识别(100vh 可能含地址栏,底部空)
content.style.columnWidth = window.innerWidth + 'px';
content.style.height = window.innerHeight + 'px';
function pageCount() {{
  return Math.max(1, Math.round(content.scrollWidth / window.innerWidth));
}}
function show() {{
  content.style.transform = 'translateX(' + (-page * window.innerWidth) + 'px)';
}}
function next() {{
  if (page < pageCount() - 1) {{ page++; show(); }}
  else {{ var nl = document.getElementById('next-ch-link'); if (nl && nl.getAttribute('href') !== '#') location.href = nl.href; }}
}}
function prev() {{
  if (page > 0) {{ page--; show(); }}
  else {{ var pl = document.getElementById('prev-ch-link'); if (pl && pl.getAttribute('href') !== '#') location.href = pl.href; }}
}}
document.getElementById('tap-left').addEventListener('click', prev);
document.getElementById('tap-right').addEventListener('click', next);
document.getElementById('tap-mid').addEventListener('click', function(e) {{
  e.stopPropagation();
  var bar = document.getElementById('bar');
  bar.className = bar.className === 'show' ? '' : 'show';
}});
// 字号调整:改 CSS 变量后 columns 重排,等下一帧再算页数回首页
function adj(d) {{
  var root = document.documentElement.style;
  var cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--fs')) || 18;
  var n = Math.max(12, Math.min(32, cur + d));
  root.setProperty('--fs', n + 'px');
  try {{ localStorage.setItem('readflow:html-fs', n); }} catch(e) {{}}
  page = 0;
  requestAnimationFrame(function() {{ requestAnimationFrame(show); }});
}}
window.addEventListener('load', function() {{ setTimeout(show, 300); }});
</script>
</body>
</html>"""


# ---------- epub 文件服务 ----------
@router.get("/api/books/{book_id}/file")
def get_epub_file(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT epub_path, original_path, title FROM books WHERE id=? AND ingest_status='ready'",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not available")
    # 用 original_path 定位:ingest 的 ON CONFLICT 会刷新 original_path 到当前环境路径
    # (容器 /books-library/... / 宿主 /Users/.../books-library/...),
    # 而 epub_path 不在 UPDATE 列表里,跨环境重入库会停留在旧环境的绝对路径 → 文件找不到。
    # epub 即原文件时两者本应相同;未来格式转换(epub_path≠original)再单独处理渲染文件定位。
    p = Path(row["original_path"])
    if not p.is_file() and row["epub_path"]:
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
        _require_book(book_id, conn)
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
        _require_book(book_id, conn)
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


# ---------- 阅读会话日志 ----------
class ReadingSessionIn(BaseModel):
    session_id: UUID | None = None
    segment_no: int | None = Field(default=None, ge=1)
    start_cfi: str
    end_cfi: str
    start_spine_index: int | None = Field(default=None, ge=0)
    end_spine_index: int | None = Field(default=None, ge=0)
    percent_from: float
    percent_to: float


def _find_reading_segment(session_id: str | None, segment_no: int | None):
    if session_id is None or segment_no is None:
        return None
    with db.get_conn() as conn:
        return conn.execute(
            """SELECT id FROM reading_log
               WHERE session_id=? AND segment_no=?""",
            (session_id, segment_no),
        ).fetchone()


@router.post("/api/books/{book_id}/reading-session")
def create_reading_session(book_id: int, body: ReadingSessionIn):
    """幂等记录一段阅读区间，并从 EPUB 提取对应章节正文。"""
    if body.start_cfi == body.end_cfi:
        return {
            "ok": True,
            "status": "skipped",
            "skipped": True,
            "reason": "no movement",
        }

    session_id = str(body.session_id) if body.session_id is not None else None
    existing = _find_reading_segment(session_id, body.segment_no)
    if existing:
        return {"ok": True, "status": "duplicate", "id": existing["id"]}

    has_start_index = body.start_spine_index is not None
    has_end_index = body.end_spine_index is not None
    if has_start_index != has_end_index:
        raise HTTPException(422, "both spine indices are required")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT original_path, epub_path FROM books WHERE id=? AND ingest_status='ready'",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")

    epub_path = Path(row["original_path"])
    if not epub_path.is_file() and row["epub_path"]:
        epub_path = Path(row["epub_path"])
    if not epub_path.is_file():
        raise HTTPException(404, "file missing on disk")

    try:
        if has_start_index:
            text = extract_spine_text(
                str(epub_path),
                body.start_spine_index,
                body.end_spine_index,
            )
        else:
            text = extract_text(str(epub_path), body.start_cfi, body.end_cfi)
    except InvalidSpineRange as exc:
        raise HTTPException(422, str(exc)) from exc
    except EpubTextError as exc:
        logger.exception("reading session EPUB extraction failed")
        raise HTTPException(500, "EPUB text extraction failed") from exc

    if not text.strip():
        return {
            "ok": True,
            "status": "skipped",
            "skipped": True,
            "reason": "empty text",
        }

    try:
        with db.db() as conn:
            cur = conn.execute(
                """INSERT INTO reading_log(
                       book_id, start_cfi, end_cfi, text,
                       percent_from, percent_to, session_id, segment_no,
                       start_spine_index, end_spine_index
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    book_id,
                    body.start_cfi,
                    body.end_cfi,
                    text,
                    body.percent_from,
                    body.percent_to,
                    session_id,
                    body.segment_no,
                    body.start_spine_index,
                    body.end_spine_index,
                ),
            )
            log_id = cur.lastrowid
    except sqlite3.IntegrityError:
        existing = _find_reading_segment(session_id, body.segment_no)
        if existing:
            return {"ok": True, "status": "duplicate", "id": existing["id"]}
        raise
    return {"ok": True, "status": "created", "id": log_id}
