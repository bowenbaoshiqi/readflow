"""SQLite 数据库 schema 与连接管理。

v0.1 表设计(采纳 MRD 评审修复点):
- books: 以 file_hash 去重,original_path + epub_path 双路径
- reading_progress: 每书最新位置(spine_index + cfi),轻量,不存页全文
- highlights: 划线用 CFI 锚点(start_cfi/end_cfi)+ 原文,重排版/换设备不错位
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash       TEXT NOT NULL UNIQUE,          -- SHA256 原始文件去重键
    title           TEXT,
    author          TEXT,
    cover_path      TEXT,                          -- 提取出的封面图相对路径
    original_path   TEXT NOT NULL,                 -- 原始文件绝对路径
    epub_path       TEXT,                          -- 渲染用 epub 路径(epub 即原文件则同 original)
    format          TEXT,                          -- epub/mobi/azw3/pdf/txt
    total_chars     INTEGER,                       -- 粗略规模感(替代不成立的"总页数")
    ingest_status   TEXT NOT NULL DEFAULT 'ready', -- ready/failed/unsupported
    ingest_error    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reading_progress (
    book_id     INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    spine_index INTEGER NOT NULL DEFAULT 0,
    cfi         TEXT,                              -- foliate-js 的 CFI 定位
    percent     REAL NOT NULL DEFAULT 0.0,         -- 全书近似进度(仅 UI 显示)
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS highlights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    spine_index INTEGER NOT NULL,
    start_cfi   TEXT NOT NULL,                     -- CFI 锚点,稳定不漂移
    end_cfi     TEXT NOT NULL,
    text        TEXT NOT NULL,                     -- 划线原文(漂移修复回退)
    color       TEXT DEFAULT 'yellow',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_highlights_book ON highlights(book_id);
"""


def init_db() -> None:
    """初始化数据库(幂等)。

    v0.2 新增的外部元数据列用 ALTER TABLE ADD COLUMN 补入(不重建表,
    保留现有数据)。ADD COLUMN 不可重复执行,用 PRAGMA 检查列存在性。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_metadata_columns(conn)


def _migrate_add_metadata_columns(conn) -> None:
    """幂等地为 books 表补外部元数据列(v0.2)。

    每列:存在则跳过,不存在则 ADD。顺序无依赖。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    new_cols = [
        ("summary", "TEXT"),
        ("rating", "REAL"),
        ("rating_count", "INTEGER"),
        ("tags", "TEXT"),              # JSON 数组字符串
        ("publisher", "TEXT"),
        ("publish_date", "TEXT"),
        ("isbn", "TEXT"),
        ("page_count", "INTEGER"),
        ("meta_source", "TEXT"),       # 'google_books' / 'douban' / provider.name
        ("meta_status", "TEXT"),       # NULL/pending/ok/failed/not_found
        ("meta_error", "TEXT"),
        ("meta_fetched_at", "TEXT"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {typ}")


def get_conn() -> sqlite3.Connection:
    """返回启用 WAL + 外键 + busy_timeout 的连接。调用方用 with 管理事务。

    busy_timeout:v0.3 跨设备后,手机存进度与 watcher 入库可能并发写。
    WAL 只让读不阻塞写,并发写仍会撞 database is locked;
    busy_timeout=5s 让写互自动重试,而非立即报错(单用户量级下足够)。
    """
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    """事务上下文:自动 commit / rollback。"""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
