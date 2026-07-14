"""测试 knowledge_cards 表 + GET /api/knowledge/cards 卡片流 API。
"""
from __future__ import annotations

import pytest

from app import db, ingest
from tests.conftest import _first_sample


class TestKnowledgeCardsTable:
    """knowledge_cards 表应正确创建，可写入/查询三种 card_type。"""

    def test_knowledge_cards_table_exists(self):
        """SCHEMA 应包含 knowledge_cards 表。"""
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_cards'"
            ).fetchone()
        assert row is not None, "knowledge_cards 表应该存在"

    def test_knowledge_cards_insert_knowledge(self):
        """可写入 knowledge 类型卡片。"""
        with db.db() as conn:
            conn.execute(
                """INSERT INTO books(file_hash, title, original_path)
                   VALUES('kc_test', '测试', '/tmp/test.epub')"""
            )
            row = conn.execute("SELECT id FROM books WHERE file_hash='kc_test'").fetchone()
            book_id = row["id"]

            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, book_id,
                   source_type, source_ids, created_at)
                   VALUES(?,?,?,?,?,?,datetime('now'))""",
                ("knowledge", "测试知识点", "这是一段知识解释", book_id,
                 "reading_log", '[1,2]'),
            )

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_cards WHERE card_type='knowledge'"
            ).fetchone()
        assert row is not None
        assert row["title"] == "测试知识点"
        assert row["body"] == "这是一段知识解释"
        assert row["book_id"] == book_id
        assert row["source_ids"] == '[1,2]'

    def test_knowledge_cards_insert_blind_spot(self):
        """可写入 blind_spot 类型卡片(无 book_id)。"""
        with db.db() as conn:
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, created_at)
                   VALUES(?,?,?,datetime('now'))""",
                ("blind_spot", "盲点：缺少XX", "你缺了这块知识"),
            )

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_cards WHERE card_type='blind_spot'"
            ).fetchone()
        assert row is not None
        assert row["title"] == "盲点：缺少XX"
        assert row["book_id"] is None

    def test_knowledge_cards_insert_recommendation(self):
        """可写入 recommendation 类型卡片(含 recommend_book JSON + parent_card_id)。"""
        with db.db() as conn:
            # 先建 knowledge
            conn.execute(
                """INSERT INTO books(file_hash, title, original_path)
                   VALUES('rc_test', '推荐测试', '/tmp/test2.epub')"""
            )
            row = conn.execute(
                "SELECT id FROM books WHERE file_hash='rc_test'"
            ).fetchone()
            book_id = row["id"]

            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, book_id,
                   source_type, source_ids, created_at)
                   VALUES(?,?,?,?,?,?,datetime('now'))""",
                ("knowledge", "父卡片", "内容", book_id, "reading_log", '[1]'),
            )
            parent_id = conn.execute(
                "SELECT id FROM knowledge_cards WHERE title='父卡片'"
            ).fetchone()["id"]

            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body,
                   parent_card_id, recommend_book, created_at)
                   VALUES(?,?,?,?,?,datetime('now'))""",
                ("recommendation", "荐书", "推荐理由",
                 parent_id, '{"title":"XX","author":"某","reason":"基于知识点","summary":"简介","isbn":"123"}'),
            )

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_cards WHERE card_type='recommendation'"
            ).fetchone()
        assert row is not None
        assert row["parent_card_id"] == parent_id
        assert "XX" in (row["recommend_book"] or "")


class TestKnowledgeCardsAPI:
    """GET /api/knowledge/cards — 卡片流 + 筛选 + 搜索。"""

    def _seed_card(self, card_type, title, body, book_id=None):
        with db.db() as conn:
            conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, book_id, created_at)
                   VALUES(?,?,?,?,datetime('now'))""",
                (card_type, title, body, book_id),
            )

    def test_list_all_cards(self, client):
        """无筛选时返回所有卡片，按时间倒序。"""
        self._seed_card("knowledge", "K1", "内容1")
        self._seed_card("blind_spot", "B1", "盲点1")
        r = client.get("/api/knowledge/cards")
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) == 2
        # 后插入的先返回
        assert cards[0]["title"] == "B1"

    def test_filter_by_card_type(self, client):
        """?card_type=knowledge 只返回 knowledge 卡片。"""
        self._seed_card("knowledge", "K1", "内容")
        self._seed_card("knowledge", "K2", "内容2")
        self._seed_card("blind_spot", "B1", "盲点")
        r = client.get("/api/knowledge/cards?card_type=knowledge")
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) == 2
        assert all(c["card_type"] == "knowledge" for c in cards)

    def test_search_by_title(self, client):
        """?q=关键词 搜索 title + body。"""
        self._seed_card("knowledge", "幸存者偏差", "二战统计")
        self._seed_card("knowledge", "回归均值", "高尔顿")
        r = client.get("/api/knowledge/cards?q=幸存者")
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) >= 1
        assert any("幸存者" in (c["title"] + c["body"]) for c in cards)

    def test_search_no_results(self, client):
        """无匹配关键词时返回空列表。"""
        self._seed_card("knowledge", "测试", "内容")
        r = client.get("/api/knowledge/cards?q=不存在的关键词xyz")
        assert r.status_code == 200
        assert r.json() == []

    def test_empty_list(self, client):
        """无卡片时返回空列表。"""
        r = client.get("/api/knowledge/cards")
        assert r.status_code == 200
        assert r.json() == []