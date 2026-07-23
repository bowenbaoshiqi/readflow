"""测试排版设置:参数 → CSS 字符串、范围校验、枚举映射。

设计:可测的核心逻辑放后端纯函数(app/typography.py),
前端只做胶水(fetch 设置 + setStyles 注入 + localStorage)。
"""
from __future__ import annotations

from app import typography


class TestToCss:
    """to_css: 设置对象 → CSS 字符串(喂给 foliate 的 view.renderer.setStyles)"""

    def test_to_css_contains_font_size(self):
        """字号应出现在 CSS 里。"""
        css = typography.to_css({
            "fontSize": 16, "spacing": 1.6, "margin": "medium", "font": "lxgw",
        })
        assert "font-size:16px" in css

    def test_font_size_uses_important_to_override_epub(self):
        """字号 CSS 加 !important,确保覆盖 epub 自带的 body/html font-size。

        epub 常用 body{font-size:100%} + 正文 em 单位,若我们的 html font-size
        优先级不够会被 epub CSS 盖掉,字号滑块无反应。!important 钉死。
        """
        css = typography.to_css({"fontSize": 20})
        assert "html, body { font-size:20px !important; }" in css

    def test_font_family_uses_important_to_override_epub(self):
        """字体 CSS 加 !important,覆盖 epub 自带 font-family。"""
        css = typography.to_css({"font": "lxgw"})
        assert "font-family: 'LXGW WenKai' !important" in css

    def test_spacing_uses_important_to_override_epub(self):
        """行距 CSS 加 !important,覆盖 epub 自带 line-height(如 .bodyContent{line-height:1.5em})。

        epub 正文常用 class 选择器设 line-height,优先级高于元素选择器 p,li,
        不加 !important 会被盖掉,行距滑块无反应。
        """
        css = typography.to_css({"spacing": 1.8})
        assert "line-height: 1.8 !important" in css

    def test_font_size_clamped_to_min(self):
        """字号低于 12 钳到 12。"""
        css = typography.to_css({"fontSize": 11})
        assert "font-size:12px" in css
        assert "font-size:11px" not in css

    def test_font_size_clamped_to_max(self):
        """字号高于 28 钳到 28。"""
        css = typography.to_css({"fontSize": 30})
        assert "font-size:28px" in css
        assert "font-size:30px" not in css

    def test_spacing_clamped_to_min(self):
        """行距低于 1.2 钳到 1.2。"""
        css = typography.to_css({"spacing": 1.0})
        assert "line-height: 1.2" in css

    def test_spacing_clamped_to_max(self):
        """行距高于 2.8 钳到 2.8。"""
        css = typography.to_css({"spacing": 3.0})
        assert "line-height: 2.8" in css


class TestMargin:
    """边距枚举:窄/中/宽 → 段落左右 margin px"""

    def test_margin_medium(self):
        """medium → 左右 16px。"""
        css = typography.to_css({"margin": "medium"})
        assert "margin: 0 16px" in css

    def test_margin_narrow(self):
        """narrow → 左右 8px。"""
        css = typography.to_css({"margin": "narrow"})
        assert "margin: 0 8px" in css

    def test_margin_wide(self):
        """wide → 左右 24px。"""
        css = typography.to_css({"margin": "wide"})
        assert "margin: 0 24px" in css

    def test_margin_invalid_falls_back_to_medium(self):
        """非法边距值 → 退回 medium(16px)。"""
        css = typography.to_css({"margin": "huge"})
        assert "margin: 0 16px" in css


class TestFont:
    """字体映射:id → font-family + @font-face"""

    def test_font_lxgw_applies_family(self):
        """font 'lxgw' → CSS 含 font-family:'LXGW WenKai'。"""
        css = typography.to_css({"font": "lxgw"})
        assert "font-family: 'LXGW WenKai'" in css

    def test_font_lxgw_declares_fontface(self):
        """应声明 @font-face 指向本地字体文件,走 /static/fonts/。"""
        css = typography.to_css({"font": "lxgw"})
        assert "@font-face" in css
        assert "LXGW WenKai" in css
        assert "/static/fonts/" in css

    def test_font_format_matches_file(self):
        """@font-face 的 format 必须与文件实际格式一致,否则浏览器不加载字体。"""
        css = typography.to_css({"font": "lxgw"})
        assert "format('truetype')" in css
        assert "lxgw.ttf" in css

    def test_font_invalid_falls_back_to_default(self):
        """非法字体 id → 退回默认(lxgw)。"""
        css = typography.to_css({"font": "nope"})
        assert "font-family: 'LXGW WenKai'" in css


class TestTypographyAPI:
    """GET /api/settings/typography:前端首次加载拿默认设置 + 字体清单 + 合法范围"""

    def test_api_returns_defaults(self, client):
        """返回默认设置(字号/行距/边距/字体)。"""
        r = client.get("/api/settings/typography")
        assert r.status_code == 200
        data = r.json()
        assert data["defaults"]["fontSize"] == 16
        assert data["defaults"]["margin"] == "medium"
        assert data["defaults"]["font"] == "lxgw"

    def test_api_returns_font_list(self, client):
        """返回字体清单,含霞鹜文楷。"""
        data = client.get("/api/settings/typography").json()
        fonts = data["fonts"]
        assert any(f["id"] == "lxgw" and f["name"] == "霞鹜文楷" for f in fonts)

    def test_api_returns_ranges(self, client):
        """返回合法范围,前端据此约束滑块。"""
        data = client.get("/api/settings/typography").json()
        ranges = data["ranges"]
        assert ranges["fontSize"] == {"min": 12, "max": 28}
        assert ranges["spacing"] == {"min": 1.2, "max": 2.8}
        assert set(ranges["margin"]) == {"narrow", "medium", "wide"}


class TestTypographyCssAPI:
    """POST /api/settings/typography/css:前端发设置,后端返回 CSS 字符串。

    CSS 生成留在后端(稳定性优先),前端只负责调 setStyles 注入。
    """

    def test_css_endpoint_returns_css(self, client):
        """发设置 → 返回可注入的 CSS,含字号。"""
        r = client.post("/api/settings/typography/css", json={
            "fontSize": 20, "spacing": 1.8, "margin": "wide", "font": "lxgw",
        })
        assert r.status_code == 200
        css = r.json()["css"]
        assert "font-size:20px" in css
        assert "margin: 0 24px" in css

    def test_css_endpoint_clamps_invalid(self, client):
        """非法值在后端被钳制,前端无需重复校验。"""
        r = client.post("/api/settings/typography/css", json={
            "fontSize": 99, "spacing": 9, "margin": "huge", "font": "nope",
        })
        assert r.status_code == 200
        css = r.json()["css"]
        assert "font-size:28px" in css
        assert "margin: 0 16px" in css  # 非法边距 → 中
        assert "LXGW WenKai" in css     # 非法字体 → 默认

    def test_css_endpoint_robust_to_bad_types(self, client):
        """前端 localStorage 脏数据(非数字字符串)不应 422,后端应回退默认。

        场景:用户 localStorage 存了 spacing:"abc" 这类脏值,
        前端原样 POST,后端必须返回 200 + 钳制后 CSS,而非 422 中断排版。
        """
        r = client.post("/api/settings/typography/css", json={
            "fontSize": "abc", "spacing": "xyz", "margin": 123, "font": 456,
        })
        assert r.status_code == 200
        css = r.json()["css"]
        # 类型错误 → 回退默认值,仍生成合法 CSS
        assert "font-size:16px" in css   # 默认字号
        assert "line-height: 1.6" in css  # 默认行距
        assert "margin: 0 16px" in css    # 默认边距
        assert "LXGW WenKai" in css       # 默认字体

    def test_css_endpoint_ignores_extra_fields(self, client):
        """前端多发的字段不影响 CSS 生成。"""
        r = client.post("/api/settings/typography/css", json={
            "fontSize": 18, "extraField": "ignore me", "another": [1, 2, 3],
        })
        assert r.status_code == 200
        assert "font-size:18px" in r.json()["css"]
