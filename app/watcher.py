"""书库目录监听:watchdog 事件 + 定时全量扫描兜底。

设计(采纳 MRD 评审):
- 事件去抖:新文件写入完成(稳定)后才入库,避免读到半写文件
- 兜底扫描:定期全量比对,弥补 inotify 在网络挂载/边缘场景的漏触发
- 幂等:ingest 内部按 file_hash 去重,重复触发安全
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import db, ingest

SUPPORTED_EXT = {".epub"}  # v0.1
STABLE_SECONDS = 2.0       # 文件写入稳定阈值


class LibraryHandler(FileSystemEventHandler):
    def __init__(self, library_dir: Path):
        self.library_dir = Path(library_dir)
        self._pending: dict[str, float] = {}  # path -> 最后一次事件时间(用于去抖)
        self._lock = threading.Lock()

    def _schedule(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() not in SUPPORTED_EXT:
            return
        with self._lock:
            self._pending[path_str] = time.time()

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event):
        # 重命名:watchdog 携带 src/dest,去重靠 hash,直接调 ingest 更新路径
        if not event.is_directory:
            self._schedule(event.dest_path)

    def flush_stable(self) -> list[int]:
        """处理已稳定(stable_seconds 内无新写入)的待入库文件。返回入库的 book_id 列表。"""
        now = time.time()
        ready: list[str] = []
        with self._lock:
            for p, t in list(self._pending.items()):
                if now - t >= STABLE_SECONDS:
                    ready.append(p)
                    del self._pending[p]
        ids: list[int] = []
        for p in ready:
            bid = ingest.ingest_file(Path(p))
            if bid is not None:
                ids.append(bid)
        return ids


class LibraryWatcher:
    def __init__(self, library_dir: Path, scan_interval: int = 600):
        self.library_dir = Path(library_dir).resolve()
        self.scan_interval = scan_interval
        self.handler = LibraryHandler(self.library_dir)
        self.observer = Observer()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.observer.schedule(self.handler, str(self.library_dir), recursive=True)
        self.observer.start()
        # 启动时全量扫描一次(纳入已有文件)
        self._scan_once()
        # 两个后台线程:稳定文件 flush + 定时兜底扫描
        self._threads = [
            threading.Thread(target=self._flush_loop, daemon=True),
            threading.Thread(target=self._scan_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self.observer.stop()
        self.observer.join(timeout=5)

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.handler.flush_stable()
            except Exception as e:
                print(f"[watcher] flush error: {e}")
            self._stop.wait(1.0)

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.scan_interval)
            if self._stop.is_set():
                break
            try:
                self._scan_once()
            except Exception as e:
                print(f"[watcher] scan error: {e}")

    def _scan_once(self) -> None:
        """全量扫描书库,把磁盘上有但 DB 没有的文件入库。幂等。"""
        if not self.library_dir.is_dir():
            return
        for p in self.library_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
                ingest.ingest_file(p)


_watcher: LibraryWatcher | None = None


def start_watcher(library_dir: Path) -> LibraryWatcher:
    global _watcher
    if _watcher is not None:
        return _watcher
    _watcher = LibraryWatcher(library_dir)
    _watcher.start()
    return _watcher


def get_watcher() -> LibraryWatcher | None:
    return _watcher
