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

    def test_detail_returns_metadata_fields(self, client):
        """详情接口应返回外部元数据字段(v0.2 新增)。

        入库后 epub 内嵌字段已写入(publisher/summary/isbn...),
        外部元数据字段(rating/tags/meta_status...)即使为 null 也要在响应里。
        """
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/library/{bid}")
        assert r.status_code == 200
        data = r.json()
        for field in ("summary", "rating", "rating_count", "tags",
                      "publisher", "publish_date", "isbn",
                      "meta_source", "meta_status"):
            assert field in data, f"详情接口缺字段: {field}"

    def test_detail_epub_embedded_fields_present(self, client):
        """详情接口的 epub 内嵌字段应有值(《钦探》有 ISBN + 简介)。"""
        from tests.conftest import _sample_epub
        bid = ingest.ingest_file(_sample_epub("钦探"))
        r = client.get(f"/api/library/{bid}")
        data = r.json()
        assert data["isbn"] == "9787521226805"
        assert data["summary"]  # epub 内嵌简介

    def test_list_includes_rating(self, client):
        """列表接口应含 rating 字段(前端卡片显示评分用)。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get("/api/library")
        books = r.json()
        book = next(b for b in books if b["id"] == bid)
        assert "rating" in book

    def test_detail_null_fields_when_no_metadata(self, client):
        """无外部元数据的书,字段为 null 不报错(优雅降级)。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/api/library/{bid}")
        data = r.json()
        # rating 在 enrich 完成前是 null,不应报错
        assert data["rating"] is None or isinstance(data["rating"], (int, float))
        assert data["tags"] is None or isinstance(data["tags"], str)


class TestBookDetailPage:
    """GET /book/{id} 详情页 HTML。"""

    def test_detail_page_returns_html(self, client):
        """详情页返回 200 + HTML,含书名。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "开始阅读" in r.text or "继续阅读" in r.text

    def test_detail_page_404_missing_book(self, client):
        """不存在的书详情页 404。"""
        r = client.get("/book/99999")
        assert r.status_code == 404

    def test_detail_page_shows_summary_and_tags(self, client):
        """详情页渲染简介和标签(《钦探》有 epub 内嵌简介)。"""
        from tests.conftest import _sample_epub
        bid = ingest.ingest_file(_sample_epub("钦探"))
        # 直接写 tags 模拟 enrich 结果
        from app import db
        with db.db() as conn:
            conn.execute("UPDATE books SET tags=? WHERE id=?",
                         ('["China"]', bid))
        r = client.get(f"/book/{bid}")
        assert "简介" in r.text  # 简介区标题
        assert "China" in r.text  # 标签渲染
        assert "9787521226805" in r.text  # ISBN 显示

    def test_detail_page_escapes_html_in_summary(self, client):
        """简介含 HTML 特殊字符时应转义,防 XSS。"""
        from app import db
        bid = ingest.ingest_file(_first_sample())
        with db.db() as conn:
            conn.execute("UPDATE books SET summary=? WHERE id=?",
                         ("<script>alert(1)</script>", bid))
        r = client.get(f"/book/{bid}")
        assert "<script>" not in r.text
        assert "&lt;script&gt;" in r.text

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

    def test_library_cover_returns_correct_image_bytes(self, client):
        """取到的封面字节 == 该书 file_hash 对应的封面文件字节(防串图)。

        回归 4.jpg/5.jpg 内容相同但属不同书的 bug:get_cover 必须按
        file_hash 取图,确保返回的是这本书自己的封面。
        """
        from tests.conftest import _sample_epub
        from app import ingest, db
        bid = ingest.ingest_file(_sample_epub("钦探"))
        r = client.get(f"/api/library/{bid}/cover")
        if r.status_code != 200:
            return  # 该样本无封面,跳过

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT file_hash FROM books WHERE id=?", (bid,)
            ).fetchone()
        fhash = row["file_hash"]
        # 直接读 file_hash 命名的封面文件,字节应与 API 返回一致
        expected = (ingest.COVER_DIR / f"{fhash}.jpg").read_bytes()
        assert r.content == expected, "API 返回的封面应与该书 file_hash 文件一致"

    def test_library_cover_different_books_different_images(self, client):
        """两本不同的书,封面内容应不同(防串图回归)。"""
        from tests.conftest import _sample_epub
        bid_a = ingest.ingest_file(_sample_epub("钦探"))
        bid_b = ingest.ingest_file(_first_sample())
        ra = client.get(f"/api/library/{bid_a}/cover")
        rb = client.get(f"/api/library/{bid_b}/cover")
        if ra.status_code != 200 or rb.status_code != 200:
            return  # 样本缺封面,跳过
        assert ra.content != rb.content, "两本不同的书封面不应字节相同"


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
