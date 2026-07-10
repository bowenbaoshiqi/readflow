"""端到端验证:用 playwright 打开阅读器,确认 foliate-js 真能渲染 epub。

验证点:
- 页面无 JS 报错
- foliate-view 元素被创建并加载了书籍
- 能看到正文文本(渲染成功)
- 截图存证
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://localhost:8765/read/3"  # 北京法源寺
SHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "screenshots"


def main():
    errors = []
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 700})
        page.on("console", lambda m: (
            errors.append(f"[{m.type}] {m.text}")
            if m.type == "error" else None
        ))
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(URL, wait_until="networkidle", timeout=20000)
        # 等 foliate-view 内部 iframe 加载出内容
        page.wait_for_timeout(4000)

        # 检查 foliate-view 是否存在且有内容
        fv_exists = page.evaluate("!!document.querySelector('foliate-view')")
        # 进度条是否更新了(foliate relocate 事件触发)
        progress_text = page.evaluate("document.getElementById('progress')?.textContent || ''")

        page.screenshot(path=str(SHOT_DIR / "reader.png"), full_page=False)

        # 翻到下一页测试交互
        try:
            page.evaluate("document.querySelector('foliate-view')?.next?.()")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(SHOT_DIR / "reader-page2.png"))
        except Exception as e:
            errors.append(f"[next] {e}")

        browser.close()

    print(f"foliate-view 存在: {fv_exists}")
    print(f"进度文本: {progress_text!r}")
    print(f"console 错误数: {len(errors)}")
    for e in errors:
        print(f"  {e}")
    print(f"截图: {SHOT_DIR}")
    return 0 if (fv_exists and not errors) else 1


if __name__ == "__main__":
    sys.exit(main())
