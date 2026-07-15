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
    """卡片流:按时间倒序,支持按 card_type 筛选 + 关键词搜索 title/body。

    recommendation 卡片额外带 parent_title(JOIN 父卡片),供前端显著标注
    它推进的盲点/知识点标题。
    """
    # 基础查询:kc.* + 父卡片标题+类型(仅 recommendation 有 parent_card_id)
    base_select = """SELECT kc.*, p.title AS parent_title, p.card_type AS parent_card_type
                     FROM knowledge_cards kc
                     LEFT JOIN knowledge_cards p ON p.id = kc.parent_card_id"""
    order_limit = "ORDER BY kc.created_at DESC LIMIT ? OFFSET ?"

    with db.get_conn() as conn:
        if q:
            pattern = f"%{q}%"
            if card_type:
                rows = conn.execute(
                    f"""{base_select}
                       WHERE kc.card_type = ? AND (kc.title LIKE ? OR kc.body LIKE ?)
                       {order_limit}""",
                    (card_type, pattern, pattern, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""{base_select}
                       WHERE kc.title LIKE ? OR kc.body LIKE ?
                       {order_limit}""",
                    (pattern, pattern, limit, offset),
                ).fetchall()
        elif card_type:
            rows = conn.execute(
                f"""{base_select}
                   WHERE kc.card_type = ?
                   {order_limit}""",
                (card_type, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""{base_select}
                   {order_limit}""",
                (limit, offset),
            ).fetchall()

    return [dict(r) for r in rows]