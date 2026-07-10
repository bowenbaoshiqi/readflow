"""后端单元测试:入库去重 + API 往返。不依赖浏览器。

用 FastAPI TestClient + 临时书库目录,跑真实 epub。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试用 epub:复用项目内已复制的样本
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "books-library"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个测试用独立临时 DB + 书库目录,互不污染。"""
    from app import db, watcher as watcher_mod
    import app.main as main_mod

    # 重定向 DB 路径到临时目录
    db.DB_PATH = tmp_path / "test.db"
    monkeypatch.setattr(main_mod, "LIBRARY_DIR", tmp_path / "lib")
    # 跳过真实 watcher(用 TestClient lifespan 会启动,但测试手动调 ingest)
    monkeypatch.setattr(watcher_mod, "start_watcher", lambda d: type("W", (), {
        "start": lambda self: None, "stop": lambda self: None
    })())

    db.init_db()
    with TestClient(main_mod.app) as c:
        yield c


def _first_sample() -> Path:
    epubs = sorted(SAMPLE_DIR.glob("*.epub"))
    assert epubs, "books-library 无测试 epub"
    return epubs[0]


def test_ingest_dedup(client, tmp_path):
    """同一文件 ingest 两次应返回相同 book_id(file_hash 去重)。"""
    from app import ingest, db
    lib = Path(client.app.__dict__.get("LIBRARY_DIR", "books-library"))
    # 直接用样本文件
    src = _first_sample()
    bid1 = ingest.ingest_file(src)
    bid2 = ingest.ingest_file(src)
    assert bid1 is not None
    assert bid1 == bid2, "去重失败:同 hash 应返回同 id"

    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    assert n == 1, f"去重后应只有 1 条记录,实际 {n}"


def test_library_api(client):
    """书库列表 + 详情 API。"""
    from app import ingest
    bid = ingest.ingest_file(_first_sample())

    r = client.get("/api/library")
    assert r.status_code == 200
    books = r.json()
    assert any(b["id"] == bid for b in books)

    r = client.get(f"/api/library/{bid}")
    assert r.status_code == 200
    assert r.json()["title"]


def test_progress_roundtrip(client):
    """进度 PUT/GET 往返。"""
    from app import ingest
    bid = ingest.ingest_file(_first_sample())

    r = client.put(f"/api/books/{bid}/progress", json={
        "spine_index": 3, "cfi": "epubcfi(/6/4!/10/1:0)", "percent": 0.42
    })
    assert r.status_code == 200

    r = client.get(f"/api/books/{bid}/progress")
    data = r.json()
    assert data["spine_index"] == 3
    assert data["cfi"] == "epubcfi(/6/4!/10/1:0)"
    assert abs(data["percent"] - 0.42) < 0.01


def test_highlight_roundtrip(client):
    """划线 POST/GET/DELETE,CFI 锚点保持。"""
    from app import ingest
    bid = ingest.ingest_file(_first_sample())

    r = client.post(f"/api/books/{bid}/highlights", json={
        "spine_index": 3,
        "start_cfi": "epubcfi(/6/4!/10/1:0)",
        "end_cfi": "epubcfi(/6/4!/10/1:50)",
        "text": "测试划线文本",
    })
    assert r.status_code == 200
    hid = r.json()["id"]

    r = client.get(f"/api/books/{bid}/highlights")
    hs = r.json()
    assert len(hs) == 1
    assert hs[0]["start_cfi"] == "epubcfi(/6/4!/10/1:0)"
    assert hs[0]["text"] == "测试划线文本"

    r = client.delete(f"/api/highlights/{hid}")
    assert r.status_code == 200
    assert client.get(f"/api/books/{bid}/highlights").json() == []


def test_epub_file_served(client):
    """epub 文件能被正确服务。"""
    from app import ingest
    bid = ingest.ingest_file(_first_sample())
    r = client.get(f"/api/books/{bid}/file")
    assert r.status_code == 200
    assert "epub" in r.headers.get("content-type", "")
    assert len(r.content) > 1000
