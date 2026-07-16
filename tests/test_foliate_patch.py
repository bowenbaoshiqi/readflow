"""foliate-js paginator.js 补丁契约测试(v0.6 bugfix)。

foliate-js 是 git submodule(上游 johnfactotum/foliate-js),补丁不能直接
改在 submodule 工作树里(会被 `git submodule update --init` 冲掉)。故采用
Dockerfile 层 patch 策略:

  - 补丁以独立文件 patches/foliate-paginator.patch 存仓库
  - Dockerfile build 阶段 `patch -p1` 应用到 foliate-js/paginator.js
  - submodule 工作树保持上游原样

本测试断言补丁机制完整:patch 文件存在 + 含三处 guard + Dockerfile 引用它。
防止补丁文件被误删或 Dockerfile 漏 apply。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH_FILE = REPO / "patches" / "foliate-paginator.patch"
DOCKERFILE = REPO / "Dockerfile"


class TestFoliatePaginatorPatch:
    """验证 foliate-js 边界崩溃补丁的持久化机制。"""

    def test_patch_file_exists(self):
        """补丁文件存在于 patches/ 目录。"""
        assert PATCH_FILE.is_file(), f"补丁文件缺失: {PATCH_FILE}"

    def test_patch_guards_scrollBy(self):
        """补丁给 scrollBy() 加 #scrollBounds 回退。"""
        content = PATCH_FILE.read_text()
        # 补丁以 + 行表示新增
        assert re.search(
            r"^\+\s+if \(!this\.#scrollBounds\) return", content, re.M
        ), "补丁应含 scrollBy 的 #scrollBounds guard"

    def test_patch_guards_snap(self):
        """补丁给 snap() 加 #scrollBounds 回退(共两处:scrollBy + snap)。"""
        content = PATCH_FILE.read_text()
        guards = re.findall(r"^\+\s+if \(!this\.#scrollBounds\) return", content, re.M)
        assert len(guards) >= 2, \
            f"补丁应含至少 2 处 #scrollBounds guard(scrollBy + snap),实际 {len(guards)}"

    def test_patch_guards_touchState(self):
        """补丁给 #onTouchEnd 加 #touchState 回退。"""
        content = PATCH_FILE.read_text()
        assert re.search(
            r"^\+\s+if \(!this\.#touchState\) return", content, re.M
        ), "补丁应含 #onTouchEnd 的 #touchState guard"

    def test_dockerfile_applies_patch(self):
        """Dockerfile 在 build 阶段应用该补丁。"""
        content = DOCKERFILE.read_text()
        assert "foliate-paginator.patch" in content, \
            "Dockerfile 应引用 foliate-paginator.patch"
        assert re.search(r"patch\s+-p1", content), \
            "Dockerfile 应含 `patch -p1` 应用命令"

    def test_submodule_worktree_clean(self):
        """submodule 工作树保持上游原样(补丁未直接改 paginator.js)。

        防止有人手改工作树后又 commit,绕过 patch 文件机制。
        paginator.js 不应含 readflow: guard 标记(那是 patch 应用后才有的)。
        """
        paginator = REPO / "app" / "static" / "foliate-js" / "paginator.js"
        content = paginator.read_text()
        assert "readflow: guard" not in content, \
            "submodule 工作树不应含补丁;补丁应经 patches/ + Dockerfile 应用"
