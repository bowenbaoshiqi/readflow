"""外部元数据补全:Google Books provider + WebSearch 兜底 + provider 抽象。

设计:
- MetadataProvider 协议:统一接口,豆瓣/ISBNdb 日后挂入不返工。
- 原则:只用 ISBN 精确查,不做书名/作者模糊匹配(模糊查找易命错书,
  如《事实》命成《事实、虚构和预测》)。无 ISBN 的书直接 not_found,
  由 epub 内嵌字段兜底显示。这保证准确率优先于覆盖率。
- GoogleBooksProvider:ISBN → Google Books isbn: 精确查。
  网络异常/429/无结果一律返回 None(降级,不阻断入库)。
- WebSearchProvider(AI 兜底):Google 查不到时,用 ISBN + "简介 评分"
  做智谱 web 搜索,摘要拼简介 + 正则提取豆瓣评分。
- enrich_book(book_id):兜底链 Google→WebSearch → 合并 epub 内嵌兜底 → 写回。

API key 从环境变量读(GOOGLE_BOOKS_API_KEY / ZHIPU_API_KEY),不入 git。
"""
from __future__ import annotations

import os

import httpx

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"


# ---------- BookMeta:provider 返回的标准化元数据 ----------

def make_bookmeta(**kw) -> dict:
    """BookMeta 是个 dict(不造 dataclass,单用户项目轻量)。

    字段:title, author, publisher, publish_date, language, summary,
    rating, rating_count, tags(list), isbn, page_count, meta_source_detail。
    缺省全 None,有值才填。
    meta_source_detail:provider 内部用的来源标记(如 'web_search'),
    用于 enrich 决定写回时的 meta_source 值。
    """
    base = {
        "title": None, "author": None, "publisher": None,
        "publish_date": None, "language": None, "summary": None,
        "rating": None, "rating_count": None, "tags": None,
        "isbn": None, "page_count": None, "meta_source_detail": None,
    }
    base.update(kw)
    return base


# ---------- provider 抽象 ----------

class MetadataProvider:
    """元数据源协议。子类实现 search。"""

    name = "base"

    def search(self, title: str | None, author: str | None,
               isbn: str | None) -> dict | None:
        raise NotImplementedError


class GoogleBooksProvider(MetadataProvider):
    """Google Books API provider。

    优先 ISBN 精确查;无 ISBN 走 title+author 双约束。
    任何异常/空结果 → None(降级)。
    """

    name = "google_books"

    def __init__(self, api_key: str | None = None, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def search(self, title: str | None, author: str | None,
               isbn: str | None) -> dict | None:
        # 原则:只用 ISBN 精确查,不做书名/作者模糊匹配(模糊查找易命错书)。
        # 无 ISBN → 直接 None(由后续兜底或 not_found 处理)。
        if not isbn:
            return None
        return self._query(f"isbn:{isbn}")

    def _query(self, q: str) -> dict | None:
        """单次 Google Books 查询,200+items 解析,否则 None。"""
        try:
            r = httpx.get(
                GOOGLE_BOOKS_URL,
                params={"q": q, "maxResults": 1, "key": self.api_key or ""},
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        items = data.get("items")
        if not items:
            return None
        return self._parse(items[0].get("volumeInfo", {}))

    def _parse(self, v: dict) -> dict:
        ids = v.get("industryIdentifiers") or []
        isbn = next((i["identifier"] for i in ids
                     if i.get("type") == "ISBN_13"), None)
        if not isbn:
            isbn = next((i["identifier"] for i in ids
                         if i.get("type") == "ISBN_10"), None)
        authors = v.get("authors") or []
        return make_bookmeta(
            title=v.get("title"),
            author=authors[0] if authors else None,
            publisher=v.get("publisher"),
            publish_date=v.get("publishedDate"),
            language=v.get("language"),
            summary=v.get("description") or None,
            rating=v.get("averageRating"),
            rating_count=v.get("ratingsCount"),
            tags=v.get("categories"),
            isbn=isbn,
            page_count=v.get("pageCount") or None,
        )


# ---------- WebSearchProvider:智谱 Web Search + GLM 解析(主源) ----------

ZHIPU_WEBSEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
ZHIPU_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_MODEL = "glm-5v-turbo"


class WebSearchProvider(MetadataProvider):
    """智谱 Web Search + GLM 大模型解析(主元数据源)。

    流程:ISBN → Web Search 搜豆瓣摘要 → GLM 解析成简介+评分 → BookMeta。
    优先数据质量:GLM 理解上下文,比正则稳(评分人数格式多变、简介含噪)。
    任一环节失败(WebSearch 空/GLM 异常/JSON 解析失败)→ None(降级到 Google Books)。
    """

    name = "web_search"

    def __init__(self, api_key: str | None = None, timeout: float = 30.0,
                 max_summary_chars: int = 300, model: str = GLM_MODEL):
        self.api_key = api_key
        self.timeout = timeout
        self.max_summary_chars = max_summary_chars
        self.model = model

    def search(self, title: str | None, author: str | None,
               isbn: str | None) -> dict | None:
        query = self._build_query(title, author)
        if not query:
            return None
        results = self._websearch(query)
        if not results:
            return None
        return self._llm_parse(results, title)

    def _websearch(self, query: str) -> list | None:
        """单次 Web Search,200+有结果返回 list,否则 None。"""
        try:
            r = httpx.post(
                ZHIPU_WEBSEARCH_URL,
                headers={"Authorization": f"Bearer {self.api_key or ''}"},
                json={
                    "search_query": query[:70],  # API 限制 70 字符
                    "search_engine": "search_std",
                    "search_intent": False,
                    "count": 5,
                    "content_size": "medium",
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        return r.json().get("search_result") or None

    def _llm_parse(self, results: list, title: str | None) -> dict | None:
        """用 GLM 把搜索摘要解析成简介+评分。

        优先数据质量:GLM 理解上下文,正则易漏提/误提。
        关键:GLM 先校验摘要确实在讲这本书,无关内容(如搜到的电影/其他书)
        一律返回 null,search 层据此降级(宁可不存也不存垃圾简介)。
        """
        # 拼接摘要去重
        chunks, seen = [], set()
        for item in results:
            content = (item.get("content") or "").strip()
            if content and content not in seen:
                seen.add(content)
                chunks.append(content)
        digest = " ".join(chunks)[:1500]
        if not digest:
            return None

        prompt = (
            f"判断以下搜索摘要是否真的是《{title or ''}》这本书的资料,再提取简介和豆瓣评分。\n"
            "先核对:摘要内容是否围绕这本书(书名/作者/情节匹配)。若摘要是无关内容"
            "(其他书、电影、新闻等),所有字段返回 null。\n"
            "- rating:豆瓣10分制评分(如8.7),没提到或无关则 null\n"
            "- rating_count:评分人数(整数),没提到或无关则 null\n"
            "- summary:这本书的一段话简介(100-200字),无关则 null\n"
            '只返回JSON,格式: {"summary": "...", "rating": 8.7, "rating_count": 1234}\n\n'
            f"摘要:\n{digest}"
        )
        # GLM 偶发限流/超时:失败重试一次再降级(数据质量优先,别因偶发失败丢豆瓣数据)
        content = None
        for attempt in (1, 2):
            try:
                r = httpx.post(
                    ZHIPU_CHAT_URL,
                    headers={"Authorization": f"Bearer {self.api_key or ''}",
                             "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=self.timeout,
                )
            except httpx.HTTPError:
                if attempt == 2:
                    return None
                continue
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                break
            # 非200(如429限流):重试一次
            if attempt == 2:
                return None
        if content is None:
            return None
        data = _parse_llm_json(content)
        if data is None:
            return None

        rating = data.get("rating")
        if rating is not None:
            try:
                rating = float(rating)
                if not (0 < rating <= 10):
                    rating = None
            except (TypeError, ValueError):
                rating = None
        rating_count = data.get("rating_count")
        if rating_count is not None:
            try:
                rating_count = int(rating_count)
            except (TypeError, ValueError):
                rating_count = None
        summary = data.get("summary")

        # 摘要与书无关时 GLM 返回全 null → 降级(不存垃圾简介/评分)。
        # summary 和 rating 都没有说明没拿到有效资料,交给兜底链(Google)处理。
        if not summary and rating is None:
            return None

        return make_bookmeta(
            summary=summary or None,
            rating=rating,
            rating_count=rating_count,
            meta_source_detail="web_search",
        )

    def _build_query(self, title, author):
        """构造 search() 的查询:{书名} {作者} 豆瓣 简介 评分。

        实测:纯 ISBN 搜索常返回无关摘要(ISBN 当字符串匹配到无关页面,
        如搜《人有人的用处》ISBN 却返回韩国电影)。改用书名+作者+豆瓣
        定位,GLM 解析时校验摘要确实是本书,误命风险低。
        ISBN 不进查询(它干扰搜索),但仍在 DB 保留作记录。
        无书名 → 返回 None(没书名没法定向搜索)。
        """
        if not title:
            return None
        parts = [p for p in (title, author) if p]
        return f"{' '.join(parts)} 豆瓣 简介 评分"


def _parse_llm_json(content: str) -> dict | None:
    """从 GLM 输出里解析 JSON,容错剥代码块。

    GLM 常把 JSON 包在 ```json ... ``` 代码块里,也可能裸返回。
    解析失败(非合法 JSON)→ None(降级,宁可不存也不存半成品)。
    """
    text = content.strip()
    # 剥 ```json ... ``` 或 ``` ... ``` 代码块
    if text.startswith("```"):
        # 去首行 ```json/```,去末尾 ```
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        import json
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None




# ---------- provider 注册表 ----------

_PROVIDERS = {
    "google_books": lambda: GoogleBooksProvider(
        api_key=os.environ.get("GOOGLE_BOOKS_API_KEY")),
    "web_search": lambda: WebSearchProvider(
        api_key=os.environ.get("ZHIPU_API_KEY")),
}


def get_provider(name: str) -> MetadataProvider:
    """按名取 provider。未知名抛 ValueError。"""
    factory = _PROVIDERS.get(name)
    if not factory:
        raise ValueError(f"未知元数据源: {name}")
    return factory()


# ---------- enrich:调 provider → 合并 epub 内嵌兜底 → 写回 DB ----------

def enrich_book(book_id: int, provider: MetadataProvider | None = None,
                fill_missing: bool = False) -> None:
    """为单本书补全外部元数据(单阶段:书名+作者搜豆瓣,智谱优先 Google 兜底)。

    策略(v0.3):
      - 智谱 WebSearch({书名} {作者} 豆瓣 简介 评分)→ GLM 解析简介+评分,
        GLM 校验摘要确实是本书(无关内容返回 null → 降级)。
      - 智谱未命中 → Google Books 兜底(补 tags/page_count 等)。
      - 都失败 → not_found。
    不再依赖 ISBN 搜索(实测 ISBN 干扰搜索,书名+作者+豆瓣命中率更高)。
    ISBN 仍写回 DB(epub 内嵌有的就存),作记录不进查询。

    - 已 meta_status='ok' 的跳过(幂等),除非 fill_missing=True 且缺关键字段
    - 任一异常 → 写 meta_status,不抛(不影响书可读)
    - 合并:provider 给的字段优先,空字段保留 epub 内嵌已有值(兜底)

    显式传 provider 时只调它(测试用),不走兜底链。
    """
    from . import db

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title, author, isbn, summary, rating, meta_status FROM books WHERE id=?",
            (book_id,),
        ).fetchone()
    if not row:
        return

    title = row["title"]
    author = row["author"]
    isbn = row["isbn"]
    status = row["meta_status"]

    # 已 ok:默认跳过(幂等)。fill_missing 且缺关键字段时,用 WebSearch 补缺。
    if status == "ok":
        if not fill_missing or not _missing_key_fields(row):
            return
        _fill_missing_with_websearch(book_id, title, author, isbn)
        return

    # 显式 provider(测试用):只调它,不走兜底链
    if provider is not None:
        try:
            meta = provider.search(title=title, author=author, isbn=isbn)
        except Exception as e:
            _write_failed(book_id, str(e), provider.name)
            return
        if meta is None:
            _write_not_found(book_id, provider.name)
            return
        _merge_and_write(book_id, meta,
                         meta.get("meta_source_detail") or provider.name)
        return

    # 单阶段:智谱(WebSearch+GLM,豆瓣数据质量优先)→ Google 兜底。
    # 智谱搜豆瓣摘要 + GLM 解析校验;Google 补 tags/page_count 等。
    google = get_provider("google_books")
    ws = get_provider("web_search")
    meta = None
    used_source = None
    for p in (ws, google):
        try:
            meta = p.search(title=title, author=author, isbn=isbn)
        except Exception as e:
            _write_failed(book_id, str(e), p.name)
            return
        if meta is not None:
            used_source = meta.get("meta_source_detail") or p.name
            break

    if meta is None:
        _write_not_found(book_id, ws.name)
        return

    _merge_and_write(book_id, meta, used_source)


def _write_not_found(book_id: int, source: str) -> None:
    from . import db
    with db.db() as conn:
        conn.execute(
            "UPDATE books SET meta_status='not_found', "
            "meta_source=?, meta_fetched_at=datetime('now') WHERE id=?",
            (source, book_id),
        )


def _missing_key_fields(row) -> bool:
    """判断是否缺关键字段(summary 或 rating),决定要不要用 WebSearch 补。"""
    return not row["summary"] or row["rating"] is None


def _fill_missing_with_websearch(book_id, title, author, isbn) -> None:
    """已 ok 但缺字段时,用 WebSearch 补缺失字段(COALESCE 不覆盖已有)。"""
    from . import db
    ws = get_provider("web_search")
    try:
        meta = ws.search(title=title, author=author, isbn=isbn)
    except Exception:
        return  # 补充失败不影响已有 ok 状态
    if meta is None:
        return
    # 只补 NULL 字段,不动 Google 已给的;meta_source 保持 google_books(主源)
    import json
    tags = meta.get("tags")
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    with db.db() as conn:
        conn.execute(
            """UPDATE books SET
                 summary=COALESCE(summary, ?),
                 rating=COALESCE(rating, ?),
                 rating_count=COALESCE(rating_count, ?),
                 tags=COALESCE(tags, ?),
                 meta_fetched_at=datetime('now')
               WHERE id=?""",
            (meta.get("summary"), meta.get("rating"), meta.get("rating_count"),
             tags_json, book_id),
        )


def _merge_and_write(book_id: int, meta: dict, source: str) -> None:
    """合并 provider 结果与 epub 内嵌(空字段不覆盖),写回 books。"""
    from . import db
    import json

    tags = meta.get("tags")
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else None

    # 只在 provider 给了非空值时更新该列;NULL 不覆盖 epub 内嵌已有值
    with db.db() as conn:
        conn.execute(
            """UPDATE books SET
                 summary=COALESCE(?, summary),
                 rating=COALESCE(?, rating),
                 rating_count=COALESCE(?, rating_count),
                 tags=COALESCE(?, tags),
                 publisher=COALESCE(?, publisher),
                 publish_date=COALESCE(?, publish_date),
                 isbn=COALESCE(?, isbn),
                 page_count=COALESCE(?, page_count),
                 meta_source=?,
                 meta_status='ok',
                 meta_error=NULL,
                 meta_fetched_at=datetime('now')
               WHERE id=?""",
            (
                meta.get("summary"),
                meta.get("rating"),
                meta.get("rating_count"),
                tags_json,
                meta.get("publisher"),
                meta.get("publish_date"),
                meta.get("isbn"),
                meta.get("page_count"),
                source,
                book_id,
            ),
        )


def _write_failed(book_id: int, error: str, source: str) -> None:
    from . import db
    with db.db() as conn:
        conn.execute(
            "UPDATE books SET meta_status='failed', meta_source=?, "
            "meta_error=?, meta_fetched_at=datetime('now') WHERE id=?",
            (source, error[:500], book_id),
        )
