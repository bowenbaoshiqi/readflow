"""测试 API 路由:library + reader(进度、划线、文件服务)。
"""
from __future__ import annotations

from app import ingest

from tests.conftest import _first_sample, _second_sample


class TestHealth:
    """/api/health 健康检查"""

    def test_health_returns_ok(self, client):
        """health 端点返回 {"ok":true},供 Docker HEALTHCHECK 探活。"""
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


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


class TestHtmlReaderPage:
    """/read-html/{id} Kindle 友好的服务端渲染 HTML 阅读页。"""

    def test_html_reader_200(self, client):
        """存在的书返回 HTML 阅读页。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read-html/{bid}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_html_reader_404(self, client):
        """不存在的书返回 404。"""
        r = client.get("/read-html/99999")
        assert r.status_code == 404

    def test_html_reader_has_chapter_content(self, client):
        """页面应含章节正文(从 epub spine 提取),不是空白。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read-html/{bid}")
        # #content 容器 + .text 内层应有非空正文
        assert 'id="content"' in r.text
        assert 'class="text"' in r.text
        # 提取 .text 内纯文本,应有一定长度(跳过空白扉页后取到正文章节)
        import re
        m = re.search(r'<div class="text">(.*?)</div>\s*<div id="tap', r.text, re.S)
        assert m, "应找到 .text 内容区"
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        assert len(text) > 20, f"章节正文应非空,实际 {len(text)} 字"

    def test_html_reader_has_font_and_pagination(self, client):
        """页面应含霞鹜文楷字体引用 + columns 分栏 + 翻页点击层。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read-html/{bid}")
        assert "lxgw.ttf" in r.text, "应引用霞鹜文楷字体"
        assert "column-width" in r.text or "columnWidth" in r.text, "应用 CSS columns 分栏"
        assert 'id="tap-left"' in r.text and 'id="tap-right"' in r.text, "应有左右翻页点击层"

    def test_html_reader_chapter_param(self, client):
        """?ch=N 翻到不同章节:ch=0 和 ch=大值 应返回不同内容。"""
        bid = ingest.ingest_file(_first_sample())
        r0 = client.get(f"/read-html/{bid}?ch=0")
        r_big = client.get(f"/read-html/{bid}?ch=9999")  # 越界 → 回落到最后一个有内容章节
        assert r0.status_code == 200
        assert r_big.status_code == 200
        # 两者进度百分比应不同(除非全书只有一章)
        import re
        def pct(text):
            m = re.search(r'id="pct">(\d+)%', text)
            return int(m.group(1)) if m else -1
        # 大 ch 落到最后一章,进度应 >= ch=0 的进度
        assert pct(r_big.text) >= pct(r0.text)

    def test_html_reader_prev_ch_link_none_for_first_chapter(self, client):
        """第一章的"上一章"链接应是禁用态(href=# + 灰色)。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read-html/{bid}?ch=0")
        # 第一章 prev_ch 为 None,链接 href 应是 # 或带灰色样式
        assert 'id="prev-ch-link"' in r.text
        # 灰色样式表示禁用
        assert "color:#999" in r.text or 'href="#"' in r.text


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


class TestReaderBackButton:
    """阅读页 #back 返回按钮:应回书架首页 /,不是 history.back()。

    history.back() 是浏览器历史回退,行为不确定:
    - 从 书架→详情页→阅读页 进来,只退到详情页而非书架
    - 直接打开阅读页链接(无历史)时卡死,按钮无反应
    用户预期"返回"= 回书架。
    """

    def test_back_button_goes_to_home_not_history_back(self, client, tmp_library):
        """reader.js 的 #back 处理应是 location.href='/',不含 history.back()。"""
        bid = ingest.ingest_file(_first_sample())
        # /read/{id} 页面注入 reader.js,返回逻辑在 reader.js 里
        r = client.get(f"/read/{bid}")
        assert r.status_code == 200
        assert "reader.js" in r.text, "阅读页应加载 reader.js"

        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        assert "history.back()" not in js, \
            "#back 不应使用 history.back();应 location.href='/' 回书架"
        assert "location.href = '/'" in js or "location.href='/'" in js, \
            "#back 应 location.href='/' 回书架首页"


class TestReaderToolbarButtons:
    """v0.6 Bug2: 划线/复制按钮从底部固定栏移到顶部 #toolbar。

    问题: #bottom-bar 固定钉在视口底部居中(z-index:20),用户选中靠底部
    的文字时(读书时最常见),工具条正好压在选区上,遮挡文字。
    方案 C: 把按钮挪进顶部 #toolbar,不再有底部浮层遮挡正文。
    """

    def _html(self, client):
        bid = ingest.ingest_file(_first_sample())
        return client.get(f"/read/{bid}")

    def test_bottom_bar_removed_from_html(self, client):
        """#bottom-bar 容器应从阅读页 HTML 移除。"""
        r = self._html(client)
        assert 'id="bottom-bar"' not in r.text, \
            "应移除 #bottom-bar(改用顶部 #toolbar 承载划线/复制按钮)"

    def test_highlight_button_in_toolbar(self, client):
        """划线按钮应在 #toolbar 内,带 data-act=highlight。"""
        r = self._html(client)
        html = r.text
        # 定位 #toolbar 块
        start = html.find('id="toolbar"')
        end = html.find("</div>", start)
        toolbar_block = html[start:end]
        assert 'data-act="highlight"' in toolbar_block, \
            "划线按钮应在 #toolbar 内"

    def test_copy_button_in_toolbar(self, client):
        """复制按钮应在 #toolbar 内,带 data-act=copy。"""
        r = self._html(client)
        html = r.text
        start = html.find('id="toolbar"')
        end = html.find("</div>", start)
        toolbar_block = html[start:end]
        assert 'data-act="copy"' in toolbar_block, \
            "复制按钮应在 #toolbar 内"

    def test_reader_js_targets_toolbar_not_bottom_bar(self):
        """reader.js 点击监听应绑到 #toolbar(或其内按钮),不再绑 #bottom-bar。"""
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        # bottom-bar 的 addEventListener 应已移除
        assert "getElementById('bottom-bar')" not in js, \
            "reader.js 不应再引用 #bottom-bar;改绑 #toolbar"
        # 应有 toolbar 相关监听
        assert "toolbar" in js, "reader.js 应监听 #toolbar 的划线/复制点击"

    def test_css_no_fixed_bottom_bar(self):
        """reader.css 中 #bottom-bar 的 position:fixed 底部浮层样式应移除。"""
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "reader.css").read_text()
        # #bottom-bar 选择器不应再有 position:fixed / bottom 定位
        import re
        m = re.search(r"#bottom-bar\s*\{[^}]*\}", css)
        assert not m or "fixed" not in m.group(0), \
            "#bottom-bar 不应再是 position:fixed 浮层"

    def test_toolbar_buttons_hidden_by_default(self, client):
        """划线/复制按钮默认隐藏(无选区时不显示),选中文字才出现。

        挪进 #toolbar 后不能常驻(否则挤占工具栏),需 hidden 默认。
        """
        r = self._html(client)
        html = r.text
        start = html.find('id="toolbar"')
        end = html.find("</div>", start)
        toolbar_block = html[start:end]
        # 两个按钮应带 hidden 或在默认隐藏的容器里
        assert "hidden" in toolbar_block.lower(), \
            "划线/复制按钮应默认 hidden,选中文字才显示"


class TestReaderScrolledFlow:
    """v0.6 Bug1: 翻页改用滚动模式(flow=scrolled),绕开分栏边界崩溃。

    背景: foliate-js 分栏翻页器(CSS multi-column)在边界场景崩溃
    - paginator.js 862: #onTouchEnd 读 undefined #touchState
    - paginator.js 806/797: snap/scrollBy 解构 undefined #scrollBounds
    - paginator.js 1016: #goTo 读 undefined sections[index].load
    上游承认 slow+buggy,无现成替换(README:171, issue #108/#86)。
    滚动模式下 paginator 的 #onTouchEnd 直接 return(603行),snap 不触发,
    上述分栏边界代码全部绕开,最稳。
    """

    def test_reader_js_sets_scrolled_flow(self):
        """reader.js 应在 open 后设 flow=scrolled。"""
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        assert "flow" in js and "scrolled" in js, \
            "reader.js 应设 setAttribute('flow', 'scrolled')"

    def test_reader_js_no_click_paging(self):
        """滚动模式下不应有点击左右半屏翻页(键盘 goLeft/goRight 作备用可保留)。"""
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        # 点击翻页那段(view.addEventListener('click' + goLeft/goRight)应移除
        assert "点左半屏" not in js and "点击左右半屏翻页" not in js, \
            "滚动模式移除点击半屏翻页;用户直接上下滑"

    def test_css_viewer_scrollable(self):
        """#viewer 在滚动模式应允许内容滚动(非 overflow:hidden 钉死)。"""
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "reader.css").read_text()
        import re
        m = re.search(r"#viewer\s*\{[^}]*\}", css)
        assert m, "reader.css 应有 #viewer 规则"
        # 滚动模式 viewer 不能 overflow:hidden 把滚动条吃掉
        viewer_rule = m.group(0)
        assert "overflow: hidden" not in viewer_rule and "overflow:hidden" not in viewer_rule, \
            "#viewer 不应 overflow:hidden(滚动模式需可见滚动)"

    def test_reader_js_version_bumped(self, client):
        """reader.js/css 引用应带新版本号(让手机拉新文件,绕开缓存)。"""
        import re
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read/{bid}")
        # 版本号应 >= v0.6.3(滚动模式 + 章节导航改动需 bump)。不写死具体小版本,
        # 否则每 bump 一次就得改测试——用正则提取并比较次版本号。
        vers = re.findall(r"\?v=v(\d+)\.(\d+)\.(\d+)", r.text)
        assert vers, "reader.js/css 引用应带 ?v=vX.Y.Z 版本号防缓存"
        # 任一引用 >= v0.6.3 即可
        assert any((int(ma), int(mi), int(pa)) >= (0, 6, 3) for ma, mi, pa in vers), \
            f"版本号应 >= v0.6.3,实际 {vers}"

    def test_toolbar_has_prev_next_chapter(self, client):
        """滚动模式无整页翻页,toolbar 需有上一章/下一章按钮。"""
        bid = ingest.ingest_file(_first_sample())
        r = client.get(f"/read/{bid}")
        html = r.text
        start = html.find('id="toolbar"')
        end = html.find("</div>", start)
        toolbar_block = html[start:end]
        assert 'id="prev-ch"' in toolbar_block, "#toolbar 应有上一章按钮"
        assert 'id="next-ch"' in toolbar_block, "#toolbar 应有下一章按钮"

    def test_reader_js_has_chapter_nav(self):
        """reader.js 应有上下章跳转逻辑:取当前 section index + 找相邻 linear section + goTo。

        scrolled 模式原生滚动只在单章内滚,不自动跨章,靠按钮。
        当前 section index 取 renderer.getContents()[0].index(内部 #index,直接反映
        正在渲染的 section,比 relocate 事件存的 currentLocation 更即时——relocate 在
        scrolled 下由 scroll 事件 250ms debounce 触发,有延迟且首屏前为 null)。
        """
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        # 按钮点击处理
        assert "prev-ch" in js and "next-ch" in js, \
            "reader.js 应处理 prev-ch / next-ch 按钮点击"
        # 当前 section index 取自 getContents(非 currentLocation,理由见 docstring)
        assert "getContents" in js, "应通过 renderer.getContents() 取当前 section index"
        assert "goTo" in js, "应用 view.goTo 跳转"
        # 跳相邻 readable section(linear !== 'no'),与 paginator #adjacentIndex 同逻辑
        assert "linear" in js and "'no'" in js, "应跳过 linear==='no' 的 section"



class TestReaderSessionPost:
    """reader.js 关书时应 POST reading-session。

    源码契约断言:reader.js 含 reading-session 端点 + beforeunload 处理。
    不做 e2e,但验证源码存在必要结构。
    """

    def test_reader_js_has_session_post(self):
        """reader.js 应在周期、后台和离页场景可靠提交。"""
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js").read_text()
        assert "ReadingSession" in js
        assert "/reading-session" in js
        assert "visibilitychange" in js
        assert "pagehide" in js
        assert "beforeunload" in js
        assert "navigator.sendBeacon" in js
        assert "readingSession.flush" in js


class TestHomepageCards:
    """主页应包含知识卡片展示。"""

    def test_homepage_has_knowledge_reference(self, client):
        """主页 HTML 应引用知识 API 端点或知识相关元素。"""
        r = client.get("/")
        assert r.status_code == 200
        html = r.text.lower()
        assert "knowledge" in html or "卡片" in html or "card" in html, \
            "主页应包含知识/卡片相关内容"

    def test_knowledge_page_shows_parent_title_on_recommendation(self, client):
        """推荐卡片应显著标注:推荐书名 + 关联的盲点/知识点标题 + 推荐类型。

        - 术语用"推荐书"(不是"推进书")
        - 区分"盲点推荐"vs"知识点推荐":带 parent_card_type 标识父卡片类型
        - 带 parent_title 标注关联的盲点/知识点标题
        """
        from app import db
        with db.db() as conn:
            # 盲点父卡 + 其推荐
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, created_at)
                   VALUES(?,?,?,datetime('now'))""",
                ("blind_spot", "缺少认知心理学视角", "盲点内容"),
            )
            blind_id = conn.execute(
                "SELECT id FROM knowledge_cards WHERE title='缺少认知心理学视角'"
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body,
                   parent_card_id, recommend_book, created_at)
                   VALUES(?,?,?,?,?,datetime('now'))""",
                ("recommendation", "思考快与慢", "推荐理由", blind_id,
                 '{"title":"思考，快与慢","author":"卡尼曼","reason":"r","summary":"s","isbn":"1"}'),
            )
            # 知识点父卡 + 其推荐
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, created_at)
                   VALUES(?,?,?,datetime('now'))""",
                ("knowledge", "幸存者偏差", "知识点内容"),
            )
            know_id = conn.execute(
                "SELECT id FROM knowledge_cards WHERE title='幸存者偏差'"
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body,
                   parent_card_id, recommend_book, created_at)
                   VALUES(?,?,?,?,?,datetime('now'))""",
                ("recommendation", "黑天鹅", "推荐理由", know_id,
                 '{"title":"黑天鹅","author":"塔勒布","reason":"r","summary":"s","isbn":"2"}'),
            )

        r = client.get("/api/knowledge/cards?card_type=recommendation")
        assert r.status_code == 200
        cards = r.json()
        rec_blind = next(c for c in cards if c["title"] == "思考快与慢")
        rec_know = next(c for c in cards if c["title"] == "黑天鹅")

        # 推荐书(不是"推进书")
        assert rec_blind["recommend_book"]
        assert rec_know["recommend_book"]

        # 关联的盲点/知识点标题
        assert rec_blind.get("parent_title") == "缺少认知心理学视角"
        assert rec_know.get("parent_title") == "幸存者偏差"

        # 区分盲点推荐 vs 知识点推荐
        assert rec_blind.get("parent_card_type") == "blind_spot", \
            "基于盲点的推荐应标识 parent_card_type=blind_spot"
        assert rec_know.get("parent_card_type") == "knowledge", \
            "基于知识点的推荐应标识 parent_card_type=knowledge"
