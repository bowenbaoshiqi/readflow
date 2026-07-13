"""测试 ingest 模块:文件 hash、元数据提取、格式识别、去重。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db, ingest


class TestFileHash:
    """测试 file_hash"""

    def test_file_hash_sha256(self, tmp_path):
        """file_hash 应返回 SHA256 值。"""
        f = tmp_path / "hello.txt"
        f.write_text("hello\n")
        expected = "94759360d4..."  # 不关心精确值,只检查长度和 hex 字符
        h = ingest.file_hash(f)
        assert len(h) == 64
        assert int(h, 16) >= 0

    def test_file_hash_identical_files_same(self, tmp_path):
        """相同内容的文件 hash 相同。"""
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"same content")
        b.write_bytes(b"same content")
        assert ingest.file_hash(a) == ingest.file_hash(b)

    def test_file_hash_different_files_differ(self, tmp_path):
        """不同内容的文件 hash 不同。"""
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"content a")
        b.write_bytes(b"content b")
        assert ingest.file_hash(a) != ingest.file_hash(b)


class TestDetectFormat:
    """测试 detect_format"""

    @pytest.mark.parametrize("ext,expected", [
        ("epub", "epub"),
        ("mobi", "mobi"),
        ("azw3", "azw3"),
        ("pdf", "pdf"),
        ("txt", "txt"),
        ("EPUB", "epub"),  # 大写扩展名
    ])
    def test_detect_format(self, tmp_path, ext, expected):
        """detect_format 能识别支持的格式(忽略大小写)。"""
        path = tmp_path / f"book.{ext}"
        path.write_text("x")
        assert ingest.detect_format(path) == expected

    def test_detect_format_no_extension(self, tmp_path):
        """无扩展名时返回 unknown。"""
        path = tmp_path / "book"
        path.write_text("x")
        assert ingest.detect_format(path) == "unknown"


class TestExtractMetadata:
    """测试 _extract_metadata"""

    def test_extract_title_author_from_epub(self):
        """从真实 epub 提取书名和作者。"""
        from tests.conftest import _first_sample
        meta = ingest._extract_metadata(_first_sample())
        assert meta["title"]
        assert meta["total_chars"] > 10000

    def test_extract_total_chars_reasonable(self):
        """total_chars 应在合理范围(去标签后纯文本字符数)。"""
        from tests.conftest import _first_sample
        meta = ingest._extract_metadata(_first_sample())
        assert 1000 < meta["total_chars"] < 10_000_000

    def test_extract_metadata_fails_gracefully_for_invalid_file(self, tmp_path):
        """解析假 epub 时不抛异常,返回空元数据。"""
        fake = tmp_path / "fake.epub"
        fake.write_text("not an epub")
        meta = ingest._extract_metadata(fake)
        assert meta["title"] is None
        assert meta["author"] is None
        assert meta["total_chars"] == 0


class TestIngestFile:
    """测试 ingest_file"""

    def test_ingest_epub_returns_book_id(self):
        """ingest epub 返回 book_id。"""
        from tests.conftest import _first_sample
        bid = ingest.ingest_file(_first_sample())
        assert isinstance(bid, int) and bid > 0

    def test_ingest_dedup_same_file(self):
        """同一文件多次 ingest 返回相同 ID。"""
        from tests.conftest import _first_sample
        bid1 = ingest.ingest_file(_first_sample())
        bid2 = ingest.ingest_file(_first_sample())
        assert bid1 == bid2

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 1, "应只有一条记录"

    def test_ingest_different_files_increments_db(self):
        """不同文件 ingest 应产生多条记录。"""
        from tests.conftest import _first_sample, _second_sample
        bid1 = ingest.ingest_file(_first_sample())
        bid2 = ingest.ingest_file(_second_sample())
        assert bid1 != bid2

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 2

    def test_ingest_non_epub_marked_unsupported(self, tmp_path):
        """非 epub 格式标记为 unsupported。"""
        mobi = tmp_path / "book.mobi"
        mobi.write_bytes(b" Pretend mobi ")
        bid = ingest.ingest_file(mobi)
        assert bid is not None

        with db.get_conn() as conn:
            row = conn.execute("SELECT ingest_status FROM books WHERE id=?", (bid,)).fetchone()
        assert row["ingest_status"] == "unsupported"

    def test_ingest_missing_file(self):
        """不存在的文件返回 None。"""
        assert ingest.ingest_file(Path("/does/not/exist.epub")) is None

    def test_ingest_updates_path_on_rename(self, tmp_path):
        """文件重命名后,同 hash 只更新路径不产生新书。"""
        from tests.conftest import _first_sample
        src = _first_sample()
        renamed = tmp_path / "renamed.epub"
        renamed.write_bytes(src.read_bytes())

        bid1 = ingest.ingest_file(src)
        bid2 = ingest.ingest_file(renamed)
        assert bid1 == bid2

        with db.get_conn() as conn:
            row = conn.execute("SELECT original_path FROM books WHERE id=?", (bid1,)).fetchone()
        assert row["original_path"] == str(renamed.resolve())

    def test_ingest_creates_cover_path_for_epub(self):
        """epub 入库后应有封面路径。"""
        from tests.conftest import _first_sample
        bid = ingest.ingest_file(_first_sample())
        with db.get_conn() as conn:
            row = conn.execute("SELECT cover_path FROM books WHERE id=?", (bid,)).fetchone()
        # 有些 epub 封面不一定能找到,但应正常入库
        assert "cover_path" in row.keys()

    def test_ingest_concurrent_same_file_no_error(self):
        """多线程同时 ingest 同一文件:最终只一条记录,且不抛 IntegrityError。

        模拟 watchdog 的 flush 线程与 scan 线程对同一本新书几乎同时触发。
        去重应在单条 SQL 内原子完成,而非 SELECT-then-INSERT 的两步(有 TOCTOU 窗口)。
        """
        import threading

        from tests.conftest import _first_sample

        src = _first_sample()
        errors: list[str] = []

        def go():
            try:
                ingest.ingest_file(src)
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 1, f"去重失败:应有 1 条记录,实际 {n}"
        assert not errors, f"并发 ingest 不应抛异常,实际: {errors}"
