# TDD 证据报告 — 文件系统删书 → 同步清库

## 1. 来源

本次 TDD 运行中派生的需求(无 `*.plan.md` 输入)。
计划文件:`/Users/ian/.claude/plans/snuggly-cuddling-seal.md`。

## 2. 用户 journey

> 作为读者,我想直接在文件系统里删掉 `books-library/` 下的 epub(Finder/命令行/NAS 文件管理),书架就自动不再显示这本书,这样删书和加书一样自然——加书是放文件,删书就是删文件,不需要额外的 UI 操作。

## 3. 问题

删书原只有一条入口:`DELETE /api/library/{book_id}` API(方案 C:把原文件 move 到 `.trash/` + 删封面 + 删 DB)。但 `LibraryHandler` **没有 `on_deleted`**,定时全量扫描 `_scan_once` 只做"磁盘有、DB 无 → 入库"的**单向增量**,从不做反向同步。所以直接 `rm` 一本书后:

- watchdog 收到 deleted 事件 → 无 handler → 什么都不发生
- DB 里那条 `books` 记录**残留**,封面图残留,首页照常显示这本书
- 点进去读才会因原文件缺失报错

**目标**:删书正式功能改为"文件系统删文件 → watcher 同步清库"。移除旧的 `.trash` API(UI 不做),删书唯一入口 = 文件系统操作。加兜底:定时全量扫描做反向同步,覆盖 inotify 在网络挂载/NFS 上漏触发。

## 4. 任务报告

### 周期 1:RED — `ingest.remove_book` 单元测试

**执行:** 在 `tests/test_ingest.py` 末尾新增 `TestRemoveBook`,5 个测试覆盖删 DB 行 / CASCADE 进度划线 / 删封面 / 不存在 id 返回 False / 封面已缺失不报错。

**验证命令:**
```bash
uv run pytest tests/test_ingest.py::TestRemoveBook -v
```

**RED 输出:**
```
FAILED tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_db_row
FAILED tests/test_ingest.py::TestRemoveBook::test_remove_book_cascades_progress_and_highlights
FAILED tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_cover_file
FAILED tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_id_returns_false
FAILED tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_cover_no_error
E       AttributeError: module 'app.ingest' has no attribute 'remove_book'
5 failed, 1 warning in 0.21s
```

失败由目标缺失导致(`remove_book` 未实现)。RED checkpoint commit:`cf6e1d7`。

### 周期 1:GREEN — 实现 `remove_book`

**执行:** `app/ingest.py` 新增 `remove_book(book_id)`:删 books 行(ON DELETE CASCADE 清进度/划线)+ 删封面 `{file_hash}.jpg`(用 `COVER_DIR` 定位,与入库对称)。封面缺失不阻断,不存在 id 返回 False。

**验证命令:**
```bash
uv run pytest tests/test_ingest.py::TestRemoveBook -v
```

**GREEN 输出:**
```
tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_db_row PASSED [ 20%]
tests/test_ingest.py::TestRemoveBook::test_remove_book_cascades_progress_and_highlights PASSED [ 40%]
tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_cover_file PASSED [ 60%]
tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_id_returns_false PASSED [ 80%]
tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_cover_no_error PASSED [100%]
5 passed, 1 warning in 0.22s
```

GREEN checkpoint commit:`eec103b`。

### 周期 2:RED — watcher `on_deleted` + 反向同步测试

**执行:** 在 `tests/test_watcher.py` 末尾新增 `TestLibraryHandlerDeletion`,8 个测试:`on_deleted` 按 path 清库 / 忽略非 epub / 忽略目录事件 / path 不在 DB 不抛;`_purge_missing` 删 DB 行 / 删封面 / 保留存在文件;`_scan_once` 正反向端到端。

**验证命令:**
```bash
uv run pytest tests/test_watcher.py::TestLibraryHandlerDeletion -v
```

**RED 输出:**
```
FAILED tests/test_watcher.py::TestLibraryHandlerDeletion::test_on_deleted_clears_db_by_path
FAILED tests/test_watcher.py::TestLibraryHandlerDeletion::test_purge_missing_removes_db_row_without_file
FAILED tests/test_watcher.py::TestLibraryHandlerDeletion::test_purge_missing_deletes_cover
FAILED tests/test_watcher.py::TestLibraryHandlerDeletion::test_purge_missing_keeps_existing_files
FAILED tests/test_watcher.py::TestLibraryHandlerDeletion::test_scan_once_purges_missing
5 failed, 3 passed, 1 warning in 0.41s
```

5 个**正面行为测试**(应清库)失败:`on_deleted`/`_purge_missing` 未实现(AttributeError)+ DB 残留断言不满足。3 个**负面测试**(不应清库:忽略非 epub / 忽略目录 / path 不在 DB)因 `FileSystemEventHandler` 基类空 `on_deleted` 恰满足"什么都不做"预期而通过——符合 RED 定义(正面测试因功能未实现而失败,失败原因正确,非语法/夹具问题)。RED checkpoint commit:`4ac8a3a`。

### 周期 2:GREEN — 实现 `on_deleted` + `_purge_missing`

**执行:** `app/watcher.py`:
- `LibraryHandler.on_deleted`:deleted 事件 → 非 epub/目录/不在 DB 跳过 → `_delete_by_path` 按 `original_path` 反查 `book_id` → `ingest.remove_book`
- `LibraryWatcher._purge_missing`:全量扫 `books` 行,原文件不存在则 `ingest.remove_book`,返回被清 id 列表
- `_scan_once` 扩展:正向入库(磁盘→DB)+ 反向清库(DB→磁盘)

**验证命令:**
```bash
uv run pytest tests/test_watcher.py::TestLibraryHandlerDeletion -v
uv run pytest tests/test_watcher.py -q   # 含原 10 个 watcher 测试,确认无回归
```

**GREEN 输出:**
```
tests/test_watcher.py::TestLibraryHandlerDeletion::test_on_deleted_clears_db_by_path PASSED [ 12%]
... (8 个全 PASSED)
8 passed, 1 warning in 0.32s

# 全 watcher
18 passed, 1 warning in 0.70s
```

GREEN checkpoint commit:`18f1c8c`。

### 周期 3:移除旧 `.trash` API + 清理测试

**执行:**
- `tests/test_api.py`:删 `TestDeleteBook` 整个类(8 个测试 + `_ingest_copy` 辅助)
- `app/routes/library.py`:删 `DELETE /api/library/{book_id}` 路由(`delete_book`)+ `_library_trash_dir()` + 顶部 `import shutil`

**验证命令:**
```bash
grep -rn "delete_book\|_library_trash_dir\|\.trash" app/ tests/   # 确认无残留
uv run pytest -q                                                    # 全套回归
```

**输出:**
```
(无残留引用)
217 passed, 1 warning in 61.93s
```

refactor checkpoint commit:`8c0ba57`。

## 5. 测试规范

| # | 保证 | 测试 / 命令 | 类型 | 结果 | 证据 |
|---|------|------------|------|------|------|
| 1 | `remove_book` 删后 books 表无该行 | `tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_db_row` | unit | PASS | `uv run pytest tests/test_ingest.py::TestRemoveBook` |
| 2 | 删书级联清 reading_progress + highlights(ON DELETE CASCADE) | `tests/test_ingest.py::TestRemoveBook::test_remove_book_cascades_progress_and_highlights` | unit | PASS | 同上 |
| 3 | 删书后封面 `{file_hash}.jpg` 从 COVER_DIR 删除 | `tests/test_ingest.py::TestRemoveBook::test_remove_book_deletes_cover_file` | unit | PASS | 同上 |
| 4 | 删不存在的 book_id 返回 False,不抛 | `tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_id_returns_false` | unit | PASS | 同上 |
| 5 | 封面文件已不存在时删书不报错(DB 仍清) | `tests/test_ingest.py::TestRemoveBook::test_remove_book_missing_cover_no_error` | unit | PASS | 同上 |
| 6 | `on_deleted` 事件 → 按 path 反查 → DB 无该行 | `tests/test_watcher.py::TestLibraryHandlerDeletion::test_on_deleted_clears_db_by_path` | unit | PASS | `uv run pytest tests/test_watcher.py::TestLibraryHandlerDeletion` |
| 7 | 删非 epub / 目录事件 / path 不在 DB 不触发清库、不抛 | `test_on_deleted_ignores_non_epub` / `_ignores_directory_event` / `_missing_path_no_error` | unit | PASS | 同上 |
| 8 | `_purge_missing`:DB 有行但文件已 rm → 清 DB + 删封面 | `test_purge_missing_removes_db_row_without_file` / `_deletes_cover` | unit | PASS | 同上 |
| 9 | `_purge_missing`:文件还在的书不误删 | `test_purge_missing_keeps_existing_files` | unit | PASS | 同上 |
| 10 | `_scan_once` 同时正向入库 + 反向清库 | `test_scan_once_purges_missing` | integration | PASS | 同上 |
| 11 | 旧 `.trash` API 已移除,无残留引用 | `grep -rn "delete_book\|_library_trash_dir\|\.trash" app/ tests/` | 契约 | PASS | 输出 "(无残留)" |
| 12 | 改动不破坏既有功能 | 全量测试 | regression | PASS | `uv run pytest` → 217 passed |

## 6. 覆盖与已知缺口

**覆盖率命令:**
```bash
uv run pytest tests/test_watcher.py tests/test_ingest.py \
  --cov=app.watcher --cov=app.ingest --cov-report=term-missing
```

**结果:**
```
Name             Stmts   Miss  Cover   Missing
----------------------------------------------
app/ingest.py      131      8    94%   104-105, 120, 125, 130-131, 263-264
app/watcher.py     121     18    85%   37-38, 41-42, 46-47, 85-86, 89-91, 127-128, 136-139, 148
TOTAL              252     26    90%
```

两个模块均 ≥ 80% 阈值。本次新增的 `on_deleted` / `_delete_by_path` / `_purge_missing` / `_scan_once` 主体路径全覆盖。

**已知缺口(均为原有后台线程异常分支,非本次新增代码盲点):**
- `watcher.py` 85-86 / 89-91:`flush_stable` 的 ready 分支与 `ingest_file` 返回 None 分支(需真实去抖时序)
- `watcher.py` 127-128 / 136-139:`_flush_loop` / `_scan_loop` 的 `except` 分支(需真实后台线程异常)
- `watcher.py` 148:`_scan_once` 的 `library_dir.is_dir()` 为 False 早退
- `ingest.py` 263-264:`_enrich_async` 的兜底 `except`(需 enrich 线程内未捕获异常)

这些是后台守护线程的异常路径,沿用本项目"不依赖真实 inotify、不引真实网络"的测试约定,不强行补测。

## 7. Merge 证据

五个 checkpoint commit(未 squash,PR 保留完整 RED/GREEN/refactor 历史,均在 `feat/v0.4-ai-brains` 分支):

- `cf6e1d7` test: add reproducer for remove_book 清库删封面 (RED)
- `eec103b` fix: remove_book 清库删封面 (GREEN)
- `4ac8a3a` test: watcher on_deleted + 反向同步 (RED)
- `18f1c8c` fix: watcher 文件删书同步清库 (GREEN)
- `8c0ba57` refactor: 移除 .trash 删书 API,文件系统删书为正式功能

## 8. 端到端验证(手动,可选)

启动服务 → 放一本 epub 入库 → `rm books-library/xxx.epub` → `on_deleted` 实时清库(或 ≤10 分钟 `_scan_loop` 兜底)→ 书架不再显示该书、`data/covers/` 对应封面消失。
