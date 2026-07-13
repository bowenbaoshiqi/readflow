"""书库 API:列表/详情/封面 + 书籍详情页。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import db, ingest

router = APIRouter(prefix="/api/library", tags=["library"])
pages = APIRouter(tags=["pages"])  # HTML 页面路由(无 /api 前缀)


@router.get("")
def list_books():
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT b.id, b.title, b.author, b.format, b.ingest_status,
                      b.total_chars, b.cover_path, b.rating, b.meta_status,
                      COALESCE(p.percent, 0) AS progress
               FROM books b
               LEFT JOIN reading_progress p ON p.book_id = b.id
               ORDER BY b.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{book_id}")
def get_book(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT b.*, COALESCE(p.percent, 0) AS progress,
                      COALESCE(p.spine_index, 0) AS spine_index,
                      p.cfi
               FROM books b
               LEFT JOIN reading_progress p ON p.book_id = b.id
               WHERE b.id = ?""",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")
    return dict(row)


@router.get("/{book_id}/cover")
def get_cover(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT file_hash, cover_path FROM books WHERE id=?", (book_id,)
        ).fetchone()
    if not row or not row["cover_path"]:
        raise HTTPException(404, "no cover")
    # 封面文件名含该书的 file_hash(防 4.jpg/5.jpg 式串图)。
    # 迁移前的旧 {book_id}.jpg 命名会在此被挡,强制走迁移重建。
    if row["file_hash"] and row["file_hash"] not in row["cover_path"]:
        raise HTTPException(404, "cover naming stale, needs re-migrate")
    # 用 ingest.COVER_DIR 定位(与入库时一致;测试 monkeypatch 的是 ingest.COVER_DIR)。
    cover_file = ingest.COVER_DIR / f'{row["file_hash"]}.jpg'
    if not cover_file.is_file():
        raise HTTPException(404, "cover file missing")
    return FileResponse(str(cover_file))


@router.delete("/{book_id}")
def delete_book(book_id: int):
    """删书(方案 C):删 DB 记录 + 原文件移到 .trash/ + 删封面。

    - 原文件移到 {书库根}/.trash/{file_hash}_{原名}:file_hash 前缀防同名撞覆盖,
      原名保留可读性。watcher 全局排除 .trash/ 防重新扫描入库。
    - 封面 {file_hash}.jpg 一并删:封面从 epub 提取,书都没了封面成孤儿;
      file_hash 命名保证不误删别的书的封面。
    - DB 记录删除,reading_progress/highlights 靠 ON DELETE CASCADE 自动清。
    - 原文件不存在(已移走/磁盘丢失)时:仍删 DB 记录,不因文件缺失阻断删除。
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT original_path, file_hash, cover_path FROM books WHERE id=?",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")

    original = Path(row["original_path"]) if row["original_path"] else None
    fhash = row["file_hash"]

    # 1. 原文件移到 .trash/
    if original and original.is_file():
        trash_dir = _library_trash_dir()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / f"{fhash}_{original.name}"
        # 极端情况:同 hash 同名已存在(理论上 file_hash 唯一不会撞),跳过覆盖
        if not dest.exists():
            shutil.move(str(original), str(dest))

    # 2. 删封面文件(用 ingest.COVER_DIR 定位,与入库时一致;
    #    不用 DATA_ROOT —— 测试时 COVER_DIR 被 monkeypatch 到 tmp)
    if row["cover_path"]:
        cover_file = ingest.COVER_DIR / f"{fhash}.jpg"
        if cover_file.is_file():
            try:
                cover_file.unlink()
            except OSError:
                pass  # 封面删失败不阻断删书

    # 3. 删 DB 记录(CASCADE 清 progress/highlights)
    with db.db() as conn:
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    return {"ok": True}


def _library_trash_dir() -> Path:
    """书库根下的 .trash/ 目录。

    延迟 import main 避免循环 import(main 依赖 routes.library)。
    测试时 main.LIBRARY_DIR 被 monkeypatch 成 tmp 目录。
    """
    from .. import main
    return Path(main.LIBRARY_DIR) / ".trash"


@pages.get("/book/{book_id}", response_class=HTMLResponse)
def book_detail_page(book_id: int, request: Request):
    """书籍详情页:封面 + 元数据 + 简介 + 开始阅读。

    外部元数据字段可能为 null(enrich 未完成或 not_found),模板对空值优雅降级。
    """
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT b.*, COALESCE(p.percent, 0) AS progress
               FROM books b
               LEFT JOIN reading_progress p ON p.book_id = b.id
               WHERE b.id = ?""",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")
    b = dict(row)
    base = str(request.base_url).rstrip("/")

    # 解析 tags(JSON 字符串 → list)
    tags = []
    if b.get("tags"):
        try:
            tags = json.loads(b["tags"])
        except (json.JSONDecodeError, TypeError):
            tags = []

    # 封面
    cover = (
        f'<img src="{base}/api/library/{b["id"]}/cover" alt="封面">'
        if b.get("cover_path") else
        '<div class="cover-placeholder">无封面</div>'
    )

    # 评分(Google 1-5 分,显示一位小数;无评分不显示)
    rating_html = ""
    if b.get("rating") is not None:
        rating_html = (
            f'<span class="rating">★ {float(b["rating"]):.1f}</span>'
            f'<span class="rating-count">{b.get("rating_count") or 0} 人评分</span>'
        )

    # 信息行(只显示有值的)
    info_rows = []
    if b.get("author"):
        info_rows.append(("作者", b["author"]))
    if b.get("publisher"):
        info_rows.append(("出版社", b["publisher"]))
    if b.get("publish_date"):
        info_rows.append(("出版日期", b["publish_date"]))
    if b.get("isbn"):
        info_rows.append(("ISBN", b["isbn"]))
    if b.get("page_count"):
        info_rows.append(("页数", f'{b["page_count"]} 页'))
    if b.get("language"):
        info_rows.append(("语言", _lang_name(b["language"])))
    info_html = "".join(
        f'<div class="info-row"><span class="info-label">{lbl}</span>'
        f'<span class="info-value">{val}</span></div>'
        for lbl, val in info_rows
    )

    # 标签
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in tags) if tags else ""

    # 简介
    summary_html = ""
    if b.get("summary"):
        summary_html = f'<section class="summary"><h2>简介</h2><p>{_esc(b["summary"])}</p></section>'

    # 元数据状态提示(enrich 未完成或失败时)
    meta_hint = ""
    ms = b.get("meta_status")
    if ms == "pending" or ms is None:
        meta_hint = '<div class="meta-hint loading">正在补全书目信息…</div>'
    elif ms == "failed":
        meta_hint = '<div class="meta-hint failed">书目信息补全失败</div>'
    elif ms == "not_found":
        meta_hint = '<div class="meta-hint">未找到外部书目信息</div>'

    prog = int((b["progress"] or 0) * 100)
    progress_html = (
        f'<div class="progress-bar"><div style="width:{prog}%"></div></div>'
        f'<span class="progress-text">已读 {prog}%</span>'
        if prog > 0 else
        '<span class="progress-text">未开始</span>'
    )

    title = _esc(b.get("title") or "无标题")

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — 书舟</title>
<link rel="stylesheet" href="{base}/static/css/book.css">
</head><body>
<a class="back" href="/">← 返回书库</a>
<main class="detail">
  <div class="cover-side">{cover}</div>
  <div class="info-side">
    <h1 class="book-title">{title}</h1>
    <div class="rating-row">{rating_html}</div>
    <div class="info-table">{info_html}</div>
    <div class="tags">{tags_html}</div>
    {meta_hint}
    <div class="progress-row">{progress_html}</div>
    <a class="read-btn" href="/read/{b['id']}">{'继续阅读' if prog > 0 else '开始阅读'}</a>
  </div>
</main>
{summary_html}
</body></html>"""


def _esc(s: str | None) -> str:
    """HTML 转义。"""
    if not s:
        return ""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _lang_name(code: str | None) -> str:
    """语言代码 → 中文名。"""
    if not code:
        return ""
    m = {"zh": "中文", "zh-cn": "中文", "zho": "中文", "en": "英语",
         "eng": "英语", "ja": "日语", "jpn": "日语"}
    return m.get(code.lower(), code)
