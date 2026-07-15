"""知识卡片流:列表 + 筛选 + 搜索 + 按日期分页。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/dates")
def list_dates(card_type: str | None = Query(None)):
    """有卡片的日期列表(去重 + 倒序)。

    可选 ?card_type= 只看某类型卡片的日期。
    返回 ["2026-07-15", "2026-07-14", ...]。
    """
    with db.get_conn() as conn:
        if card_type:
            rows = conn.execute(
                """SELECT DISTINCT date(created_at) AS d
                   FROM knowledge_cards
                   WHERE card_type = ?
                   ORDER BY d DESC""",
                (card_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DISTINCT date(created_at) AS d
                   FROM knowledge_cards
                   ORDER BY d DESC"""
            ).fetchall()
    return [r["d"] for r in rows]


@router.get("/cards")
def list_cards(
    card_type: str | None = Query(None),
    date: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """卡片流:按时间倒序,支持按 card_type/日期筛选 + 关键词搜索 title/body。

    ?date=YYYY-MM-DD 按日期筛选(配合前端按日期分页)。
    recommendation 卡片额外带 parent_title/parent_card_type(JOIN 父卡片),
    供前端显著标注推荐类型 + 关联的盲点/知识点标题。
    """
    # 基础查询:kc.* + 父卡片标题+类型(仅 recommendation 有 parent_card_id)
    base_select = """SELECT kc.*, p.title AS parent_title, p.card_type AS parent_card_type
                     FROM knowledge_cards kc
                     LEFT JOIN knowledge_cards p ON p.id = kc.parent_card_id"""
    order_limit = "ORDER BY kc.created_at DESC LIMIT ? OFFSET ?"

    # 组装 WHERE 条件
    where = []
    params = []
    if card_type:
        where.append("kc.card_type = ?")
        params.append(card_type)
    if date:
        where.append("date(kc.created_at) = ?")
        params.append(date)
    if q:
        where.append("(kc.title LIKE ? OR kc.body LIKE ?)")
        pattern = f"%{q}%"
        params.extend([pattern, pattern])
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db.get_conn() as conn:
        rows = conn.execute(
            f"{base_select} {where_clause} {order_limit}",
            (*params, limit, offset),
        ).fetchall()

    return [dict(r) for r in rows]