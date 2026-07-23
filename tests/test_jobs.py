"""测试每日批处理:生成 knowledge + blind_spot + recommendation 卡片。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import db


def _seed_reading_log(book_id=1, text="苏格拉底在雅典广场与青年对话..."):
    """插入一条 reading_log 作为原料。"""
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO books(id, file_hash, title, original_path) VALUES(?,?,?,?)",
            (book_id, "hash1", "测试书", "/tmp/test.epub"),
        )
        conn.execute(
            """INSERT INTO reading_log(book_id, start_cfi, end_cfi, text,
               percent_from, percent_to)
               VALUES(?,?,?,?,?,?)""",
            (book_id, "cfi0", "cfi1", text, 0.1, 0.5),
        )


def _mock_glm_response(knowledge_count=5, blind_count=5):
    """构造 GLM 返回的 JSON(模拟步骤 1 输出)。"""
    kcs = []
    for i in range(knowledge_count):
        kcs.append({
            "card_type": "knowledge",
            "title": f"知识点{i+1}",
            "body": f"这是第{i+1}个知识点的解释",
        })
    for i in range(blind_count):
        kcs.append({
            "card_type": "blind_spot",
            "title": f"盲点{i+1}",
            "body": f"这是第{i+1}个盲点的解释",
        })
    return kcs


def _mock_websearch_response(title="推荐书", reason="基于知识点X"):
    """构造智谱 Web Search 返回的 JSON(模拟搜索结果)。"""
    return {
        "search_result": [
            {
                "content": f"{title} — 介绍:这是一本关于{title}的书,评分8.5",
                "title": title,
            }
        ]
    }


def _mock_glm_parse_recommendation(title="推荐书", reason="基于知识点"):
    """构造 GLM 解析推荐书后的 JSON。"""
    return {
        "title": title,
        "author": "某作者",
        "reason": reason,
        "summary": f"这是{title}的简介。" + "这本书深入探讨了相关主题，为读者提供了全新的视角和理解框架。内容包括核心概念阐述、案例分析、以及实践指导。" * 3,
        "isbn": "9780000000001",
    }


class TestDailyJobGenerateCards:
    """步骤 1:GLM 生成 5 knowledge + 5 blind_spot。"""

    def test_generate_knowledge_and_blindspots_count(self):
        """应产出正好 5 knowledge + 5 blind_spot。"""
        _seed_reading_log()
        from app.jobs import _generate_knowledge_and_blindspots

        with patch("app.jobs._call_glm") as mock_glm:
            mock_glm.return_value = json.dumps(_mock_glm_response(5, 5))
            cards = _generate_knowledge_and_blindspots()

        assert len(cards) == 10
        k = [c for c in cards if c["card_type"] == "knowledge"]
        b = [c for c in cards if c["card_type"] == "blind_spot"]
        assert len(k) == 5
        assert len(b) == 5

    def test_knowledge_card_has_required_fields(self):
        """knowledge 卡片应有 title/body/book_id/source_type/source_ids。"""
        _seed_reading_log()
        from app.jobs import _generate_knowledge_and_blindspots

        with patch("app.jobs._call_glm") as mock_glm:
            mock_glm.return_value = json.dumps(_mock_glm_response(1, 0))
            cards = _generate_knowledge_and_blindspots()

        k = cards[0]
        assert k["card_type"] == "knowledge"
        assert k["title"]
        assert k["body"]
        assert k["book_id"] == 1
        assert k["source_type"] == "reading_log"
        assert k["source_ids"] == [1]

    def test_blind_spot_card_no_book_id(self):
        """blind_spot 卡片 book_id 应为 NULL。"""
        _seed_reading_log()
        from app.jobs import _generate_knowledge_and_blindspots

        with patch("app.jobs._call_glm") as mock_glm:
            mock_glm.return_value = json.dumps(_mock_glm_response(0, 1))
            cards = _generate_knowledge_and_blindspots()

        b = cards[0]
        assert b["card_type"] == "blind_spot"
        assert "book_id" not in b or b.get("book_id") is None

    def test_no_reading_log_returns_empty(self):
        """没有 reading_log 时返回空列表(不调 GLM)。"""
        from app.jobs import _generate_knowledge_and_blindspots

        cards = _generate_knowledge_and_blindspots()
        assert cards == []

    def test_glm_error_logs_and_returns_empty(self, capsys):
        """GLM 调用失败时日志告警,返回空列表,不崩溃。"""
        _seed_reading_log()
        from app.jobs import _generate_knowledge_and_blindspots

        with patch("app.jobs._call_glm", side_effect=Exception("API timeout")):
            cards = _generate_knowledge_and_blindspots()

        assert cards == []
        captured = capsys.readouterr()
        assert "API timeout" in captured.out or "API timeout" in captured.err


class TestDailyJobRecommendations:
    """步骤 2:为每张 knowledge/blind_spot 生成 1 张 recommendation。"""

    def test_recommendation_count(self):
        """10 张父卡片 → 10 张 recommendation。"""
        from app.jobs import _generate_recommendations

        parents = [
            {"id": 1, "card_type": "knowledge", "title": "K1", "body": "内容"},
            {"id": 2, "card_type": "blind_spot", "title": "B1", "body": "盲点"},
        ]
        with patch("app.jobs._search_book") as mock_search:
            mock_search.return_value = _mock_glm_parse_recommendation(
                "推荐书", "基于知识点的推荐理由"
            )
            cards = _generate_recommendations(parents)

        assert len(cards) == 2
        assert all(c["card_type"] == "recommendation" for c in cards)

    def test_recommendation_has_parent_card_id(self):
        """每张 recommendation 应有 parent_card_id 指向父卡。"""
        from app.jobs import _generate_recommendations

        parents = [{"id": 42, "card_type": "knowledge", "title": "K1", "body": "内容"}]
        with patch("app.jobs._search_book") as mock_search:
            mock_search.return_value = _mock_glm_parse_recommendation(
                "推荐书", "基于「K1」的推荐理由"
            )
            cards = _generate_recommendations(parents)

        assert cards[0]["parent_card_id"] == 42

    def test_recommendation_has_recommend_book_json(self):
        """每张 recommendation 应有 recommend_book JSON 含所有必要字段。"""
        from app.jobs import _generate_recommendations

        parents = [{"id": 1, "card_type": "knowledge", "title": "K1", "body": "内容"}]
        with patch("app.jobs._search_book") as mock_search:
            mock_search.return_value = _mock_glm_parse_recommendation(
                "某书", "基于「K1」的推荐"
            )
            cards = _generate_recommendations(parents)

        rb = json.loads(cards[0]["recommend_book"])
        assert rb["title"] == "某书"
        assert rb["author"] == "某作者"
        assert "K1" in rb["reason"]
        assert len(rb["summary"]) >= 50
        assert rb["isbn"]

    def test_search_book_failure_skips(self):
        """某本书搜索失败时跳过该推荐,不影响其他。"""
        from app.jobs import _generate_recommendations

        parents = [
            {"id": 1, "card_type": "knowledge", "title": "K1", "body": "1"},
            {"id": 2, "card_type": "knowledge", "title": "K2", "body": "2"},
        ]
        call_count = [0]

        def flaky_search(parent):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("search failed")
            return _mock_glm_parse_recommendation(f"书{call_count[0]}", f"基于{parent['title']}")

        with patch("app.jobs._search_book", side_effect=flaky_search):
            cards = _generate_recommendations(parents)

        # 第一张搜索失败跳过,第二张成功
        assert len(cards) == 1
        assert cards[0]["parent_card_id"] == 2


class TestDailyJobFullFlow:
    """完整流程:run_daily_job() 集成测试。"""

    def test_full_flow_inserts_20_cards(self):
        """全流程应入库 20 张卡片(5+5+10)。"""
        _seed_reading_log()
        from app.jobs import run_daily_job

        with patch("app.jobs._call_glm") as mock_glm, \
             patch("app.jobs._search_book") as mock_search:
            mock_glm.return_value = json.dumps(_mock_glm_response(5, 5))
            mock_search.return_value = _mock_glm_parse_recommendation(
                "推荐书", "基于卡片的推荐理由"
            )
            ids = run_daily_job()

        assert len(ids) == 20
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0]
        assert count == 20

    def test_full_flow_no_reading_log(self, capsys):
        """无 reading_log 时:不生成卡片,不崩溃。"""
        from app.jobs import run_daily_job

        ids = run_daily_job()
        output = capsys.readouterr().out
        assert ids == []
        assert "[readflow] daily_cards input: reading_logs=0 highlights=0" in output
        assert "[readflow] daily_cards skipped: no recent reading input" in output

    def test_full_flow_reports_created_count(self, capsys):
        from app.jobs import run_daily_job

        cards = [{"card_type": "knowledge", "title": "T", "body": "B"}]
        with patch(
            "app.jobs._generate_knowledge_and_blindspots",
            return_value=cards,
        ), patch("app.jobs._generate_recommendations", return_value=[]):
            ids = run_daily_job()
        output = capsys.readouterr().out
        assert len(ids) == 1
        assert "[readflow] daily_cards created: cards=1" in output


class TestDailyJobRetry:
    """API 调用串行、有间隔、失败重试 2 次。"""

    def test_do_websearch_retries_on_failure(self):
        """_do_websearch 调用失败应重试 2 次(共 3 次 HTTP 请求)。"""
        import app.jobs as jobs_mod
        import httpx

        attempts = [0]

        def _flaky_post(*args, **kwargs):
            attempts[0] += 1
            raise httpx.HTTPError("rate limited")

        with patch.object(jobs_mod.httpx, "post", side_effect=_flaky_post), \
             patch.object(jobs_mod.time, "sleep"):  # 不真等
            try:
                jobs_mod._do_websearch("test query")
            except Exception:
                pass

        assert attempts[0] == 3  # 1 original + 2 retries = 3 attempts

    def test_call_glm_no_retry_on_failure(self):
        """_call_glm 失败应只调 1 次,不重试(偶发 ReadTimeout 重试无意义)。"""
        import app.jobs as jobs_mod
        import httpx

        attempts = [0]

        def _flaky_post(*args, **kwargs):
            attempts[0] += 1
            raise httpx.ReadTimeout("read timed out")

        with patch.object(jobs_mod.httpx, "post", side_effect=_flaky_post), \
             patch.object(jobs_mod.time, "sleep"):  # 即便误重试也不真等
            try:
                jobs_mod._call_glm("test prompt")
            except Exception:
                pass

        assert attempts[0] == 1  # 只调 1 次,不重试

    def test_call_glm_timeout_is_300(self):
        """_call_glm 应把 timeout=300 传给 httpx(GLM 偶发慢响应,需长 read timeout)。"""
        import app.jobs as jobs_mod

        captured = {}

        class _FakeResponse:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": "[]"}}]}

        def _capture_post(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResponse()

        with patch.object(jobs_mod.httpx, "post", side_effect=_capture_post):
            jobs_mod._call_glm("test prompt")

        assert captured["timeout"] == 300.0
