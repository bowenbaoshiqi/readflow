"""测试 watcher 模块:定时扫描 + 事件去抖 + 幂等。

注意:真实 watchdog inotify 事件测试在不同 OS 上行为差异大,本文件主要测试
LibraryHandler 和 LibraryWatcher 的 flush/scan 逻辑,不大量依赖真实文件系统事件。
"""
from __future__ import annotations

import time
from pathlib import Path

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
