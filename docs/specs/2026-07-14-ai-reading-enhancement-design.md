# ReadFlow AI 增强 — 第二大脑 & 知识盲点

> 设计文档 | 2026-07-14 | t91 & Codex

## 1. 概述

在 MRD v0.3 已有 AI 能力（元数据补全）基础上，新增阅读内容 AI 分析层，实现两个核心能力：

- **第二大脑**：阅读内容自动沉淀为知识点卡片，可搜索、可回顾
- **知识盲点驱动推荐**：AI 分析你的知识结构，发现盲区，从互联网推荐补缺书籍

### 简版数据流

```
打开书 → 翻页（前端只存起止 CFI）
    ↓
关书 → POST /api/books/{id}/reading-session
    ↓
后端从 epub 提取 CFI 区间纯文本 → INSERT reading_log
    ↓
每日凌晨批处理（GLM）→ knowledge_cards（20 张/天）
    ↓
主页卡片流 + 筛选 + 搜索
```

## 2. 新增数据库表

### 2.1 reading_log（阅读会话日志）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| book_id | INTEGER NOT NULL | FK → books(id) |
| start_cfi | TEXT | 本次会话起始 CFI |
| end_cfi | TEXT | 本次会话结束 CFI |
| text | TEXT NOT NULL | 后端从 epub 提取的纯文本 |
| percent_from | REAL | 起始进度百分比 |
| percent_to | REAL | 结束进度百分比 |
| created_at | TEXT | 时间戳 |

### 2.2 knowledge_cards（知识卡片）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| card_type | TEXT NOT NULL | `knowledge` / `blind_spot` / `recommendation` |
| title | TEXT NOT NULL | 卡片标题 |
| body | TEXT NOT NULL | 卡片正文 |
| book_id | INTEGER | FK → books(id)，仅 knowledge 用；其余 NULL |
| source_type | TEXT | `reading_log` / `highlight`，仅 knowledge 用；其余 NULL |
| source_ids | TEXT | JSON 数组，关联原料行 ID；仅 knowledge 用 |
| parent_card_id | INTEGER | recommendation 关联的 knowledge/blind_spot 卡片 ID，一对一 |
| recommend_book | TEXT | JSON `{title, author, reason, summary, isbn}`，仅 recommendation 用 |
| card_metadata | TEXT | JSON，弹性字段（领域标签、观点立场等） |
| created_at | TEXT | 时间戳 |

**card_type 说明：**

| 类型 | 含义 | 数量/天 |
|---|---|---|
| `knowledge` | 从阅读内容提炼的知识点 | 5 |
| `blind_spot` | 知识盲点（缺的领域/视角） | 5 |
| `recommendation` | 互联网推荐书籍 | 10 |

## 3. reading_log 写入（前端 + 后端）

### 3.1 前端（reader.js）

- 打开书籍时：内存记录 `start_cfi`
- 翻页（relocate 事件）：更新内存中的最新 CFI、最新 percent
- 关闭页面（beforeunload / visibilitychange）：POST `/api/books/{book_id}/reading-session`

请求体：
```json
{
  "start_cfi": "epubcfi(/6/4[chap01]!/4/2/4)",
  "end_cfi": "epubcfi(/6/4[chap01]!/4/16/2)",
  "percent_from": 0.30,
  "percent_to": 0.45
}
```

- 若 start_cfi == end_cfi（没翻页或只翻一页），不发送请求
- 崩溃容忍：CFI 已存后端进度表，不丢位置；本次会话文本丢失（可接受）

### 3.2 后端（API + epub 提取）

新增路由：`POST /api/books/{book_id}/reading-session`

处理逻辑：
1. 校验 book_id 存在
2. 用 ebooklib 打开 epub 文件
3. 定位 start_cfi 到 end_cfi 之间的章节 HTML
4. 剥离 HTML 标签 → 纯文本
5. INSERT INTO reading_log
6. 返回 201

**轻量原则：** 不做 CFI 逐节点解析，只需定位到章节级别取文本即可。跨章节则拼接。

## 4. 每日凌晨批处理

APScheduler 定时任务（凌晨 2:00）。

### 步骤 1：生成 knowledge + blind_spot 卡片

```
输入：
  - 最近 24h 的 reading_log（按 book_id 聚合文本）
  - 最近 24h 的 highlights
  - 书库所有已有的 knowledge_cards
  - 书库 books.tags

GLM prompt 要求：
  - 产出 5 张 knowledge 卡片：具体知识点（概念、观点、事实）
    - title: 知识点标题
    - body: 100-200 字解释
    - book_id: 来源书
    - source_type/source_ids: 溯源
  - 产出 5 张 blind_spot 卡片：知识盲点
    - title: 盲点标题（如「缺少认知心理学视角」）
    - body: 100-200 字解释你缺什么、为什么重要

返回 JSON，解析后 INSERT INTO knowledge_cards
```

### 步骤 2：生成 recommendation 卡片

```
输入：
  - 步骤 1 产出的 5 张 knowledge + 5 张 blind_spot

每张 knowledge → 1 张 recommendation:
  - 智谱 Web Search 搜相关书籍
  - recommend_book.reason 必须引用该知识点的具体内容
    例: "基于你读到的「幸存者偏差」概念，推荐这本书因为它深入剖析了二战时期的统计谬误案例"
  - recommend_book.summary: 200-400 字简介

每张 blind_spot → 1 张 recommendation:
  - 智谱 Web Search 搜补盲书籍
  - recommend_book.reason 必须说明补什么缺口
    例: "你的认知心理学方向缺少「具身认知」视角，这本书是该领域核心入门"
  - recommend_book.summary: 200-400 字简介

每张 recommendation 的 parent_card_id 指向对应的 knowledge/blind_spot
```

### 每日产出合计：20 张卡片

| 类型 | 数量 |
|---|---|
| knowledge | 5 |
| blind_spot | 5 |
| recommendation | 10 |
| **合计** | **20** |

## 5. 主页展示

### 5.1 卡片流

- 按 `created_at` 倒序
- card_type 筛选标签：全部 / 知识点 / 盲点 / 推荐
- 搜索框：按 `title` + `body` 关键词搜索

### 5.2 卡片渲染

**knowledge 卡片：**
```
[知识点] 幸存者偏差
来源：《事实》第 4 章
在二战中，统计学家沃尔德建议加固返航飞机上弹孔最少的区域...
```

**blind_spot 卡片：**
```
[盲点] 缺少认知心理学视角
你的阅读偏重社会心理学和经济学，缺少从大脑认知机制出发的视角...
```

**recommendation 卡片：**
```
[推荐] 《思考，快与慢》
推荐理由：基于你读到的「系统1和系统2」概念...
简介：卡尼曼在书中展示了人类认知的两套系统...(200-400 字)
← 关联知识点：幸存者偏差
```

### 5.3 卡片关联展示

- recommendation 卡片下方展示「← 关联知识点/盲点：{parent_card.title}」
- 点击父卡片标题可跳转

## 6. 不做的事

- 不做向量检索（chromadb/lancedb）：当前几百本量级，FTS5 够用
- 不做多周期（weekly/monthly/quarterly/yearly）：只做 daily
- 不做知识图谱可视化：先做好文本卡片流
- 不做书库已有书籍推荐：推荐一律从互联网搜
- 不做 reading_log 按页存储：按阅读会话存

## 7. 测试策略

### reading_log 写入
- 单元：CFI 区间 → epub 文本提取
- 集成：POST /api/books/{id}/reading-session → reading_log 入库
- 边界：空 CFI 区间、跨章节、不存在的 book_id

### 批处理
- 单元：GLM 响应 JSON 解析 → knowledge_cards 入库
- 单元：Web Search → recommend_book JSON 组装
- 集成：批处理全流程（mock GLM + WebSearch）→ 20 张卡片入库
- 边界：无 reading_log（跳过知识卡片）/ 无 highlights / 卡片不足 5 张

### 主页
- 集成：卡片流 API + 筛选 + 搜索

## 8. 变更范围

| 层 | 变更 |
|---|---|
| 数据库 | `reading_log` 表 + `knowledge_cards` 表 |
| 后端 | `POST /api/books/{id}/reading-session` + epub 文本提取 + 批处理定时任务 + 卡片流 API |
| 前端 | reader.js 存起止 CFI + beforeunload POST + 主页卡片流页面 |
| 测试 | TDD 全覆盖 |