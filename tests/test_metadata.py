"""测试外部元数据:Google Books provider 解析 + provider 抽象 + enrich 写回。

设计(TDD seam):
- S1 GoogleBooksProvider.search(title, author, isbn) → BookMeta|None
  网络调用集中在 provider,用 mock httpx 测解析逻辑,不触真网。
- S2 provider 抽象 + 注册表:get_provider() / 未知源抛错 / 豆瓣扩展点。
- S3 enrich_book(book_id):调 provider → 写回 books 新字段 + meta_status。
  mock provider,只测"调度+写回+状态机",不测 provider 内部。
- S4 ingest 集成:入库后异步触发 enrich,不阻塞入库返回。
- S5 API:详情/列表接口返回新字段。
"""
from __future__ import annotations

from app import metadata


# ---------- S1: GoogleBooksProvider 解析 Google Books JSON ----------

class TestGoogleBooksProviderParse:
    """search() 把 Google Books JSON 解析成 BookMeta,网络层 mock。"""

    def _fake_volume(self, **overrides):
        """构造一个最小可解析的 Google Books volumeInfo。"""
        v = {
            "title": "北京法源寺",
            "authors": ["李敖"],
            "publisher": "中国友谊出版公司",
            "publishedDate": "2000",
            "language": "zh-CN",
            "categories": ["China"],
            "description": "这是一本关于北京法源寺的历史小说。",
            "averageRating": 4.5,
            "ratingsCount": 2,
            "pageCount": 304,
            "industryIdentifiers": [
                {"type": "ISBN_13", "identifier": "9787505715486"},
                {"type": "ISBN_10", "identifier": "7505715485"},
            ],
        }
        v.update(overrides)
        return {"items": [{"volumeInfo": v}]}

    def test_parse_full_volume_returns_bookmeta(self, monkeypatch):
        """完整 volume → BookMeta 字段全映射(用 ISBN 查询)。"""
        captured = {}
        def fake_get(url, params=None, **kw):
            captured["url"] = url
            captured["params"] = params
            return _FakeResp(200, self._fake_volume())
        monkeypatch.setattr(metadata.httpx, "get", fake_get)

        p = metadata.GoogleBooksProvider(api_key="testkey")
        m = p.search(title="北京法源寺", author="李敖", isbn="9787505715486")

        assert m is not None
        assert m["title"] == "北京法源寺"
        assert m["author"] == "李敖"
        assert m["publisher"] == "中国友谊出版公司"
        assert m["publish_date"] == "2000"
        assert m["summary"] == "这是一本关于北京法源寺的历史小说。"
        assert m["rating"] == 4.5
        assert m["rating_count"] == 2
        assert m["tags"] == ["China"]
        assert m["isbn"] == "9787505715486"  # 取 ISBN_13
        assert m["page_count"] == 304
        # 确认走的是 isbn: 精确查询
        assert "isbn:9787505715486" in captured["params"]["q"]

    def test_isbn_query_uses_isbn_param(self, monkeypatch):
        """有 ISBN 时走 isbn: 精确查询。"""
        captured = {}
        def fake_get(url, params=None, **kw):
            captured["params"] = params
            return _FakeResp(200, self._fake_volume())
        monkeypatch.setattr(metadata.httpx, "get", fake_get)

        p = metadata.GoogleBooksProvider(api_key="k")
        p.search(title="某书", author="某作者", isbn="9787521226805")

        assert "isbn:9787521226805" in captured["params"]["q"]

    def test_no_isbn_returns_none(self, monkeypatch):
        """无 ISBN → 直接 None,不做书名/作者模糊查找(原则:只 ISBN 精确查)。"""
        called = []
        monkeypatch.setattr(metadata.httpx, "get",
                            lambda *a, **k: called.append(1) or _FakeResp(200, self._fake_volume()))
        p = metadata.GoogleBooksProvider(api_key="k")
        assert p.search(title="北京法源寺", author="李敖", isbn=None) is None
        assert called == [], "无 ISBN 时不应发任何网络请求"

    def test_empty_items_returns_none(self, monkeypatch):
        """Google 返回 totalItems=0 / 无 items → None(降级,不抛)。"""
        monkeypatch.setattr(metadata.httpx, "get",
                            lambda *a, **k: _FakeResp(200, {"totalItems": 0, "items": []}))
        p = metadata.GoogleBooksProvider(api_key="k")
        assert p.search(title="不存在的书", author=None, isbn=None) is None

    def test_network_error_returns_none_not_raise(self, monkeypatch):
        """网络异常/超时 → None,不抛(降级,不影响入库)。"""
        def boom(*a, **k):
            raise metadata.httpx.ConnectError("connection refused")
        monkeypatch.setattr(metadata.httpx, "get", boom)
        p = metadata.GoogleBooksProvider(api_key="k")
        assert p.search(title="x", author=None, isbn=None) is None

    def test_http_429_returns_none_not_raise(self, monkeypatch):
        """配额超限 429 → None,不抛。"""
        monkeypatch.setattr(metadata.httpx, "get",
                            lambda *a, **k: _FakeResp(429, {"error": {"code": 429}}))
        p = metadata.GoogleBooksProvider(api_key="k")
        assert p.search(title="x", author=None, isbn=None) is None


class _FakeResp:
    """mock httpx.Response。"""
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


# ---------- S2: provider 抽象 + 注册表 ----------

class TestProviderRegistry:
    """get_provider 注册表:扩展点(豆瓣日后挂入)。"""

    def test_get_google_books_provider(self):
        """get_provider('google_books') 返回 GoogleBooksProvider 实例。"""
        p = metadata.get_provider("google_books")
        assert isinstance(p, metadata.GoogleBooksProvider)
        assert p.name == "google_books"

    def test_unknown_provider_raises(self):
        """未知名抛 ValueError,不静默返回 None。"""
        import pytest
        with pytest.raises(ValueError):
            metadata.get_provider("不存在的源")

    def test_provider_is_subclass_of_protocol(self):
        """GoogleBooksProvider 是 MetadataProvider 子类(扩展点存在性)。"""
        assert issubclass(metadata.GoogleBooksProvider, metadata.MetadataProvider)


# ---------- S3: enrich_book 写回 DB ----------

class _FakeProvider:
    """可控的假 provider,返回预设 BookMeta 或 None。"""
    name = "fake"
    def __init__(self, result):
        self._result = result
        self.calls = []
    def search(self, title, author, isbn):
        self.calls.append((title, author, isbn))
        return self._result
    def find_isbn(self, title, author):
        # 默认不找 ISBN(显式 provider 测试用);两阶段测试用专门 mock
        return None


class TestEnrichBook:
    """enrich_book(book_id):调 provider → 写回 books + meta_status 状态机。

    mock provider,只测调度/写回/状态,不测 provider 内部。
    """

    def _seed_book(self, conn, title="北京法源寺", author="李敖",
                   isbn=None, summary=None, meta_status=None):
        """插一本原始书(epub 内嵌字段已有,待补外部元数据)。"""
        conn.execute(
            """INSERT INTO books(file_hash, title, author, original_path,
               epub_path, format, ingest_status, summary, isbn, meta_status)
               VALUES('h1', ?, ?, '/p', '/p', 'epub', 'ready', ?, ?, ?)""",
            (title, author, summary, isbn, meta_status),
        )
        return conn.execute("SELECT id FROM books WHERE file_hash='h1'").fetchone()["id"]

    def test_success_writes_fields_and_status_ok(self, tmp_path, monkeypatch):
        """provider 返回 BookMeta → 写回 books 新字段 + meta_status='ok'。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="北京法源寺", author="李敖")

        fake = _FakeProvider(metadata.make_bookmeta(
            title="北京法源寺", author="李敖", publisher="中国友谊出版公司",
            publish_date="2000", summary="历史小说", rating=4.5,
            rating_count=2, tags=["China"], isbn="9787505715486",
            page_count=304,
        ))
        metadata.enrich_book(bid, provider=fake)

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
        assert dict(row)["meta_status"] == "ok"
        assert dict(row)["summary"] == "历史小说"
        assert dict(row)["rating"] == 4.5
        assert dict(row)["publisher"] == "中国友谊出版公司"
        assert dict(row)["isbn"] == "9787505715486"
        assert dict(row)["meta_source"] == "fake"
        assert dict(row)["meta_fetched_at"] is not None

    def test_not_found_sets_status_not_found(self, tmp_path, monkeypatch):
        """provider 返回 None → meta_status='not_found',不覆盖已有字段。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, summary="epub原有简介")

        metadata.enrich_book(bid, provider=_FakeProvider(None))

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "not_found"
        assert row["summary"] == "epub原有简介"  # 不覆盖

    def test_provider_exception_sets_status_failed(self, tmp_path, monkeypatch):
        """provider 抛异常 → meta_status='failed' + meta_error,不影响书可读。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn)

        class BoomProvider:
            name = "boom"
            def search(self, title=None, author=None, isbn=None):
                raise RuntimeError("网络炸了")

        metadata.enrich_book(bid, provider=BoomProvider())

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "failed"
        assert "网络炸了" in (row["meta_error"] or "")
        assert row["ingest_status"] == "ready"  # 入库状态不受影响

    def test_already_enriched_skips(self, tmp_path, monkeypatch):
        """meta_status='ok' 的不重复拉(幂等,省配额)。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, summary="已有", meta_status="ok")

        fake = _FakeProvider(metadata.make_bookmeta(summary="新的不该写入"))
        metadata.enrich_book(bid, provider=fake)

        assert fake.calls == [], "已 ok 的书不应再调 provider"
        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT summary FROM books WHERE id=?", (bid,)).fetchone())
        assert row["summary"] == "已有"

    def test_merges_epub_fallback_for_empty_fields(self, tmp_path, monkeypatch):
        """Google 没给的字段(如 publisher),epub 内嵌已有则保留。

        实测《钦探》:Google 无 publisher,epub 有(作家出版社)。合并后应保留 epub 的。
        """
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            # epub 内嵌已有 publisher/isbn/summary
            conn.execute(
                """INSERT INTO books(file_hash,title,author,original_path,epub_path,
                   format,ingest_status,publisher,isbn,summary,meta_status)
                   VALUES('h1','钦探','周游','/p','/p','epub','ready',
                   '作家出版社','9787521226805','epub内嵌简介',NULL)"""
            )
            bid = conn.execute("SELECT id FROM books WHERE file_hash='h1'").fetchone()["id"]

        # Google 返回的 publisher=None(实测确实没给),但给了 tags
        fake = _FakeProvider(metadata.make_bookmeta(
            title="钦探", author="周游", publisher=None,
            tags=["China"], isbn="9787521226805",
        ))
        metadata.enrich_book(bid, provider=fake)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["publisher"] == "作家出版社"  # epub 内嵌兜底,不被 None 覆盖
        assert row["summary"] == "epub内嵌简介"  # epub 内嵌简介保留
        assert row["tags"] is not None  # Google 给的 tags 写入


# ---------- S6: WebSearchProvider (智谱 Web Search,AI 兜底) ----------

class TestWebSearchProviderParse:
    """Google Books 查不到时,用智谱 Web Search API 兜底补简介/评分。

    网络层 mock,测解析逻辑:把搜索结果摘要拼成简介 + 正则提取评分。
    """

    def _fake_websearch_resp(self, results):
        """构造智谱 Web Search API 响应。"""
        return {
            "id": "ws-1", "created": 1700000000,
            "search_result": results,
        }

    def test_parse_search_results_into_summary(self, monkeypatch):
        """搜索摘要经 GLM 解析后含简介。查询用书名+作者(不含 ISBN)。

        (旧正则版已弃用,简介/评分解析改由 GLM 负责,见 TestWebSearchGLMParse。
         此处只断言查询走书名+作者 + 解析链路通。)
        """
        captured = {}
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                captured["json"] = json
                return _FakeResp(200, self._fake_websearch_resp([
                    {"title": "北京法源寺-豆瓣", "content": "《北京法源寺》是李敖创作的长篇历史小说。",
                     "link": "", "media": "豆瓣"},
                ]))
            return _FakeResp(200, {"choices": [{"message": {"content":
                '```json\n{"summary":"李敖创作的长篇历史小说。","rating":null,"rating_count":null}\n```'}}]})
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        m = p.search(title="北京法源寺", author="李敖", isbn="9787505715486")

        assert m is not None
        assert "李敖" in m["summary"]
        assert m["meta_source_detail"] == "web_search"
        # 查询用书名+作者,不含 ISBN(v0.3:ISBN 干扰搜索,去掉)
        q = captured["json"]["search_query"]
        assert "北京法源寺" in q
        assert "李敖" in q
        assert "9787505715486" not in q

    def test_no_title_returns_none(self, monkeypatch):
        """无书名 → None(没书名没法定向搜索)。ISBN 有无不影响。"""
        called = []
        monkeypatch.setattr(metadata.httpx, "post",
                            lambda *a, **k: called.append(1) or _FakeResp(200, self._fake_websearch_resp([
                                {"title": "x", "content": "s", "link": "", "media": ""}
                            ])))
        p = metadata.WebSearchProvider(api_key="k")
        # 无书名 → 不搜,返回 None(即使有 ISBN 也不搜,书名是定向锚)
        assert p.search(title=None, author="李敖", isbn="9787505715486") is None
        assert called == [], "无书名时不应发任何网络请求"

    def test_empty_results_returns_none(self, monkeypatch):
        """搜索无结果 → None(降级)。"""
        monkeypatch.setattr(metadata.httpx, "post", lambda url, json=None, **kw: _FakeResp(200, self._fake_websearch_resp([])))
        p = metadata.WebSearchProvider(api_key="k")
        assert p.search(title="x", author=None, isbn="9787521226805") is None

    def test_network_error_returns_none(self, monkeypatch):
        """网络异常 → None,不抛。"""
        def boom(*a, **k):
            raise metadata.httpx.ConnectError("refused")
        monkeypatch.setattr(metadata.httpx, "post", boom)
        p = metadata.WebSearchProvider(api_key="k")
        assert p.search(title="x", author=None, isbn="9787521226805") is None

    # 查询内容(含书名+作者+ISBN+豆瓣)的断言见 TestWebSearchGLMParse,
    # 旧"纯 ISBN 不含书名"断言已随查询策略调整移除(书名帮搜索定位,非模糊猜书)。


# ---------- S7: GLM 解析豆瓣摘要(替代正则,优先数据质量) ----------

class TestWebSearchGLMParse:
    """WebSearchProvider 用 GLM 大模型解析搜索摘要成简介+评分。

    优先数据质量:正则易漏提/误提(评分人数格式多变、简介含噪),
    GLM 能理解上下文,稳定提出评分和干净的简介。
    流程:WebSearch 摘要 → GLM → 结构化 JSON → BookMeta。
    """

    def _websearch_resp(self, contents):
        """构造 Web Search 响应(几条 content)。"""
        return {"id": "ws", "created": 1,
                "search_result": [{"title": "x", "content": c, "link": "", "media": "豆瓣"}
                                  for c in contents]}

    def _glm_resp(self, content_text):
        """构造 GLM chat completions 响应。"""
        return {"id": "c", "created": 1, "model": "glm-5v-turbo",
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                      "content": content_text}}]}

    def test_glm_parses_rating_and_summary_from_douban(self, monkeypatch):
        """GLM 把豆瓣摘要解析成简介+评分。

        实测摘要含"豆瓣评分:8.7"和一段内容简介,GLM 返回 JSON(常包在
        ```json 代码块里),须剥壳后解析出 rating/summary。
        """
        calls = []
        def fake_post(url, json=None, **kw):
            calls.append(json)
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp([
                    "《真希望我父母读过这本书》豆瓣评分：8.7 1234人评价 "
                    "父母与孩子之间的关系，对孩子一生有着深远的影响。",
                ]))
            # GLM chat completions
            return _FakeResp(200, self._glm_resp(
                "```json\n{\"summary\": \"父母与孩子之间的关系对孩子一生有深远影响。\", "
                "\"rating\": 8.7, \"rating_count\": 1234}\n```"))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        m = p.search(title="真希望我父母读过这本书", author="菲利帕·佩里",
                     isbn="9787521719253")
        assert m is not None
        assert m["rating"] == 8.7
        assert m["rating_count"] == 1234
        assert "父母与孩子" in m["summary"]
        assert m["meta_source_detail"] == "web_search"

    def test_glm_returns_null_rating_when_absent(self, monkeypatch):
        """摘要里没评分 → GLM 返回 null,不强造评分。"""
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp(["这是一本小说的简介,没提评分。"]))
            return _FakeResp(200, self._glm_resp(
                '```json\n{"summary": "一本小说。", "rating": null, "rating_count": null}\n```'))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        m = p.search(title="x", author=None, isbn="9787521719253")
        assert m is not None
        assert m["rating"] is None
        assert m["rating_count"] is None
        assert m["summary"]  # 仍有简介

    def test_glm_json_without_codeblock_still_parses(self, monkeypatch):
        """GLM 偶尔不包代码块,直接返回裸 JSON,也要能解析。"""
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp(["某书简介"]))
            return _FakeResp(200, self._glm_resp(
                '{"summary": "某书简介。", "rating": 9.0, "rating_count": 500}'))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        m = p.search(title="x", author=None, isbn="9787521719253")
        assert m is not None
        assert m["rating"] == 9.0
        assert m["rating_count"] == 500

    def test_glm_malformed_json_degrades_to_none(self, monkeypatch):
        """GLM 返回非合法 JSON(如带解释文字)→ 解析失败 → None(降级到 Google)。

        数据质量优先:解析不出干净的结构化数据,宁可返回 None 让兜底链处理,
        也不存半成品。
        """
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp(["某书"]))
            return _FakeResp(200, self._glm_resp(
                "抱歉,我无法从摘要中提取信息,因为它不完整。"))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        assert p.search(title="x", author=None, isbn="9787521719253") is None

    def test_glm_api_error_degrades_to_none(self, monkeypatch):
        """GLM 接口异常(超时/非200)→ None,不抛(降级到 Google)。"""
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp(["某书"]))
            return _FakeResp(500, {"error": "glm down"})
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        assert p.search(title="x", author=None, isbn="9787521719253") is None

    def test_glm_prompt_includes_isbn_and_title(self, monkeypatch):
        """GLM 的 prompt 应含书名(帮模型定位)和摘要内容。"""
        captured_glm = {}
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp(["豆瓣评分8.5"]))
            captured_glm["prompt"] = json["messages"][0]["content"]
            return _FakeResp(200, self._glm_resp(
                '```json\n{"summary":"s","rating":8.5,"rating_count":null}\n```'))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        p.search(title="北京法源寺", author="李敖", isbn="9787505715486")
        prompt = captured_glm["prompt"]
        assert "北京法源寺" in prompt  # 书名帮模型理解
        assert "豆瓣评分8.5" in prompt  # 摘要原文传入

    def test_query_is_title_author_douban_no_isbn(self, monkeypatch):
        """查询用 {书名} {作者} 豆瓣 简介 评分,不含 ISBN。

        实测纯 ISBN/含 ISBN 搜索常返回无关摘要(ISBN 当字符串匹配到无关页面,
        如搜《人有人的用处》ISBN 却返回韩国电影)。去掉 ISBN,用书名+作者+
        豆瓣定向,GLM 校验摘要确实是本书。ISBN 不进查询(干扰),但 DB 保留。
        """
        captured = {}
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                captured["q"] = json["search_query"]
                return _FakeResp(200, self._websearch_resp(["某书简介"]))
            return _FakeResp(500, {"error": "skip glm"})
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        p.search(title="人有人的用处", author="N. 维纳", isbn="9787100005166")
        q = captured["q"]
        assert "人有人的用处" in q  # 书名
        assert "维纳" in q  # 作者
        assert "豆瓣" in q  # 意图词:定向豆瓣数据
        assert "9787100005166" not in q  # ISBN 不进查询(干扰搜索)

    def test_glm_rejects_unrelated_summary(self, monkeypatch):
        """摘要与书无关(如搜到电影)→ GLM 应判定无关,返回 null → search 返回 None。

        数据质量优先:宁可 not_found 也不存垃圾简介。
        实测《人有人的用处》ISBN 搜出韩国电影摘要,GLM 须识别无关并拒绝。
        """
        def fake_post(url, json=None, **kw):
            if "web_search" in url:
                return _FakeResp(200, self._websearch_resp([
                    "《格斗少年,菀得》是一部韩国剧情片,由李翰执导,金允石、刘亚仁主演。"
                    "影片讲述了问题少年菀得与老师东洙之间的故事。",
                ]))
            # GLM 判定摘要与《人有人的用处》无关,返回 null
            return _FakeResp(200, self._glm_resp(
                '```json\n{"summary": null, "rating": null, "rating_count": null}\n```'))
        monkeypatch.setattr(metadata.httpx, "post", fake_post)

        p = metadata.WebSearchProvider(api_key="k")
        # 摘要全是无关电影内容 → summary 为 null → search 返回 None(降级)
        assert p.search(title="人有人的用处", author="维纳",
                        isbn="9787100005166") is None

class TestEnrichFallback:
    """enrich_book 的兜底链:Google not_found → WebSearch 兜底。"""

    def _seed_book(self, conn, title="x", author=None, isbn=None):
        conn.execute(
            """INSERT INTO books(file_hash,title,author,original_path,epub_path,
               format,ingest_status,isbn,meta_status)
               VALUES('h1',?,?, '/p','/p','epub','ready',?,NULL)""",
            (title, author, isbn),
        )
        return conn.execute("SELECT id FROM books WHERE file_hash='h1'").fetchone()["id"]

    def test_zhipu_hits_google_not_called(self, tmp_path, monkeypatch):
        """有 ISBN:智谱(WebSearch)优先命中 → 用智谱结果,Google 不再调用。

        v0.3 优先级:智谱搜豆瓣+GLM 解析优先(数据质量),Google 降为兜底。
        智谱给了结果就不再调 Google(省配额、避免 Google 弱数据覆盖)。
        """
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="某书", author="某作者",
                                  isbn="9787521226805")

        import app.metadata as meta_mod
        google = _FakeProvider(meta_mod.make_bookmeta(summary="不该用Google"))
        websearch = _FakeProvider(meta_mod.make_bookmeta(
            summary="智谱豆瓣简介", rating=8.7,
            meta_source_detail="web_search"))
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: google if name == "google_books" else websearch)

        meta_mod.enrich_book(bid)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "ok"
        assert row["summary"] == "智谱豆瓣简介"
        assert row["meta_source"] == "web_search"
        assert row["rating"] == 8.7
        # 智谱命中 → Google 未被调用
        assert google.calls == [], "智谱命中后不应再调 Google"

    def test_zhipu_none_falls_back_to_google(self, tmp_path, monkeypatch):
        """有 ISBN:智谱返回 None → Google 用同 ISBN 兜底,成功则 ok。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="某书", author="某作者",
                                  isbn="9787521226805")

        import app.metadata as meta_mod
        websearch = _FakeProvider(None)
        google = _FakeProvider(meta_mod.make_bookmeta(
            summary="Google兜底简介", rating=4.0))
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: google if name == "google_books" else websearch)

        meta_mod.enrich_book(bid)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "ok"
        assert row["summary"] == "Google兜底简介"
        assert row["meta_source"] == "fake"  # _FakeProvider.name
        # 智谱先查(返回 None),Google 兜底;两者用同 ISBN
        assert websearch.calls[0][2] == "9787521226805"
        assert google.calls[0][2] == "9787521226805"

    def test_no_isbn_still_searches_by_title(self, tmp_path, monkeypatch):
        """无 ISBN 也能搜:单阶段用书名+作者搜,不依赖 ISBN。

        v0.3 改动:查询去 ISBN(实测 ISBN 干扰搜索),无 ISBN 不再走 find_isbn,
        直接用书名+作者+豆瓣搜。智谱命中即 ok。
        """
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="北京法源寺", author="李敖", isbn=None)

        import app.metadata as meta_mod
        google = _FakeProvider(meta_mod.make_bookmeta(summary="不该用Google"))
        websearch = _FakeProvider(meta_mod.make_bookmeta(
            summary="智谱豆瓣简介", rating=8.4,
            meta_source_detail="web_search"))
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: google if name == "google_books" else websearch)

        meta_mod.enrich_book(bid)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "ok"
        assert row["summary"] == "智谱豆瓣简介"
        assert row["rating"] == 8.4
        # 无 ISBN 不影响搜索;Google 未被调用(智谱命中)
        assert google.calls == []
        # 智谱用书名+作者调( isbn=None 也照搜)
        assert websearch.calls[0][0] == "北京法源寺"
        assert websearch.calls[0][1] == "李敖"

    def test_no_isbn_both_fail_sets_not_found(self, tmp_path, monkeypatch):
        """无 ISBN 且智谱/Google 都返回 None → meta_status='not_found'。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="神秘书", author="佚名", isbn=None)

        import app.metadata as meta_mod
        google = _FakeProvider(None)
        class FakeWs:
            name = "web_search"
            # search 签名须与 enrich_book 调用一致(关键字传 title/author/isbn)
            def search(self, title, author, isbn): return None
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: google if name == "google_books" else FakeWs())
        meta_mod.enrich_book(bid)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT meta_status, isbn FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "not_found"
        assert row["isbn"] is None

    def test_both_fail_sets_not_found(self, tmp_path, monkeypatch):
        """有 ISBN 但 Google 和 WebSearch 都 None → meta_status='not_found'。"""
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            bid = self._seed_book(conn, title="x", author="y", isbn="9787521226805")

        import app.metadata as meta_mod
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: _FakeProvider(None))
        meta_mod.enrich_book(bid)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT meta_status FROM books WHERE id=?", (bid,)).fetchone())
        assert row["meta_status"] == "not_found"

    def test_ok_but_missing_summary_uses_websearch_to_fill(self, tmp_path, monkeypatch):
        """meta_status=ok 但缺 summary → 用 WebSearch 补缺失字段。

        场景:Google 给了 rating/tags 但没简介(如《北京法源寺》)。
        此时 meta_status 已 ok,但 summary 空 → WebSearch 补 summary,
        不覆盖 Google 已给的字段(rating 等)。
        """
        from app import db
        db.DB_PATH = tmp_path / "t.db"
        db.init_db()
        with db.db() as conn:
            # Google 已 ok:有 rating,无 summary
            conn.execute(
                """INSERT INTO books(file_hash,title,author,original_path,epub_path,
                   format,ingest_status,rating,summary,meta_status,meta_source)
                   VALUES('h1','北京法源寺','李敖','/p','/p','epub','ready',
                   4.5,NULL,'ok','google_books')"""
            )
            bid = conn.execute("SELECT id FROM books WHERE file_hash='h1'").fetchone()["id"]

        import app.metadata as meta_mod
        # _fill_missing_with_websearch 直接调 get_provider("web_search")
        websearch = _FakeProvider(meta_mod.make_bookmeta(
            summary="WebSearch补的简介", rating=None,
            meta_source_detail="web_search"))
        monkeypatch.setattr(meta_mod, "get_provider",
                            lambda name: websearch)

        meta_mod.enrich_book(bid, fill_missing=True)

        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        assert row["summary"] == "WebSearch补的简介"  # WebSearch 补上了
        assert row["rating"] == 4.5  # Google 的 rating 没被覆盖
        assert row["meta_source"] == "google_books"  # 主源不变
