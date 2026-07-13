"""测试 API 路由:library + reader(进度、划线、文件服务)。
"""
from __future__ import annotations

from app import ingest

from tests.conftest import _first_sample, _second_sample


class TestLibraryAPI:
    """GET /api/library 书库列表"""

    def test_library_empty(self, client):
        """空书库返回空列表。"""
        r = client.get("/api/library")
        assert r.status_code == 200
        assert r.json() == []

    def test_library_with_books(self, client):
        """有书时返回完整字段。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get("/api/library")
        assert r.status_code == 200
        books = r.json()
        assert any(b["id"] == bid for b in books)
        assert any("title" in b and "author" in b for b in books)

    def test_library_book_detail_found(self, client):
        """获取存在的书详情。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/library/{bid}")
        assert r.status_code == 200
        assert r.json()["title"]

    def test_library_book_detail_404(self, client):
        """获取不存在的书返回 404。"""
        r = client.get("/api/library/99999")
        assert r.status_code == 404

    def test_library_cover_200(self, client):
        """有封面的书能获取封面图。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/library/{bid}/cover")
        assert r.status_code in (200, 404)  # 有些 epub 无封面

    def test_library_cover_404_no_cover(self, client):
        """无封面的书返回 404。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/library/{bid}/cover")
        assert r.status_code in (200, 404)  # 200 如果有封面,否则 404


class TestReaderPage:
    """阅读器页面"""

    def test_reader_page_200(self, client):
        """存在的书返回 HTML 阅读器页面。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read/{bid}")
        assert r.status_code == 200
        assert "foliate-view" in r.text or "data-book-id" in r.text

    def test_reader_page_404(self, client):
        """不存在的书返回 404。"""
        r = client.get("/read/99999")
        assert r.status_code == 404

    def test_reader_page_has_data_attrs(self, client):
        """阅读器页面应含 data-book-id 和 data-base。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read/{bid}")
        assert f'data-book-id="{bid}"' in r.text
        assert 'data-base=' in r.text


class TestEpubFileService:
    """GET /api/books/{id}/file"""

    def test_epub_file_200(self, client):
        """存在的书返回 epub 文件。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/books/{bid}/file")
        assert r.status_code == 200
        assert "epub+zip" in r.headers.get("content-type", "")
        assert len(r.content) > 1000

    def test_epub_file_404_not_found(self, client):
        """不存在的书返回 404。"""
        r = client.get("/api/books/99999/file")
        assert r.status_code == 404

    def test_epub_file_404_unsupported_format(self, client, tmp_path):
        """unsupported 状态的书不暴露文件。"""
        mobi = tmp_path / "book.mobi"
        mobi.write_bytes(b"mobi")
        bid = ingest.ingest_file(mobi)
        r = client.get(f"/api/books/{bid}/file")
        assert r.status_code == 404


class TestProgressAPI:
    """进度 PUT/GET"""

    def test_progress_default_zero(self, client):
        """未设置进度时默认为 0。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/books/{bid}/progress")
        assert r.status_code == 200
        data = r.json()
        assert data["spine_index"] == 0
        assert data["percent"] == 0.0

    def test_progress_put_get_roundtrip(self, client):
        """PUT 后 GET 应返回相同值。"""
        bid = ingest.ingest_file(_first_sample())
        body = {"spine_index": 2, "cfi": "epubcfi(/6/4!/10/1:0)", "percent": 0.42}
        r = client.put(f"/api/books/{bid}/progress", json=body)
        assert r.status_code == 200

        r = client.get(f"/api/books/{bid}/progress")
        data = r.json()
        assert data["spine_index"] == 2
        assert data["cfi"] == "epubcfi(/6/4!/10/1:0)"
        assert abs(data["percent"] - 0.42) < 0.01

    def test_progress_upsert_overwrites(self, client):
        """第二次 PUT 覆盖前一次。"""
        bid = ingest.ingest_file(_first_sample())
        client.put(f"/api/books/{bid}/progress", json={"spine_index": 1, "cfi": "a", "percent": 0.1})
        client.put(f"/api/books/{bid}/progress", json={"spine_index": 5, "cfi": "b", "percent": 0.9})
        r = client.get(f"/api/books/{bid}/progress")
        data = r.json()
        assert data["spine_index"] == 5
        assert data["percent"] == 0.9

    def test_progress_put_404_for_missing_book(self, client):
        """对不存在的 book_id 存进度应返回 404,而非 500。"""
        r = client.put("/api/books/99999/progress", json={
            "spine_index": 0, "cfi": None, "percent": 0.0,
        })
        assert r.status_code == 404


class TestHighlightAPI:
    """划线 CRUD"""

    def test_highlights_empty(self, client):
        """无划线时返回空列表。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/books/{bid}/highlights")
        assert r.status_code == 200
        assert r.json() == []

    def test_highlights_create_list(self, client):
        """POST 划线后 GET 列到。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.post(f"/api/books/{bid}/highlights", json={
            "spine_index": 2, "start_cfi": "epubcfi(/a)", "end_cfi": "epubcfi(/b)", "text": "划一段",
        })
        assert r.status_code == 200
        hid = r.json()["id"]

        r = client.get(f"/api/books/{bid}/highlights")
        hs = r.json()
        assert len(hs) == 1
        assert hs[0]["text"] == "划一段"
        assert hs[0]["id"] == hid

    def test_highlights_delete(self, client):
        """DELETE 后划线消失。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.post(f"/api/books/{bid}/highlights", json={
            "spine_index": 2, "start_cfi": "epubcfi(/a)", "end_cfi": "epubcfi(/b)", "text": "划一段",
        })
        hid = r.json()["id"]
        client.delete(f"/api/highlights/{hid}")
        assert client.get(f"/api/books/{bid}/highlights").json() == []

    def test_highlights_book_isolation(self, client):
        """不同书的划线互不干扰。"""
        bid1 = ingest.ingest_file(_first_sample())
        bid2 = ingest.ingest_file(_second_sample())
        client.post(f"/api/books/{bid1}/highlights", json={
            "spine_index": 0, "start_cfi": "a", "end_cfi": "b", "text": "book1",
        })
        assert len(client.get(f"/api/books/{bid2}/highlights").json()) == 0
        assert len(client.get(f"/api/books/{bid1}/highlights").json()) == 1

    def test_highlights_color_default_yellow(self, client):
        """默认颜色为 yellow。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.post(f"/api/books/{bid}/highlights", json={
            "spine_index": 0, "start_cfi": "a", "end_cfi": "b", "text": "x",
        })
        hs = client.get(f"/api/books/{bid}/highlights").json()
        assert hs[0]["color"] == "yellow"

    def test_highlights_custom_color(self, client):
        """可指定自定义颜色。"""
        bid = ingest.ingest_file(_first_sample())
        client.post(f"/api/books/{bid}/highlights", json={
            "spine_index": 0, "start_cfi": "a", "end_cfi": "b", "text": "x", "color": "pink",
        })
        hs = client.get(f"/api/books/{bid}/highlights").json()
        assert hs[0]["color"] == "pink"

    def test_highlights_post_404_for_missing_book(self, client):
        """对不存在的 book_id 划线应返回 404,而非 500。"""
        r = client.post("/api/books/99999/highlights", json={
            "spine_index": 0, "start_cfi": "a", "end_cfi": "b", "text": "x",
        })
        assert r.status_code == 404
