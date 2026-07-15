"""真实流程:1本书抽1章节 → reading_log → run_daily_job → 入生产库。

入生产库 data/readflow.db(非临时库)。跑完可在 /knowledge 页面查看。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from ebooklib import epub
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DB_PATH
from app.jobs import run_daily_job

BOOKS_LIB = Path(__file__).resolve().parent.parent / "books-library"

# 选定的 1 本
BOOK = (4, "真希望我父母读过这本书", BOOKS_LIB / "真希望我父母读过这本书.epub")


def extract_half_chapter(epub_path: Path) -> str:
    """抽取第一个有内容章节的前半段文本。"""
    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        content = item.get_content().decode("utf-8", errors="ignore")
        body = re.search(r"<body[^>]*>(.*?)</body>", content, re.S)
        inner = body.group(1) if body else content
        inner = re.sub(r"<script[^>]*?>.*?</script>", "", inner, flags=re.S)
        inner = re.sub(r"<style[^>]*?>.*?</style>", "", inner, flags=re.S)
        text = re.sub(r"<[^>]+>", "", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 200:
            return text[: len(text) // 2]
    return ""


def main():
    print("=== 真实流程入生产库(1本书1章节)===")
    print(f"生产库: {DB_PATH}\n")

    # 1. 建 v0.4 表(生产库可能还没有)
    print("1. init_db() 确保 reading_log / knowledge_cards 表存在...")
    db.init_db()

    # 2. 抽章节 + 写 reading_log(幂等:已有则跳过)
    bid, title, path = BOOK
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT id, length(text) AS textlen FROM reading_log WHERE book_id=?",
            (bid,),
        ).fetchone()
    if existing:
        print(f"\n2. reading_log 已存在(id={existing['id']}, {existing['textlen']} 字), 跳过抽取")
    else:
        print(f"\n2. 抽取《{title}》(id={bid}) 1章节前半段 → reading_log...")
        if not path.exists():
            print(f"   ❌ 找不到 {path}")
            return
        text = extract_half_chapter(path)
        print(f"   抽取 {len(text)} 字")
        with db.db() as conn:
            conn.execute(
                """INSERT INTO reading_log(book_id, start_cfi, end_cfi, text,
                   percent_from, percent_to)
                   VALUES(?,?,?,?,?,?)""",
                (bid, "epubcfi(/6/2!/4/1)", "epubcfi(/6/2!/4/99)", text, 0.1, 0.3),
            )

    # 3. 跑批处理(真实调 API)
    print("\n3. 运行 run_daily_job()(真实调智谱 API, 约 6-9 分钟)...")
    start = time.time()
    try:
        ids = run_daily_job()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n❌ 批处理异常: {e}")
        return
    elapsed = time.time() - start

    # 4. 查验
    print(f"\n4. 完成! 耗时 {elapsed:.1f}s, 入库 {len(ids)} 张卡片")
    with db.get_conn() as conn:
        for r in conn.execute(
            "SELECT card_type, COUNT(*) AS n FROM knowledge_cards GROUP BY card_type ORDER BY card_type"
        ).fetchall():
            print(f"   {r['card_type']}: {r['n']} 张")
        total = conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0]
    print(f"\n   总计 {total} 张卡片, 已入生产库 {DB_PATH}")
    print(f"   访问 http://localhost:8765/knowledge 查看")


if __name__ == "__main__":
    main()
