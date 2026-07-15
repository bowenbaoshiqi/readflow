"""测试 watcher 模块:定时扫描 + 事件去抖 + 幂等。

注意:真实 watchdog inotify 事件测试在不同 OS 上行为差异大,本文件主要测试
LibraryHandler 和 LibraryWatcher 的 flush/scan 逻辑,不大量依赖真实文件系统事件。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import ingest, watcher
from tests.conftest import _first_sample, _second_sample


class TestLibraryHandler:
    """LibraryHandler 事件缓冲"""

    def test_schedule_ignores_non_epub(self, tmp_path):
        """非 epub 文件不进入待处理队列。"""
        h = watcher.LibraryHandler(tmp_path)
        txt = tmp_path / "a.txt"
        txt.write_text("hello")
        h._schedule(str(txt))
        assert len(h._pending) == 0, "txt 不应进入待处理"

    def test_schedule_includes_epub(self, tmp_path):
        """epub 文件进入待处理队列。"""
        h = watcher.LibraryHandler(tmp_path)
        epub = tmp_path / "a.epub"
        epub.write_text("x")
        h._schedule(str(epub))
        assert str(epub) in h._pending

    def test_flush_stable_after_delay(self, tmp_path):
        """文件稳定 STABLE_SECONDS 后 flush 会入库。"""
        h = watcher.LibraryHandler(tmp_path)
        src = _first_sample()
        epub = tmp_path / "b.epub"
        epub.write_bytes(src.read_bytes())
        h._schedule(str(epub))
        time.sleep(0.1)
        # 没稳定前不应入库
        ids = h.flush_stable()
        assert ids == []

    def test_flush_not_ready_before_stable(self, tmp_path):
        """未稳定时 flush 返回空列表。"""
        h = watcher.LibraryHandler(tmp_path)
        epub = tmp_path / "c.epub"
        epub.write_text("x")
        h._schedule(str(epub))
        time.sleep(0.01)
        ids = h.flush_stable()
        assert ids == []


class TestLibraryWatcher:
    """LibraryWatcher 整体逻辑"""

    def test_watcher_start_stop(self, tmp_path):
        """watcher 能启动和停止。"""
        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        w.start()
        time.sleep(0.1)
        w.stop()
        assert True, "start/stop 不应抛异常"

    def test_scan_once_imports_existing_books(self, tmp_path):
        """启动时全量扫描应把已存在的 epub 入库。"""
        src = _first_sample()
        epub = tmp_path / "existing.epub"
        epub.write_bytes(src.read_bytes())

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        w._scan_once()

        from app import db
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 1

    def test_scan_once_is_idempotent(self, tmp_path):
        """多次全量扫描不重复入库。"""
        src = _first_sample()
        epub = tmp_path / "dup.epub"
        epub.write_bytes(src.read_bytes())

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        w._scan_once()
        w._scan_once()

        from app import db
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 1

    def test_scan_once_imports_multiple_books(self, tmp_path):
        """一次扫描导入多本 epub。"""
        e1 = tmp_path / "a.epub"
        e2 = tmp_path / "b.epub"
        e1.write_bytes(_first_sample().read_bytes())
        e2.write_bytes(_second_sample().read_bytes())

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        w._scan_once()

        from app import db
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 2


class TestWatcherModuleLevelState:
    """watcher 模块级状态"""

    def test_start_watcher_creates_singleton(self, monkeypatch, tmp_path):
        """start_watcher 返回单例,重复调用返回同一对象。"""
        # 防止真实启动
        import app.watcher as wmod
        monkeypatch.setattr(wmod, "_watcher", None)
        called = []

        class FakeW:
            def start(self): called.append("start")
            def stop(self): pass

        monkeypatch.setattr(wmod, "LibraryWatcher", lambda d, **kw: FakeW())
        a = wmod.start_watcher(tmp_path)
        b = wmod.start_watcher(tmp_path)
        assert a is b

    def test_get_watcher_initially_none(self):
        """未启动时 get_watcher 返回 None。"""
        import app.watcher as wmod
        # 注意此测试可能受其他测试影响,用 monkeypatch 重置更稳
        wmod._watcher = None
        assert wmod.get_watcher() is None


class TestLibraryHandlerDeletion:
    """文件系统删书 → 同步清库。

    on_deleted:watchdog deleted 事件 → 按 original_path 反查 book_id → remove_book。
    _purge_missing:兜底反向同步(DB 有、磁盘无 → 清库),覆盖 inotify 漏触发。
    沿用本文件约定:不依赖真实 inotify,直接构造事件喂 handler / 直调方法。
    """

    def test_on_deleted_clears_db_by_path(self, tmp_path):
        """删文件事件 → 按 path 反查 → DB 无该行。"""
        from watchdog.events import FileDeletedEvent
        from app import db

        h = watcher.LibraryHandler(tmp_path)
        src = _first_sample()
        epub = tmp_path / "gone.epub"
        epub.write_bytes(src.read_bytes())
        bid = ingest.ingest_file(epub)
        assert bid is not None

        h.on_deleted(FileDeletedEvent(str(epub)))

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books WHERE id=?", (bid,)).fetchone()[0]
        assert n == 0, "on_deleted 后 DB 应无该行"

    def test_on_deleted_ignores_non_epub(self, tmp_path):
        """删 .txt 不触发清库(非支持格式)。"""
        from watchdog.events import FileDeletedEvent
        from app import db

        h = watcher.LibraryHandler(tmp_path)
        src = _first_sample()
        epub = tmp_path / "keep.epub"
        epub.write_bytes(src.read_bytes())
        bid = ingest.ingest_file(epub)

        # 删一个无关的 txt 文件,不应动到书
        h.on_deleted(FileDeletedEvent(str(tmp_path / "note.txt")))

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books WHERE id=?", (bid,)).fetchone()[0]
        assert n == 1, "删非 epub 不应清库"

    def test_on_deleted_ignores_directory_event(self, tmp_path):
        """删目录事件不应触发清库(只处理文件删除)。"""
        from watchdog.events import DirDeletedEvent
        from app import db

        h = watcher.LibraryHandler(tmp_path)
        src = _first_sample()
        epub = tmp_path / "keep.epub"
        epub.write_bytes(src.read_bytes())
        bid = ingest.ingest_file(epub)

        h.on_deleted(DirDeletedEvent(str(tmp_path / "subdir")))

        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books WHERE id=?", (bid,)).fetchone()[0]
        assert n == 1

    def test_on_deleted_missing_path_no_error(self, tmp_path):
        """path 不在 DB 时(如从未入库的文件被删)不抛。"""
        from watchdog.events import FileDeletedEvent

        h = watcher.LibraryHandler(tmp_path)
        # 不应抛
        h.on_deleted(FileDeletedEvent(str(tmp_path / "never_ingested.epub")))

    def test_purge_missing_removes_db_row_without_file(self, tmp_path):
        """DB 有行但原文件已 rm → _purge_missing 后 DB 无该行。"""
        from app import db

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        src = _first_sample()
        epub = tmp_path / "vanished.epub"
        epub.write_bytes(src.read_bytes())
        bid = ingest.ingest_file(epub)

        epub.unlink()  # 模拟文件系统删书
        removed = w._purge_missing()

        assert bid in removed, f"应清掉 book_id={bid},实际清了 {removed}"
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books WHERE id=?", (bid,)).fetchone()[0]
        assert n == 0

    def test_purge_missing_deletes_cover(self, tmp_path):
        """反向同步也删封面(与 remove_book 行为一致)。"""
        from tests.conftest import _sample_epub
        from app import db

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        src = _sample_epub("钦探")
        epub = tmp_path / "vanished.epub"
        epub.write_bytes(src.read_bytes())
        bid = ingest.ingest_file(epub)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT file_hash, cover_path FROM books WHERE id=?", (bid,)
            ).fetchone()
        if not row["cover_path"]:
            pytest.skip("该样本无封面")
        cover_file = ingest.COVER_DIR / f"{row['file_hash']}.jpg"
        assert cover_file.is_file()

        epub.unlink()
        w._purge_missing()

        assert not cover_file.is_file(), "反向同步应删封面"

    def test_purge_missing_keeps_existing_files(self, tmp_path):
        """文件还在的书不动(不误删)。"""
        from app import db

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)
        e1 = tmp_path / "a.epub"
        e2 = tmp_path / "b.epub"
        e1.write_bytes(_first_sample().read_bytes())
        e2.write_bytes(_second_sample().read_bytes())
        bid1 = ingest.ingest_file(e1)
        bid2 = ingest.ingest_file(e2)

        removed = w._purge_missing()

        assert removed == [], f"文件都在不应清任何书,实际清了 {removed}"
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        assert n == 2

    def test_scan_once_purges_missing(self, tmp_path):
        """_scan_once 同时做正向入库(磁盘有DB无) + 反向清库(DB有磁盘无)。"""
        from app import db

        w = watcher.LibraryWatcher(tmp_path, scan_interval=600)

        # 先入库一本
        src = _first_sample()
        gone = tmp_path / "gone.epub"
        gone.write_bytes(src.read_bytes())
        bid_gone = ingest.ingest_file(gone)

        # 删掉它,再放一本新的(磁盘有、DB 无)
        gone.unlink()
        fresh = tmp_path / "fresh.epub"
        fresh.write_bytes(_second_sample().read_bytes())

        w._scan_once()  # 应:清掉 gone + 入库 fresh

        with db.get_conn() as conn:
            n_gone = conn.execute(
                "SELECT COUNT(*) FROM books WHERE id=?", (bid_gone,)
            ).fetchone()[0]
            titles = [r["title"] for r in conn.execute("SELECT title FROM books").fetchall()]
        assert n_gone == 0, "反向同步应清掉已删文件的书"
        assert len(titles) == 1, "正向应入库新书,最终只 1 本"
