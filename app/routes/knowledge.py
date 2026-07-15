"""知识卡片流:列表 + 筛选 + 搜索 + 按日期分页。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/dates")
def list_dates(
    card_type: str | None = Query(None),
    month: str | None = Query(None),
):
    """有卡片的日期列表。两种模式:

    1. ?month=YYYY-MM:返回该月的天级日期列表(倒序去重)
       → 点月展开天用。返回 ["2026-06-15", "2026-06-10", ...]
    2. 无 month:返回分组结构 {this_week, older_months}
       - this_week:最近 7 天的天级日期(倒序),直达
       - older_months:更早的月份(倒序),每项 {month, count},点月再展开天
       → 避免日期多了 chip 太乱:本周直达,更早按月折叠。
    """
    type_clause = "WHERE card_type = ?" if card_type else ""
    type_params = (card_type,) if card_type else ()

    with db.get_conn() as conn:
        if month:
            # 模式 1:某月的天级日期
            rows = conn.execute(
                f"""SELECT DISTINCT date(created_at) AS d
                    FROM knowledge_cards
                    {type_clause + " AND" if card_type else "WHERE"}
                    strftime('%Y-%m', created_at) = ?
                    ORDER BY d DESC""",
                (*type_params, month),
            ).fetchall() if card_type else conn.execute(
                """SELECT DISTINCT date(created_at) AS d
                   FROM knowledge_cards
                   WHERE strftime('%Y-%m', created_at) = ?
                   ORDER BY d DESC""",
                (month,),
            ).fetchall()
            return [r["d"] for r in rows]

        # 模式 2:分组(本周 + 更早月级)
        # 本周 = 最近 7 天(滚动窗口,直觉优于周一/周日边界)
        if card_type:
            week_rows = conn.execute(
                """SELECT DISTINCT date(created_at) AS d
                   FROM knowledge_cards
                   WHERE card_type = ? AND date(created_at) >= date('now', '-6 days')
                   ORDER BY d DESC""",
                (card_type,),
            ).fetchall()
            month_rows = conn.execute(
                """SELECT strftime('%Y-%m', created_at) AS m, COUNT(*) AS c
                   FROM knowledge_cards
                   WHERE card_type = ? AND date(created_at) < date('now', '-6 days')
                   GROUP BY m ORDER BY m DESC""",
                (card_type,),
            ).fetchall()
        else:
            week_rows = conn.execute(
                """SELECT DISTINCT date(created_at) AS d
                   FROM knowledge_cards
                   WHERE date(created_at) >= date('now', '-6 days')
                   ORDER BY d DESC"""
            ).fetchall()
            month_rows = conn.execute(
                """SELECT strftime('%Y-%m', created_at) AS m, COUNT(*) AS c
                   FROM knowledge_cards
                   WHERE date(created_at) < date('now', '-6 days')
                   GROUP BY m ORDER BY m DESC"""
            ).fetchall()

    return {
        "this_week": [r["d"] for r in week_rows],
        "older_months": [{"month": r["m"], "count": r["c"]} for r in month_rows],
    }


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