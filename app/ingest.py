"""书籍入库:文件 hash 去重 + ebooklib 元数据/封面提取。

安全注意:ebooklib 2026 年有未修的路径遍历漏洞,只解析用户自己放入书库的文件。
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import ebooklib
from ebooklib import epub

from . import db
from .config import COVER_DIR
from .db import db as db_tx


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
    """用 ebooklib 从 epub 提取书名/作者/封面/规模 + 外部元数据兜底字段。

    v0.2 新增:publisher/publish_date/description/isbn —— 这些是 epub 内嵌的
    本地零成本数据,作为 Google Books 查询不到时的兜底,也作为 ISBN 精确查询的输入。
    ebooklib 读取异常时返回空字段,不阻断入库(降级为可用文件名)。
    """
    meta: dict = {
        "title": None, "author": None, "cover_rel": None, "total_chars": 0,
        "publisher": None, "publish_date": None, "description": None, "isbn": None,
    }
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

    # 出版社/出版日期/简介(epub 内嵌兜底字段)
    publisher = book.get_metadata("DC", "publisher")
    if publisher:
        meta["publisher"] = publisher[0][0]
    date = book.get_metadata("DC", "date")
    if date:
        # date 可能带 attrs/event,取值;格式如 "2020-08-12T00:00:00+00:00" 取日期部分
        raw_date = str(date[0][0])[:10]
        meta["publish_date"] = raw_date or None
    desc = book.get_metadata("DC", "description")
    if desc:
        raw_desc = str(desc[0][0])
        # epub description 常是 HTML,去标签存纯文本
        plain = re.sub(r"<[^>]+>", "", raw_desc).strip()
        meta["description"] = plain or None

    # ISBN:遍历 identifier,scheme 含 isbn 或值以 isbn: 开头
    for ident_val, attrs in book.get_metadata("DC", "identifier") or []:
        scheme = ""
        if attrs:
            scheme = str(attrs.get("{http://www.idpf.org/2007/opf}scheme") or "").lower()
        val = str(ident_val)
        if "isbn" in scheme or val.lower().startswith("isbn:"):
            meta["isbn"] = re.sub(r"^isbn:", "", val, flags=re.I)
            break

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
            content = item.get_content().decode("utf-8", errors="ignore")
            # 去标签粗估
            total += len(re.sub(r"<[^>]+>", "", content))
        except Exception:
            continue
    meta["total_chars"] = total

    return meta


def _save_cover(epub_path: Path, cover_rel: str | None, file_hash: str) -> str | None:
    """从 epub 提取封面图存到 data/covers/{file_hash}.jpg,返回相对路径。

    用 file_hash 命名而非 book_id:book_id 会被复用(删书后自增回退),
    用它命名会在并发入库/重入库时串图(4.jpg/5.jpg bug)。
    file_hash 由内容决定、入库即有、稳定唯一 → 同书重入库封面文件复用,
    并发写也是同一文件(内容相同),不会串到别的书。
    """
    if not cover_rel:
        return None
    try:
        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        item = book.get_item_with_href(cover_rel)
        if item is None:
            return None
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        out = COVER_DIR / f"{file_hash}.jpg"
        out.write_bytes(item.get_content())
        return str(out.relative_to(out.parent.parent.parent))  # data/covers/x.jpg
    except Exception:
        return None


def ingest_file(path: Path) -> int | None:
    """入库单个文件。

    - 以 file_hash 去重:已存在则更新路径/不重建
    - epub 直接解析;其他格式 v0.1 标记 unsupported(转换管线后续版本)
    返回 book_id,None 表示跳过(已存在或异常)。

    并发安全:去重 + 插入合并为单条
    INSERT ... ON CONFLICT(file_hash) DO UPDATE ... RETURNING id,
    消除原 SELECT-then-INSERT 两步之间的 TOCTOU 窗口
    (watchdog 的 flush 线程与 scan 线程可能对同一文件几乎同时触发)。
    """
    path = Path(path).resolve()
    if not path.is_file():
        return None

    fhash = file_hash(path)
    fmt = detect_format(path)

    # v0.1 只处理 epub,其他格式先标记 unsupported
    if fmt != "epub":
        with db_tx() as conn:
            row = conn.execute(
                """INSERT INTO books(file_hash,title,author,original_path,format,
                   ingest_status,ingest_error)
                   VALUES(?,?,?,?,?, 'unsupported','v0.1 暂不支持该格式转换')
                   ON CONFLICT(file_hash) DO UPDATE SET
                     original_path=excluded.original_path,
                     updated_at=datetime('now')
                   RETURNING id""",
                (fhash, path.stem, None, str(path), fmt),
            ).fetchone()
            return row["id"]

    meta = _extract_metadata(path)
    title = meta["title"] or path.stem

    # 原子去重+插入:冲突时只刷新 original_path(处理移动/重命名),不覆盖已有元数据/封面。
    # 返回 id;并附带 xmax 判断是否为本次新建(SQLite 无 xmax,改用先查再定)。
    # v0.2:同时写入 epub 内嵌的 publisher/date/description/isbn,作为外部元数据兜底 + 查询输入。
    with db_tx() as conn:
        row = conn.execute(
            """INSERT INTO books(file_hash,title,author,cover_path,original_path,
               epub_path,format,total_chars,publisher,publish_date,summary,isbn,
               ingest_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ready')
               ON CONFLICT(file_hash) DO UPDATE SET
                 original_path=excluded.original_path,
                 updated_at=datetime('now')
               RETURNING id""",
            (
                fhash,
                title,
                meta["author"],
                None,  # cover 先占位 NULL,下面用 file_hash 命名回填
                str(path),
                str(path),  # epub 原文件即渲染文件
                fmt,
                meta["total_chars"],
                meta["publisher"],
                meta["publish_date"],
                meta["description"],
                meta["isbn"],
            ),
        ).fetchone()
        book_id = row["id"]
        # 冲突命中(已存在)时 cover_path 可能已有值 → 不覆盖;新建时 cover_path 为 NULL → 回填。
        existed = conn.execute(
            "SELECT cover_path FROM books WHERE id=?", (book_id,)
        ).fetchone()

    # 仅新建或原本无封面时回填,避免重复提取/覆盖已存在封面
    if not existed["cover_path"]:
        cover_path = _save_cover(path, meta["cover_rel"], fhash)
        if cover_path:
            with db_tx() as conn:
                conn.execute(
                    "UPDATE books SET cover_path=? WHERE id=?", (cover_path, book_id)
                )

    # 入库后异步补全外部元数据(Google Books)。
    # 失败不影响入库:阅读/书库照常,_enrich_async 内部吞异常。
    _enrich_async(book_id)

    return book_id


def _enrich_async(book_id: int) -> None:
    """后台线程补全外部元数据,不阻塞入库。

    单用户、入库即触发:用 daemon 线程,不引 APScheduler(那是定时任务用的)。
    enrich 内部已对 provider 异常/无结果降级,这里再加一层兜底,
    确保任何异常都不影响入库链路。
    """
    import threading
    from . import metadata

    def _worker():
        try:
            metadata.enrich_book(book_id)
        except Exception:
            # 兜底:enrich 自身已降级,这里只防线程内未捕获异常冒泡
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
