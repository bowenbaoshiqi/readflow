"""测试 reading_log 表 + POST /api/books/{id}/reading-session + epub 文本提取。
"""
from __future__ import annotations

import pytest

from app import db, ingest
from tests.conftest import _first_sample


class TestReadingLogTable:
    """reading_log 表应正确创建并可通过 SQL 写入/查询。"""

    def test_reading_log_table_exists(self):
        """SCHEMA 应包含 reading_log 表。"""
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reading_log'"
            ).fetchone()
        assert row is not None, "reading_log 表应该存在"

    def test_reading_log_insert_and_query(self):
        """应可写入 reading_log 并查询。"""
        with db.db() as conn:
            conn.execute(
                """INSERT INTO books(file_hash, title, original_path)
                   VALUES('test_hash', '测试书', '/tmp/test.epub')"""
            )
            row = conn.execute("SELECT id FROM books WHERE file_hash='test_hash'").fetchone()
            book_id = row["id"]

            conn.execute(
                """INSERT INTO reading_log(book_id, start_cfi, end_cfi, text,
                   percent_from, percent_to)
                   VALUES(?,?,?,?,?,?)""",
                (book_id, "cfi_start", "cfi_end", "这是测试文本", 0.1, 0.3),
            )

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM reading_log WHERE book_id=?", (book_id,)
            ).fetchone()
        assert row is not None
        assert row["text"] == "这是测试文本"
        assert row["percent_from"] == 0.1
        assert row["percent_to"] == 0.3
        assert row["created_at"] is not None


class TestReadingSessionAPI:
    """POST /api/books/{id}/reading-session"""

    def test_reading_session_creates_log(self, client):
        """关书时应创建 reading_log 记录并返回 201。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.post(
            f"/api/books/{bid}/reading-session",
            json={
                "start_cfi": "epubcfi(/6/4!/4/2/4)",
                "end_cfi": "epubcfi(/6/4!/4/16/2)",
                "percent_from": 0.30,
                "percent_to": 0.45,
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["ok"] is True
        assert "id" in data

    def test_reading_session_book_not_found(self, client):
        """不存在的 book_id 返回 404。"""
        r = client.post(
            "/api/books/99999/reading-session",
            json={
                "start_cfi": "cfi",
                "end_cfi": "cfi2",
                "percent_from": 0.0,
                "percent_to": 0.1,
            },
        )
        assert r.status_code == 404

    def test_reading_session_same_cfi_skipped(self, client):
        """start_cfi == end_cfi 时跳过,不创建记录,返回 200。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.post(
            f"/api/books/{bid}/reading-session",
            json={
                "start_cfi": "same_cfi",
                "end_cfi": "same_cfi",
                "percent_from": 0.30,
                "percent_to": 0.30,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data.get("skipped") is True


class TestEpubTextExtraction:
    """epub CFI 区间 → 纯文本提取。"""

    def test_extract_text_between_cfi_in_chapter(self):
        """章节内 CFI 区间应提取纯文本。"""
        from app.epub_text import extract_text

        epub_path = _first_sample()
        text = extract_text(str(epub_path), "epubcfi(/6/4!/4/2/4)", "epubcfi(/6/4!/4/16/2)")
        assert text is not None
        assert len(text.strip()) > 0

    def test_extract_text_across_chapters(self):
        """跨章节 CFI 区间应拼接文本。"""
        from app.epub_text import extract_text

        epub_path = _first_sample()
        text = extract_text(str(epub_path), "epubcfi(/6/4!/4/1)", "epubcfi(/6/10!/4/1)")
        assert text is not None
        assert len(text.strip()) > 0

    def test_extract_text_same_cfi_returns_empty(self):
        """相同 CFI 返回空字符串。"""
        from app.epub_text import extract_text

        epub_path = _first_sample()
        text = extract_text(str(epub_path), "epubcfi(/6/4!/4/2)", "epubcfi(/6/4!/4/2)")
        assert text == ""