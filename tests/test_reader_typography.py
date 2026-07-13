"""前端排版面板端到端测试(Playwright + 起 uvicorn 服务)。

验证前端胶水行为,这些是 pytest 无法用 TestClient 测的(需真浏览器跑 JS):
- 「文」按钮点击 → 面板弹出
- init 失败时按钮仍可点(绑定不依赖异步链成功)
- 滑块/纸片改动 → foliate renderer 收到 setStyles

默认 pytest run 不忽略本文件;需 playwright + chromium。
服务在 fixture 里起,用真实后端。
"""
from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from uvicorn import Config, Server


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url(tmp_path, monkeypatch):
    """每测起一个真实 uvicorn 服务,用 tmp_path 库 + 预入库一本样本书。

    function scope:跟 conftest 的 isolate_db 同生命周期,服务线程读到的
    db.DB_PATH 始终是本 fixture 设的 tmp 库,不串。
    """
    import app.main as main_mod
    from app import db, ingest
    from tests.conftest import _first_sample
    db.DB_PATH = tmp_path / "e2e.db"
    ingest.COVER_DIR = tmp_path / "covers"
    db.init_db()
    ingest.ingest_file(_first_sample())
    port = _free_port()
    config = Config(main_mod.app, host="127.0.0.1", port=port, log_level="warning")
    server = Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture
def page(server_url):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        yield pg
        b.close()


def _book_id(server_url) -> int:
    """拿任意一本已入库书的 id。"""
    import httpx
    r = httpx.get(f"{server_url}/api/library")
    books = r.json()
    assert books, "书库为空,无法测阅读器"
    return books[0]["id"]


class TestTypoPanelInteraction:
    """排版面板:按钮点击与控件交互"""

    def test_button_opens_panel(self, server_url, page):
        """点「文」按钮,面板从 hidden 变可见。"""
        bid = _book_id(server_url)
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(2000)
        assert page.evaluate("document.getElementById('typo-panel').hidden") is True
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        assert page.evaluate("document.getElementById('typo-panel').hidden") is False

    def test_button_works_even_if_init_fails(self, server_url, page):
        """initTypography 抛错时,按钮仍能开面板(绑定不依赖异步链)。

        模拟:注入坏 fetch 让 /api/settings/typography 404,initTypography 必失败。
        若 bindTypography 在异步链末端,此时按钮无响应 —— 这是要防的 bug。
        """
        bid = _book_id(server_url)
        # 在页面加载前注入:让 typography 接口返回坏数据触发异常
        page.add_init_script("""
            const origFetch = window.fetch;
            window.fetch = (url, opts) => {
                if (typeof url === 'string' && url.includes('/api/settings/typography')) {
                    return Promise.resolve({json: () => Promise.reject(new Error('mock fail'))});
                }
                return origFetch(url, opts);
            };
        """)
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)
        # initTypography 应已失败,但按钮必须仍可点
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        panel_open = page.evaluate("document.getElementById('typo-panel').hidden")
        assert panel_open is False, "init 失败后按钮无响应 —— 绑定不应依赖异步链成功"

    def test_slider_no_crash_when_init_failed(self, server_url, page):
        """init 失败(typoSettings 仍为 null)时拖滑块不应抛异常。

        滑块 oninput 直接写 typoSettings.fontSize,若 typoSettings 未初始化会崩。
        控件处理器必须容忍初始化未完成。
        """
        bid = _book_id(server_url)
        page.add_init_script("""
            const origFetch = window.fetch;
            window.fetch = (url, opts) => {
                if (typeof url === 'string' && url.includes('/api/settings/typography')) {
                    return Promise.resolve({json: () => Promise.reject(new Error('mock fail'))});
                }
                return origFetch(url, opts);
            };
        """)
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(3000)
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        # 拖字号滑块 —— typoSettings 此时为 null,不应崩
        page.evaluate("""const s = document.getElementById('typo-size');
            s.value = 22;
            s.dispatchEvent(new Event('input', {bubbles: true}));""")
        page.wait_for_timeout(500)
        assert not errs, f"init 失败时拖滑块抛错: {errs}"


class TestTypoAppliesToRenderer:
    """排版改动 → foliate renderer.setStyles 收到含新值的 CSS"""

    def _patch_setstyles(self, page):
        """patch foliate-view 的 setStyles,记录每次调用的 CSS。返回取值函数。

        用 MutationObserver 等 renderer attach 后立即 patch,
        确保 initTypography 首次调用 setStyles 也能捕获(reload 场景关键)。
        """
        page.add_init_script("""
            window.__typoCSS = [];
            const patchRenderer = (fv) => {
                if (!fv || !fv.renderer || fv.__patched) return false;
                if (typeof fv.renderer.setStyles !== 'function') return false;
                const orig = fv.renderer.setStyles.bind(fv.renderer);
                fv.renderer.setStyles = (css) => {
                    window.__typoCSS.push(css);
                    return orig(css);
                };
                fv.__patched = true;
                return true;
            };
            const tryPatch = () => {
                const fv = document.querySelector('foliate-view');
                if (patchRenderer(fv)) return;
                requestAnimationFrame(tryPatch);
            };
            // foliate-view 创建后,renderer 可能稍后 attach;用 observer 兜底
            const obs = new MutationObserver(() => {
                const fv = document.querySelector('foliate-view');
                patchRenderer(fv);
            });
            document.addEventListener('DOMContentLoaded', () => {
                obs.observe(document.body, {childList: true, subtree: true});
                tryPatch();
            });
        """)

    def _last_css(self, page):
        return page.evaluate("window.__typoCSS.slice(-1)[0] || ''")

    def test_font_size_slider_applies(self, server_url, page):
        """拖字号滑块 → setStyles 收到含新字号的 CSS。"""
        bid = _book_id(server_url)
        self._patch_setstyles(page)
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(4000)
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        # 初始 CSS(初始化时注入的)
        before = self._last_css(page)
        # 拖到 24
        page.evaluate("""const s = document.getElementById('typo-size');
            s.value = 24;
            s.dispatchEvent(new Event('input', {bubbles: true}));""")
        page.wait_for_timeout(500)  # 节流 120ms + fetch
        after = self._last_css(page)
        assert after, "setStyles 未被调用"
        assert "font-size:24px" in after, f"拖滑块后 CSS 不含新字号: {after}"
        assert after != before, "拖滑块后 CSS 未变化"

    def test_settings_persist_across_reload(self, server_url, page):
        """改字号 → 刷新页面 → 设置恢复(面板滑块值 + 注入的 CSS 都是新值)。

        localStorage 往返:用户调过一次,重开书应自动应用,无需再调。
        """
        bid = _book_id(server_url)
        self._patch_setstyles(page)
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(4000)
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        # 改字号到 26 + 边距到 wide
        page.evaluate("""const s = document.getElementById('typo-size');
            s.value = 26; s.dispatchEvent(new Event('input', {bubbles: true}));""")
        page.click('#typo-margin-chips button[data-margin="wide"]')
        page.wait_for_timeout(600)  # 等节流 + 存 localStorage
        # 确认存了
        saved = page.evaluate("localStorage.getItem('readflow:typography')")
        assert saved and '"fontSize":26' in saved, f"未存入 localStorage: {saved}"

        # 刷新页面
        page.reload(wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(4000)
        # 滑块值应恢复 26
        slider_val = page.evaluate("document.getElementById('typo-size').value")
        assert slider_val == "26", f"刷新后滑块未恢复: {slider_val}"
        # 注入的 CSS 应含 26px
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        css = self._last_css(page)
        assert "font-size:26px" in css, f"刷新后未自动应用设置: {css}"

    def test_margin_chip_applies(self, server_url, page):
        """点边距纸片 → setStyles 收到对应 margin px。"""
        bid = _book_id(server_url)
        self._patch_setstyles(page)
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(4000)
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        page.click('#typo-margin-chips button[data-margin="wide"]')
        page.wait_for_timeout(500)
        css = self._last_css(page)
        assert "margin: 0 24px" in css, f"边距 wide 未生效: {css}"
        # 选中态
        pressed = page.evaluate("""document.querySelector(
            '#typo-margin-chips button[data-margin="wide"]').getAttribute('aria-pressed')""")
        assert pressed == "true", "wide 纸片未标记选中"

    def test_font_chip_applies(self, server_url, page):
        """点字体纸片 → setStyles 收到对应 font-family。"""
        bid = _book_id(server_url)
        self._patch_setstyles(page)
        page.goto(f"{server_url}/read/{bid}", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(4000)
        page.click("#typo-btn")
        page.wait_for_timeout(300)
        # 默认就是 lxgw,先切到非默认再切回,验证切换生效
        # v0.2 只有 lxgw 一个,断言点击后 CSS 仍含其 family 且纸片选中
        page.click('#typo-font-chips button[data-font="lxgw"]')
        page.wait_for_timeout(500)
        css = self._last_css(page)
        assert "font-family: 'LXGW WenKai'" in css, f"字体未生效: {css}"
        pressed = page.evaluate("""document.querySelector(
            '#typo-font-chips button[data-font="lxgw"]').getAttribute('aria-pressed')""")
        assert pressed == "true", "lxgw 纸片未标记选中"
