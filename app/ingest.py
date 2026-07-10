"""书籍入库:文件 hash 去重 + ebooklib 元数据/封面提取。

安全注意:ebooklib 2026 年有未修的路径遍历漏洞,只解析用户自己放入书库的文件。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import ebooklib
from ebooklib import epub

from . import db
from .db import db as db_tx

COVER_DIR = Path(__file__).resolve().parent.parent / "data" / "covers"


def file_hash(path: Path, buf_size: int = 1 << 20) -> str:
    """计算文件 SHA256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf_size):
            h.update(chunk)
    return h.hexdigest()


def detect_format(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    # azw3/mobi/pdf/txt 留待后续版本转换管线,v0.1 直接返回扩展名
    return ext or "unknown"


def _extract_metadata(epub_path: Path) -> dict:
    """用 ebooklib 从 epub 提取书名/作者/封面/规模。

    ebooklib 读取异常时返回空字段,不阻断入库(降级为可用文件名)。
    """
    meta: dict = {"title": None, "author": None, "cover_rel": None, "total_chars": 0}
    try:
        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    except Exception:
        # 解析失败不抛,上层用文件名兜底
        return meta

    # 书名/作者
    title = book.get_metadata("DC", "title")
    if title:
        meta["title"] = title[0][0]
    author = book.get_metadata("DC", "creator")
    if author:
        meta["author"] = author[0][0]

    # 封面:ebooklib 的 get_item_with_id('cover') 不稳,遍历 images 找 cover-xhtml-image
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        meta["cover_rel"] = item.get_name()
        break
    if not meta["cover_rel"]:
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            name = (item.get_name() or "").lower()
            if "cover" in name:
                meta["cover_rel"] = item.get_name()
                break

    # 粗略规模:章节纯文本字符数(替代不成立的"总页数")
    total = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        try:
            from ebooklib.utils import debug
            content = item.get_content().decode("utf-8", errors="ignore")
            # 去标签粗估
            import re
            total += len(re.sub(r"<[^>]+>", "", content))
        except Exception:
            continue
    meta["total_chars"] = total

    return meta


def _save_cover(epub_path: Path, cover_rel: str | None, book_id: int) -> str | None:
    """从 epub 提取封面图存到 data/covers/{book_id}.jpg,返回相对路径。"""
    if not cover_rel:
        return None
    try:
        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        item = book.get_item_with_href(cover_rel)
        if item is None:
            return None
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        out = COVER_DIR / f"{book_id}.jpg"
        out.write_bytes(item.get_content())
        return str(out.relative_to(out.parent.parent.parent))  # data/covers/x.jpg
    except Exception:
        return None


def ingest_file(path: Path) -> int | None:
    """入库单个文件。

    - 以 file_hash 去重:已存在则更新路径/不重建
    - epub 直接解析;其他格式 v0.1 标记 unsupported(转换管线后续版本)
    返回 book_id,None 表示跳过(已存在或异常)。
    """
    path = Path(path).resolve()
    if not path.is_file():
        return None

    fhash = file_hash(path)
    fmt = detect_format(path)

    # 去重:已入库同 hash → 仅更新 original_path(处理移动/重命名),不重复入库
    with db_tx() as conn:
        row = conn.execute(
            "SELECT id FROM books WHERE file_hash=?", (fhash,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE books SET original_path=?, updated_at=datetime('now') WHERE id=?",
                (str(path), row["id"]),
            )
            return row["id"]

    # v0.1 只处理 epub,其他格式先标记 unsupported
    if fmt != "epub":
        with db_tx() as conn:
            cur = conn.execute(
                """INSERT INTO books(file_hash,title,author,original_path,format,
                   ingest_status,ingest_error)
                   VALUES(?,?,?,?,?, 'unsupported','v0.1 暂不支持该格式转换')""",
                (fhash, path.stem, None, str(path), fmt),
            )
            return cur.lastrowid

    meta = _extract_metadata(path)
    title = meta["title"] or path.stem

    with db_tx() as conn:
        cur = conn.execute(
            """INSERT INTO books(file_hash,title,author,cover_path,original_path,
               epub_path,format,total_chars,ingest_status)
               VALUES(?,?,?,?,?,?,?,?, 'ready')""",
            (
                fhash,
                title,
                meta["author"],
                None,  # cover 先占位,用 book_id 命名,下面回填
                str(path),
                str(path),  # epub 原文件即渲染文件
                fmt,
                meta["total_chars"],
            ),
        )
        book_id = cur.lastrowid

    cover_path = _save_cover(path, meta["cover_rel"], book_id)
    if cover_path:
        with db_tx() as conn:
            conn.execute(
                "UPDATE books SET cover_path=? WHERE id=?", (cover_path, book_id)
            )

    return book_id
