"""共享测试 fixture。

每个测试获得独立临时 DB + 书库目录,互不污染。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "books-library"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient + 临时 DB + 临时书库目录。"""
    from app import db, watcher as watcher_mod
    import app.main as main_mod

    db.DB_PATH = tmp_path / "test.db"
    monkeypatch.setattr(main_mod, "LIBRARY_DIR", tmp_path / "lib")
    # 跳过真实 watcher 启动(避免后台线程干扰测试)
    monkeypatch.setattr(watcher_mod, "start_watcher", lambda d: type("W", (), {
        "start": lambda self: None, "stop": lambda self: None
    })())

    db.init_db()
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def tmp_library(tmp_path):
    """一个临时书库目录。"""
    return tmp_path / "lib"


@pytest.fixture(autouse=True)
def isolate_db(monkeypatch, tmp_path):
    """每个测试自动获得独立临时数据库+封面目录,互不污染。"""
    from app import db, ingest
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(ingest, "COVER_DIR", tmp_path / "covers")
    db.init_db()


def _first_sample() -> Path:
    epubs = sorted(SAMPLE_DIR.glob("*.epub"))
    assert epubs, "books-library 无测试 epub"
    return epubs[0]


def _second_sample() -> Path:
    epubs = sorted(SAMPLE_DIR.glob("*.epub"))
    assert len(epubs) >= 2, "需要至少 2 本 epub"
    return epubs[1]


def _sample_epub(name_keyword: str) -> Path:
    for p in SAMPLE_DIR.glob("*.epub"):
        if name_keyword in p.name:
            return p
    pytest.skip(f"找不到含 {name_keyword} 的测试 epub")
