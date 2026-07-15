"""每日凌晨批处理:GLM 生成知识卡片 + 盲点 + 互联网推荐书籍。

定时策略:APScheduler 凌晨 2:00(CRON)。
调用策略:串行,API 间 1s 间隔,失败重试 2 次(间隔 3s,与 metadata.py 一致)。
模型:glm-5.2。
"""
from __future__ import annotations

import json
import os
import time

import httpx

from . import db

ZHIPU_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_WEBSEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
GLM_MODEL = "glm-5.2"
MAX_RETRIES = 2
RETRY_DELAY = 3.0
API_INTERVAL = 1.0


def _api_key() -> str:
    return os.environ.get("ZHIPU_API_KEY", "")


# ---------- step 1: 生成 knowledge + blind_spot ----------

def _call_glm(prompt: str, timeout: float = 300.0) -> str:
    """调 GLM chat API,失败不重试。

    timeout=300s:GLM-5.2 偶发慢响应(实测单次 121s/171s 仍成功),60s read
    timeout 会误杀偶发卡顿的请求。ReadTimeout 重试对"服务端偶发卡顿"无效
    (实测 3 次重试均失败,每次跑满 60s = 186s/次纯浪费),故不重试。
    """
    key = _api_key()
    r = httpx.post(
        ZHIPU_CHAT_URL,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": GLM_MODEL,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    if r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"]
    raise Exception(f"GLM API {r.status_code}: {r.text[:200]}")


def _generate_knowledge_and_blindspots() -> list[dict]:
    """步骤 1:GLM 读 24h reading_log + highlights → 5 knowledge + 5 blind_spot。

    GLM 只返回 title + body。book_id/source_type/source_ids 由代码层
    从 reading_log 注入到 knowledge 卡片上。
    """
    with db.get_conn() as conn:
        logs = conn.execute(
            """SELECT rl.book_id, rl.text, b.title AS book_title
               FROM reading_log rl
               JOIN books b ON b.id = rl.book_id
               WHERE rl.created_at >= datetime('now', '-1 day')
               ORDER BY rl.created_at DESC"""
        ).fetchall()
        highlights = conn.execute(
            """SELECT h.text, b.title AS book_title
               FROM highlights h
               JOIN books b ON b.id = h.book_id
               WHERE h.created_at >= datetime('now', '-1 day')
               ORDER BY h.created_at DESC"""
        ).fetchall()
        existing_tags = conn.execute(
            "SELECT DISTINCT tags FROM books WHERE tags IS NOT NULL AND tags != ''"
        ).fetchall()
        existing_cards = conn.execute(
            "SELECT card_type, title, body FROM knowledge_cards"
        ).fetchall()

    if not logs and not highlights:
        return []

    # 记录 book_id 用于代码层注入
    log_book_ids = [r["book_id"] for r in logs]

    log_text = "\n\n".join(
        f"《{r['book_title']}》: {r['text'][:3000]}" for r in logs
    )
    hl_text = "\n".join(
        f"《{r['book_title']}》: {r['text']}" for r in highlights
    )
    tags_text = ", ".join(
        r["tags"] for r in existing_tags
    ) if existing_tags else "暂无标签"
    existing_cards_text = "\n".join(
        f"[{r['card_type']}] {r['title']}: {r['body'][:100]}" for r in existing_cards
    ) if existing_cards else "暂无已有卡片"

    prompt = (
        "你是知识分析助手。根据用户最近的阅读内容和已有知识库，生成今日知识卡片和盲点卡片。\n\n"
        f"## 最近阅读内容:\n{log_text[:5000]}\n\n"
        f"## 最近划线:\n{hl_text[:2000]}\n\n"
        f"## 书库标签:\n{tags_text}\n\n"
        f"## 已有知识卡片:\n{existing_cards_text[:2000]}\n\n"
        "请生成 **正好 5 张 knowledge 卡片** 和 **正好 5 张 blind_spot 卡片**。\n\n"
        "knowledge 卡片:从阅读内容中提炼的具体知识点(概念、观点、事实)。每张含:\n"
        "- title: 知识点标题(15字以内)\n"
        "- body: 100-200字解释\n\n"
        "blind_spot 卡片:基于书库标签和已有卡片分析的知识盲点。每张含:\n"
        "- title: 盲点标题(15字以内,如「缺少认知心理学视角」)\n"
        "- body: 100-200字解释你缺什么、为什么重要\n\n"
        "只返回 JSON 数组,不要其他内容。格式示例:\n"
        '[{"card_type":"knowledge","title":"幸存者偏差","body":"..."},'
        '{"card_type":"blind_spot","title":"缺少XX","body":"..."}]'
    )

    try:
        content = _call_glm(prompt)
        cards = _parse_glm_json(content)
    except Exception as e:
        print(f"[readflow] step1 GLM failed: {e}")
        return []

    # 代码层注入:为 knowledge 卡片补充 book_id/source_type/source_ids
    for c in cards:
        if c.get("card_type") == "knowledge":
            c["book_id"] = log_book_ids[0] if log_book_ids else None
            c["source_type"] = "reading_log"
            c["source_ids"] = log_book_ids[:1]

    # 确保正好 5 knowledge + 5 blind_spot
    k = [c for c in cards if c.get("card_type") == "knowledge"][:5]
    b = [c for c in cards if c.get("card_type") == "blind_spot"][:5]
    return k + b


def _parse_glm_json(content: str) -> list:
    """从 GLM 输出解析 JSON 数组,容错剥代码块。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


# ---------- step 2: 生成 recommendation ----------

def _do_websearch(query: str) -> dict:
    """单次智谱 Web Search,失败重试 2 次。"""
    key = _api_key()
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = httpx.post(
                ZHIPU_WEBSEARCH_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "search_query": query[:70],
                    "search_engine": "search_std",
                    "search_intent": False,
                    "count": 5,
                    "content_size": "medium",
                },
                timeout=30.0,
            )
        except httpx.HTTPError:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_DELAY)
            continue
        if r.status_code == 200:
            return r.json()
        if attempt == MAX_RETRIES:
            raise Exception(f"WebSearch API {r.status_code}")
        time.sleep(RETRY_DELAY)
    raise Exception("unreachable")


def _search_book(parent: dict) -> dict:
    """为一张 knowledge/blind_spot 卡片搜 1 本推荐书。

    流程:Web Search 搜书名 → GLM 从搜索结果中选择最合适的一本 +
    生成推荐理由(引用具体知识点/盲点)+ 200-400 字简介。
    """
    if parent["card_type"] == "knowledge":
        query = f"{parent['title']} 相关书籍 豆瓣"
    else:
        query = f"{parent['title']} 入门书 豆瓣"

    search_result = _do_websearch(query)
    search_items = search_result.get("search_result") or []
    digest = " ".join(
        (item.get("content") or "")[:500] for item in search_items[:3]
    )
    if not digest:
        raise Exception("no search results")

    prompt = (
        f"从以下搜索结果中,选出最合适的一本书推荐给用户。\n\n"
        f"推荐背景:用户读到了「{parent['title']}」"
        f"{'（知识盲点）' if parent['card_type'] == 'blind_spot' else '（知识点）'}。\n"
        f"父卡片内容:{parent['body'][:200]}\n\n"
        f"搜索结果:\n{digest[:2000]}\n\n"
        "请选出最合适的一本,给出:\n"
        "- title: 书名\n"
        "- author: 作者\n"
        "- reason: 真实推荐理由,必须引用具体的知识点或盲点内容(100字左右)\n"
        "- summary: 200-400字简介\n"
        "- isbn: ISBN\n\n"
        '只返回JSON:{"title":"","author":"","reason":"","summary":"","isbn":""}\n'
    )

    content = _call_glm(prompt[:3000])
    data = _parse_glm_json(content)
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data:
        return data[0]
    raise Exception("GLM failed to parse recommendation")


def _generate_recommendations(parents: list[dict]) -> list[dict]:
    """步骤 2:为每张 knowledge/blind_spot 各生成 1 张 recommendation。

    串行调用:每本书搜完后等 API_INTERVAL,失败重试内置。
    """
    cards = []
    for parent in parents:
        try:
            book = _search_book(parent)
        except Exception:
            continue  # 跳过失败的书
        cards.append({
            "card_type": "recommendation",
            "title": book.get("title", ""),
            "body": book.get("reason", ""),
            "parent_card_id": parent["id"],
            "recommend_book": json.dumps(book, ensure_ascii=False),
        })
        time.sleep(API_INTERVAL)
    return cards


# ---------- 主入口 ----------

def run_daily_job() -> list[int]:
    """每日凌晨批处理主函数。

    返回入库的卡片 ID 列表。
    """
    # 步骤 1
    try:
        raw_cards = _generate_knowledge_and_blindspots()
    except Exception as e:
        print(f"[readflow] job step1 failed: {e}")
        return []

    if not raw_cards:
        return []

    with db.db() as conn:
        ids = []
        for c in raw_cards:
            # 校验 book_id:必须存在,否则用 NULL
            book_id = None
            if c.get("book_id") is not None:
                try:
                    bid = int(c["book_id"])
                    exists = conn.execute(
                        "SELECT 1 FROM books WHERE id=?", (bid,)
                    ).fetchone()
                    if exists:
                        book_id = bid
                except (ValueError, TypeError):
                    pass

            cur = conn.execute(
                """INSERT INTO knowledge_cards(card_type, title, body, book_id,
                   source_type, source_ids, created_at)
                   VALUES(?,?,?,?,?,?,datetime('now'))""",
                (c.get("card_type"), c.get("title"), c.get("body"),
                 book_id, c.get("source_type"),
                 json.dumps(c.get("source_ids")) if c.get("source_ids") else None),
            )
            ids.append(cur.lastrowid)

    # 组装父卡片(已入库,有真实 ID)
    parents = []
    for i, c in enumerate(raw_cards):
        c_with_id = dict(c)
        c_with_id["id"] = ids[i] if i < len(ids) else None
        parents.append(c_with_id)

    # 步骤 2:串行生成推荐
    try:
        recs = _generate_recommendations(parents)
    except Exception as e:
        print(f"[readflow] job step2 failed: {e}")
        return ids

    if recs:
        with db.db() as conn:
            for r in recs:
                cur = conn.execute(
                    """INSERT INTO knowledge_cards(card_type, title, body,
                       parent_card_id, recommend_book, created_at)
                       VALUES(?,?,?,?,?,datetime('now'))""",
                    (r["card_type"], r["title"], r["body"],
                     r.get("parent_card_id"), r.get("recommend_book")),
                )
                ids.append(cur.lastrowid)

    return ids