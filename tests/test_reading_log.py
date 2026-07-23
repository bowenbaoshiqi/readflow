"""测试 reading_log 表 + POST /api/books/{id}/reading-session + epub 文本提取。
"""
from __future__ import annotations

import re

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

    def test_reading_log_session_columns_and_unique_index(self):
        with db.get_conn() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(reading_log)")
            }
            indexes = {
                row["name"] for row in conn.execute("PRAGMA index_list(reading_log)")
            }
        assert {
            "session_id",
            "segment_no",
            "start_spine_index",
            "end_spine_index",
        } <= columns
        assert "idx_reading_log_session_segment" in indexes

    def test_reading_log_migration_is_idempotent(self):
        db.init_db()
        db.init_db()
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('reading_log') "
                "WHERE name IN ('session_id','segment_no',"
                "'start_spine_index','end_spine_index')"
            ).fetchone()[0]
        assert count == 4


class TestReadingSessionAPI:
    """POST /api/books/{id}/reading-session"""

    def test_reading_session_creates_log(self, client):
        """关书时应创建 reading_log 记录并返回 200。"""
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
        assert r.status_code == 200
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

    def test_reading_session_is_idempotent(self, client):
        bid = ingest.ingest_file(_first_sample())
        index = _readable_spine_indices(_first_sample())[0]
        payload = _segment_payload(
            start_spine_index=index,
            end_spine_index=index,
        )
        first = client.post(f"/api/books/{bid}/reading-session", json=payload)
        second = client.post(f"/api/books/{bid}/reading-session", json=payload)
        assert first.status_code == 200
        assert first.json()["status"] == "created"
        assert second.json() == {
            "ok": True,
            "status": "duplicate",
            "id": first.json()["id"],
        }
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM reading_log "
                "WHERE session_id=? AND segment_no=?",
                (payload["session_id"], payload["segment_no"]),
            ).fetchone()[0]
        assert count == 1

    def test_reading_session_rejects_invalid_spine_range(self, client):
        bid = ingest.ingest_file(_first_sample())
        response = client.post(
            f"/api/books/{bid}/reading-session",
            json=_segment_payload(start_spine_index=-1),
        )
        assert response.status_code == 422

    def test_reading_session_accepts_backward_spine_range(self, client):
        bid = ingest.ingest_file(_first_sample())
        indices = _readable_spine_indices(_first_sample())
        response = client.post(
            f"/api/books/{bid}/reading-session",
            json=_segment_payload(
                start_spine_index=indices[1],
                end_spine_index=indices[0],
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "created"

    def test_legacy_reading_session_request_remains_supported(self, client):
        bid = ingest.ingest_file(_first_sample())
        response = client.post(
            f"/api/books/{bid}/reading-session",
            json={
                "start_cfi": "epubcfi(/6/4!/4/2/4)",
                "end_cfi": "epubcfi(/6/4!/4/16/2)",
                "percent_from": 0.30,
                "percent_to": 0.45,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] in {"created", "skipped"}


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

    def test_extract_text_from_explicit_spine_index(self):
        from app.epub_text import extract_spine_text

        path = _first_sample()
        index = _readable_spine_indices(path)[0]
        text = extract_spine_text(str(path), index, index)
        assert text.strip()

    def test_extract_text_across_explicit_spine_range(self):
        from app.epub_text import extract_spine_text

        path = _first_sample()
        indices = _readable_spine_indices(path)
        start, end = indices[0], indices[1]
        text = extract_spine_text(str(path), start, end)
        first = extract_spine_text(str(path), start, start)
        second = extract_spine_text(str(path), end, end)
        assert first in text
        assert second in text

    def test_extract_text_rejects_bad_spine_range(self):
        from app.epub_text import InvalidSpineRange, extract_spine_text

        with pytest.raises(InvalidSpineRange):
            extract_spine_text(str(_first_sample()), -1, 0)


def _segment_payload(**overrides):
    payload = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "segment_no": 1,
        "start_cfi": "epubcfi(/6/2!/4)",
        "end_cfi": "epubcfi(/6/4!/4)",
        "start_spine_index": 0,
        "end_spine_index": 0,
        "percent_from": 0.01,
        "percent_to": 0.02,
    }
    payload.update(overrides)
    return payload


def _readable_spine_indices(path):
    from ebooklib import epub

    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    indices = []
    for index, (item_id, linear) in enumerate(book.spine):
        item = book.get_item_with_id(item_id)
        if linear == "no" or item is None:
            continue
        text = re.sub(
            rb"<[^>]+>",
            b"",
            item.get_content(),
        ).strip()
        if text:
            indices.append(index)
    return indices
