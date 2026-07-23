# ReadFlow 可靠阅读日志设计

日期：2026-07-23  
目标分支：`v2.5`

## 背景

NAS 生产数据库显示，阅读进度持续更新，但 `reading_log` 在 2026-07-16
之后不再产生。当前实现只在页面触发 `beforeunload` 时提交阅读会话；该事件在
移动浏览器切换应用、锁屏、回收后台页面等场景中不可靠，而且请求失败被静默
忽略。

后端文本提取还有独立缺陷：现有正则把 EPUB CFI 的第一个 `/6` 当作 spine
索引，导致不同阅读位置经常被解析到同一个章节。

## 目标

- 阅读期间每隔约 5 分钟可靠保存一段实际发生过位置变化的阅读日志。
- 跨章节、返回书库和页面进入后台时立即保存尚未提交的区间。
- 网络重试不得产生重复日志。
- 从 CFI 定位正确的 EPUB spine 项，避免记录无关章节。
- 保存失败和跳过原因可观察、可测试。
- 保持现有阅读进度、划线和每日卡片 API 的兼容性。

## 非目标

- 不重做阅读器 UI。
- 不追踪停留时长、滚动速度或用户行为分析。
- 不回填 2026-07-17 之后已经丢失的阅读正文；数据库中没有足够信息恢复这些
  精确区间。
- 不改变每日知识卡片的生成数量和提示词。

## 方案选择

采用“时间与进度混合提交”：

- 只按固定时间提交容易在没有实际阅读时生成空日志。
- 只按进度阈值提交会遗漏长时间精读但移动较少的场景。
- 混合方案仅在位置发生变化后启动待提交状态，并结合周期检查与生命周期事件，
  在可靠性和日志质量之间取得平衡。

## 前端设计

### 会话状态

阅读器维护以下内存状态：

- `sessionId`：打开一本书时生成的 UUID。
- `segmentNo`：从 1 递增的日志片段序号。
- `pendingStart`：当前未提交片段的起始 CFI、全书进度和 spine index。
- `pendingEnd`：最近一次 `relocate` 的 CFI、全书进度和 spine index。
- `dirty`：起止位置不同且存在实际位置变化时为真。
- `inFlight`：当前正在提交的 Promise，防止多个触发器并发提交同一片段。

首次有效 `relocate` 只建立起点。后续位置变化更新终点并设置 `dirty`。提交
成功后，以已提交终点作为下一片段起点，递增 `segmentNo`，并清除 `dirty`。

恢复历史进度引发的初始导航不算阅读变化，避免打开后立即生成日志。

### 提交时机

- 每 60 秒检查一次；若片段已持续至少 5 分钟且 `dirty`，执行普通异步提交。
- 跨到另一个 spine 项时立即提交上一片段。
- 点击“返回书库”前等待普通提交完成，再跳转。
- `visibilitychange` 变为 `hidden` 或收到 `pagehide` 时，使用
  `navigator.sendBeacon`；不可用时退回 `fetch(..., keepalive: true)`。
- `beforeunload` 仅保留为最后兜底，不承担主要可靠性职责。

短于 5 分钟的阅读仍会在跨章节、返回或进入后台时保存。没有位置变化则不提交。

### 失败处理

普通提交只有在收到成功响应后才推进片段起点。网络错误或非 2xx 响应保留
`dirty` 状态，下一次触发器重试同一个 `sessionId + segmentNo`。

生命周期提交无法可靠读取响应，因此也不提前递增客户端片段状态；服务端幂等
约束负责吸收随后可能发生的重复提交。开发者控制台记录简短错误，不向阅读者
弹阻塞对话框。

## API 与数据库设计

保留端点：

`POST /api/books/{book_id}/reading-session`

请求新增字段：

```json
{
  "session_id": "UUID",
  "segment_no": 1,
  "start_cfi": "epubcfi(...)",
  "end_cfi": "epubcfi(...)",
  "start_spine_index": 3,
  "end_spine_index": 4,
  "percent_from": 0.12,
  "percent_to": 0.14
}
```

`session_id` 和 `segment_no` 对新客户端必填。服务端暂时接受缺少这两个字段的
旧客户端请求，确保滚动升级期间兼容。

`reading_log` 新增可空列：

- `session_id TEXT`
- `segment_no INTEGER`
- `start_spine_index INTEGER`
- `end_spine_index INTEGER`

创建部分唯一索引：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_log_session_segment
ON reading_log(session_id, segment_no)
WHERE session_id IS NOT NULL AND segment_no IS NOT NULL;
```

该索引允许历史记录保持空值，并保证新请求幂等。

响应状态：

- 新建成功：`{"ok": true, "status": "created", "id": N}`
- 已存在：`{"ok": true, "status": "duplicate", "id": N}`
- 没有有效移动：`{"ok": true, "status": "skipped", "reason": "..."}`
- 文件、CFI 或提取错误：使用明确的 4xx/5xx 响应，并写入服务日志。

## CFI 与正文提取

前端已经从 Foliate `relocate` 事件获得 renderer 的 `index`，新 API 将该值作为
spine index 一起提交。后端以经过范围校验的 `start_spine_index` 和
`end_spine_index` 为主要章节定位依据，不再从 CFI 的第一个数字猜测章节。

CFI 保留用于恢复位置、审计和未来实现段落级裁剪。本次修复按章节提取：

- 同一 spine 项：提取该章节正文。
- 跨 spine 项：按阅读方向拼接范围内的可读章节。
- 忽略 `linear="no"`、脚本、样式和空正文。
- 索引越界、反向区间或 EPUB 无法读取时返回明确错误。

本次不实现 CFI DOM 路径级的首尾字符精确裁剪。卡片任务需要的是足量语义正文，
章节级提取已经满足目标，同时显著降低 EPUB 方言兼容风险。

## 每日卡片衔接

每日任务继续读取最近 24 小时的 `reading_log` 和 `highlights`。新增日志无需改变
调度接口。

任务无输入时应输出包含计数的日志，例如：

```text
[readflow] daily_cards skipped: reading_logs=0 highlights=0
```

有输入、生成失败或成功时也记录对应数量，便于从 NAS 容器日志区分“没有阅读
原料”和“模型调用失败”。

## 数据迁移

迁移使用现有 `init_db()` 幂等模式：

1. 读取 `PRAGMA table_info(reading_log)`。
2. 逐列执行缺失的 `ALTER TABLE ... ADD COLUMN`。
3. 创建部分唯一索引。

迁移不修改现有日志，不需要停机导出或重建数据库。

## 测试与验收

### 单元测试

- 数据库迁移可重复执行，历史空值记录不冲突。
- 同一 `session_id + segment_no` 重试只生成一条日志。
- spine 索引范围校验、同章提取、跨章提取、反向区间和空正文行为正确。
- 卡片任务在有最近日志和无最近日志时行为正确。

### 前端契约与集成测试

- 位置没有变化时不提交。
- 有变化且达到 5 分钟时提交。
- 跨章节、返回书库、`visibilitychange` 和 `pagehide` 会触发提交。
- 普通请求失败后不推进片段，下一次使用相同幂等键重试。
- 成功后下一片段从上一片段终点开始。

### 端到端验收

1. 在手机打开一本 EPUB，阅读并滚动超过 5 分钟。
2. 切换到其他应用，再返回阅读器。
3. 返回书库后检查数据库。
4. 确认至少存在一条对应书籍的新 `reading_log`，正文来自实际阅读章节。
5. 重放同一请求，确认日志数量不增加。
6. 运行每日任务，确认它读取该日志并生成卡片或输出明确的外部 API 错误。

## 完成标准

- 手机正常阅读不依赖关闭标签页也能产生日志。
- 生命周期事件丢失最多影响最后不足 5 分钟的未提交片段。
- 重试不会重复写入。
- 日志正文与实际 spine 范围一致。
- NAS 容器日志可以判断会话为何创建、跳过或失败。
