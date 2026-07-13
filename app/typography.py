"""排版设置:参数 → CSS 字符串、范围校验、枚举映射。

纯逻辑模块,无 DB 无副作用。前端 reader.js 拿到 to_css 的输出后
直接喂给 foliate-js 的 view.renderer.setStyles(css)。
"""
from __future__ import annotations

# 合法范围(MRD 3.2:字号 12-28px,行距 1.2-2.8,边距 窄/中/宽,字体选择)
FONT_SIZE_MIN = 12
FONT_SIZE_MAX = 28
SPACING_MIN = 1.2
SPACING_MAX = 2.8

# 边距枚举 → 段落左右 margin(px)
MARGINS = {"narrow": 8, "medium": 16, "wide": 24}
DEFAULT_MARGIN = "medium"

# 字体清单(id → CSS font-family 名)。v0.2 先加霞鹜文楷一个。
# format 字段对应 @font-face 的 src format(),必须与文件实际格式一致,否则浏览器不加载。
FONTS = [
    {"id": "lxgw", "name": "霞鹜文楷", "family": "LXGW WenKai",
     "file": "/static/fonts/lxgw.ttf", "format": "truetype"},
]
DEFAULT_FONT = "lxgw"

# id → 字体定义,便于查表
_FONTS_BY_ID = {f["id"]: f for f in FONTS}

DEFAULT_SETTINGS = {
    "fontSize": 16,
    "spacing": 1.6,
    "margin": DEFAULT_MARGIN,
    "font": DEFAULT_FONT,
}


def to_css(settings: dict) -> str:
    """把排版设置转成注入 iframe 的 CSS 字符串。

    settings: {fontSize, spacing, margin, font}
    返回的 CSS 喂给 foliate-js view.renderer.setStyles。
    """
    font_size = settings.get("fontSize", DEFAULT_SETTINGS["fontSize"])
    font_size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, font_size))
    spacing = settings.get("spacing", DEFAULT_SETTINGS["spacing"])
    spacing = max(SPACING_MIN, min(SPACING_MAX, spacing))
    margin = settings.get("margin", DEFAULT_MARGIN)
    margin_px = MARGINS.get(margin, MARGINS[DEFAULT_MARGIN])
    font_id = settings.get("font", DEFAULT_FONT)
    font = _FONTS_BY_ID.get(font_id, _FONTS_BY_ID[DEFAULT_FONT])

    return f"""
    @font-face {{
        font-family: '{font["family"]}';
        src: url('{font["file"]}') format('{font["format"]}');
    }}
    html {{ font-size:{font_size}px !important; }}
    body {{ font-family: '{font["family"]}' !important; }}
    p, li, blockquote, dd {{
        line-height: {spacing} !important;
        margin: 0 {margin_px}px;
        font-family: '{font["family"]}' !important;
    }}
    """
