"""知识卡片流:列表 + 筛选 + 搜索。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/cards")
def list_cards(
    card_type: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """卡片流:按时间倒序,支持按 card_type 筛选 + 关键词搜索 title/body。"""
    with db.get_conn() as conn:
        if q:
            pattern = f"%{q}%"
            if card_type:
                rows = conn.execute(
                    """SELECT * FROM knowledge_cards
                       WHERE card_type = ? AND (title LIKE ? OR body LIKE ?)
                       ORDER BY created_at DESC
                       LIMIT ? OFFSET ?""",
                    (card_type, pattern, pattern, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM knowledge_cards
                       WHERE title LIKE ? OR body LIKE ?
                       ORDER BY created_at DESC
                       LIMIT ? OFFSET ?""",
                    (pattern, pattern, limit, offset),
                ).fetchall()
        elif card_type:
            rows = conn.execute(
                """SELECT * FROM knowledge_cards
                   WHERE card_type = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (card_type, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM knowledge_cards
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()

    return [dict(r) for r in rows]