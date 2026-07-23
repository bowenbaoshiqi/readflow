"""epub CFI 区间 → 纯文本提取(轻量:章节级定位,不解析 CFI 路径)。"""
from __future__ import annotations

import re

from ebooklib import epub


class EpubTextError(Exception):
    """EPUB 正文提取失败。"""


class InvalidSpineRange(EpubTextError):
    """传入的 spine 区间不合法。"""


def _html_to_text(content: bytes) -> str:
    html = content.decode("utf-8", errors="ignore")
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.S | re.I)
    inner = body.group(1) if body else html
    inner = re.sub(r"<script[^>]*>.*?</script>", "", inner, flags=re.S | re.I)
    inner = re.sub(r"<style[^>]*>.*?</style>", "", inner, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", inner)
    return re.sub(r"\s+", " ", text).strip()


def extract_spine_text(
    epub_path: str, start_index: int, end_index: int
) -> str:
    """按 Foliate 提供的 zero-based spine index 提取章节正文。"""
    try:
        book = epub.read_epub(epub_path, options={"ignore_ncx": True})
    except Exception as exc:
        raise EpubTextError(f"cannot read epub: {exc}") from exc

    spine = book.spine
    if start_index < 0 or end_index < 0 or max(start_index, end_index) >= len(spine):
        raise InvalidSpineRange(
            f"invalid spine range {start_index}..{end_index} "
            f"for {len(spine)} items"
        )

    range_start, range_end = sorted((start_index, end_index))
    parts = []
    for item_id, linear in spine[range_start:range_end + 1]:
        if linear == "no":
            continue
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        text = _html_to_text(item.get_content())
        if text:
            parts.append(text)
    return "\n".join(parts)


def extract_text(epub_path: str, start_cfi: str, end_cfi: str) -> str:
    """提取 start_cfi 到 end_cfi 之间的纯文本。

    CFI 格式约 "epubcfi(/6/4!/4/2)",其中 /6 是 spine item 序号(0-based)。
    轻量策略:取 CFI 里的第一个 spine 序号定位起始章和结束章,不做段落级定位。
    """
    if start_cfi == end_cfi:
        return ""

    try:
        book = epub.read_epub(epub_path, options={"ignore_ncx": True})
    except Exception:
        return ""

    start_idx = _cfi_spine_index(start_cfi)
    end_idx = _cfi_spine_index(end_cfi)

    spine = book.spine
    if not spine:
        return ""

    # 确保索引在范围内
    start_idx = max(0, start_idx)
    end_idx = min(len(spine) - 1, end_idx)
    if start_idx > end_idx:
        start_idx, end_idx = end_idx, start_idx

    parts = []
    for sidx, (item_id, _linear) in enumerate(spine):
        if sidx < start_idx:
            continue
        if sidx > end_idx:
            break
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        text = _html_to_text(item.get_content())
        if text:
            parts.append(text)

    return "\n".join(parts)


def _cfi_spine_index(cfi: str) -> int:
    """从 CFI 提取第一个 spine 序号。

    CFI 例子: "epubcfi(/6/4!/4/2)" → /6 = spine item 6
    """
    m = re.search(r"epubcfi\(/(\d+)", cfi)
    if m:
        return int(m.group(1))
    return 0
