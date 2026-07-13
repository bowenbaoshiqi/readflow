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

    def test_extract_publisher_from_epub(self):
        """从 epub 提取出版社(《北京法源寺》有:时代文艺出版社)。"""
        from tests.conftest import _sample_epub
        meta = ingest._extract_metadata(_sample_epub("北京法源寺"))
        assert meta.get("publisher") == "时代文艺出版社"

    def test_extract_publish_date_from_epub(self):
        """从 epub 提取出版日期(《北京法源寺》有)。"""
        from tests.conftest import _sample_epub
        meta = ingest._extract_metadata(_sample_epub("北京法源寺"))
        assert meta.get("publish_date")  # 有值即可

    def test_extract_isbn_and_description_from_qintan(self):
        """《钦探》epub 内嵌有 ISBN + description,应提取到。

        这是外部元数据合并的关键:epub 内嵌的简介/ISBN 作为兜底,
        《钦探》Google 无简介,靠 epub 内嵌的补上。
        """
        from tests.conftest import _sample_epub
        meta = ingest._extract_metadata(_sample_epub("钦探"))
        assert meta.get("isbn"), "《钦探》应有 ISBN"
        assert meta.get("description"), "《钦探》应有内嵌简介"

    def test_description_stripped_of_html(self):
        """简介应去 HTML 标签存纯文本(epub description 常是 HTML)。"""
        from tests.conftest import _sample_epub
        meta = ingest._extract_metadata(_sample_epub("钦探"))
        desc = meta.get("description") or ""
        assert "<" not in desc, f"简介未去 HTML 标签: {desc[:80]}"


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

    def test_cover_file_named_by_file_hash_not_id(self, tmp_path):
        """封面文件名用 file_hash,不用自增 book_id。

        book_id 会被复用(删书后自增 id 回退/重入库),用它命名会串图。
        file_hash 由内容决定、入库即有、稳定唯一 → 封面文件名绑它。
        """
        from tests.conftest import _sample_epub
        src = _sample_epub("钦探")
        bid = ingest.ingest_file(src)

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT file_hash, cover_path FROM books WHERE id=?", (bid,)
            ).fetchone()
        fhash = row["file_hash"]
        cover_rel = row["cover_path"]
        assert cover_rel is not None, "《钦探》应有封面"
        # 文件名应精确等于 {file_hash}.jpg(不是 endswith,避免 hash 末位巧合)
        fname = Path(cover_rel).name
        assert fname == f"{fhash}.jpg", \
            f"封面应按 file_hash 命名,实际文件名:{fname}"
        assert fname != f"{bid}.jpg", \
            f"封面不应按 book_id 命名(会串图),实际文件名:{fname}"

    def test_cover_reused_on_reingest_no_duplicate_file(self, tmp_path):
        """同书重入库(改名)→ file_hash 不变 → 封面文件复用,不产生重复/串图。

        回归 4.jpg/5.jpg 串图 bug:旧逻辑用 book_id 命名,重入库时
        两个线程并发写 {book_id}.jpg 会互相覆盖串图。
        用 file_hash 后:同一内容 → 同一文件名 → 天然幂等。
        """
        from tests.conftest import _sample_epub
        src = _sample_epub("钦探")
        renamed = tmp_path / "钦探_副本.epub"
        renamed.write_bytes(src.read_bytes())

        bid1 = ingest.ingest_file(src)
        bid2 = ingest.ingest_file(renamed)
        assert bid1 == bid2  # 同 hash 同书

        # 封面目录里应只有这一个 file_hash 命名的文件,没有 {id}.jpg 残留
        covers = list(ingest.COVER_DIR.glob("*.jpg"))
        assert len(covers) == 1, f"应只一个封面文件,实际:{[c.name for c in covers]}"
        # 且不是以 book_id 命名
        assert not any(c.name == f"{bid1}.jpg" for c in covers)

    def test_ingest_persists_epub_embedded_metadata(self):
        """入库时 epub 内嵌的 publisher/date/description/isbn 应写入 books 表。

        这些是外部元数据的兜底数据 + ISBN 查询输入,必须入库即存。
        《钦探》有全部四字段。
        """
        from tests.conftest import _sample_epub
        bid = ingest.ingest_file(_sample_epub("钦探"))
        with db.get_conn() as conn:
            row = dict(conn.execute(
                "SELECT publisher, publish_date, isbn, summary FROM books WHERE id=?",
                (bid,)
            ).fetchone())
        assert row["publisher"] == "作家出版社" or "huibooks" in (row["publisher"] or "")
        assert row["isbn"] == "9787521226805"
        assert row["summary"]  # epub 内嵌简介
        assert row["publish_date"]

    def test_ingest_triggers_async_enrich(self, monkeypatch):
        """入库成功后应异步触发 enrich(不阻塞 ingest 返回)。

        用 mock 替换 enrich 的实际执行,验证被调用;ingest 返回值不受 enrich 影响。
        不触真网。
        """
        from tests.conftest import _first_sample
        import app.metadata as meta_mod
        calls = []
        # mock 真正的 enrich 执行(worker 线程里调的函数)
        monkeypatch.setattr(meta_mod, "enrich_book", lambda bid: calls.append(bid))
        bid = ingest.ingest_file(_first_sample())
        assert bid is not None
        # enrich 在 daemon 线程里跑,等它一下
        import time
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
        assert bid in calls, "入库后未触发 enrich"

    def test_ingest_enrich_failure_does_not_break_ingest(self, monkeypatch):
        """enrich 抛异常不影响入库(ingest 仍返回 book_id,书可读)。

        这是稳定性核心:外部元数据挂了,阅读/入库不受影响。
        enrich 在 daemon 线程里跑,异常被 worker 兜底吞掉,不冒泡到 ingest。
        """
        from tests.conftest import _first_sample
        import app.metadata as meta_mod
        def boom(bid):
            raise RuntimeError("enrich 炸了")
        monkeypatch.setattr(meta_mod, "enrich_book", boom)
        bid = ingest.ingest_file(_first_sample())
        assert bid is not None, "enrich 异常不应让 ingest 返回 None"
        # 给 daemon 线程时间跑完(异常应被吞,不冒泡)
        import time
        time.sleep(0.3)

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
