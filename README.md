# 书舟 ReadFlow

私人 NAS 阅读服务。把电子书放在自己的硬盘里，用浏览器阅读、记录进度、划线。同一局域网内的手机、平板、Kindle 浏览器都能直接访问。

> v0.3：容器化部署，LAN 跨设备访问，Kindle 友好的 HTML 阅读路径。

---

## 安装与启动

### Docker（推荐）

```bash
git submodule update --init     # 首次 clone 必须初始化阅读器前端
cp .env.example .env             # 可选：填入元数据 API key（无 key 也能用）
docker compose up -d             # 构建并后台启动
```

启动后，浏览器访问 `http://<运行机器的IP>:8765`。

### 本地运行

```bash
uv sync                          # 安装依赖
uv run python -m app             # 启动，默认监听 0.0.0.0:8765
```

## 添加书籍

把 `.epub` 文件放进 `books-library/` 目录，服务会自动扫描入库，几秒后刷新书架即可看到。

入库后异步补全书籍信息（简介、豆瓣评分、标签等）。无 API key 时自动降级，不影响阅读。

## 阅读

### 手机 / 平板

书架点书籍封面进详情页 → 点「开始阅读」。基于 foliate-js 渲染，支持：

- 翻页、目录跳转
- 选中文字**划线**（持久化，重排版不错位）或复制
- 阅读进度自动保存，跨会话/跨设备恢复
- 排版设置（字号 / 行距 / 边距 / 字体），点工具栏「文」按钮

### Kindle

Kindle 实验性浏览器不支持现代 JS，走专用的 HTML 阅读路径（访问阅读页时自动识别 Kindle 并切换）：

- 点屏幕**右侧**翻下一页，**左侧**翻上一页（整页切换，翻页书体验）
- 到章末继续点下一页自动跳下一章；章首点上一页跳上一章
- 点屏幕**顶部中间**唤出工具栏：返回书库、A−/A+ 调字号、上下章
- 字号、字体（霞鹜文楷）自动记忆

> Kindle 路径无划线、进度为章节级（实验性浏览器限制）。手机/平板走 foliate 仍有完整能力。

## 配置

所有配置项有默认值，通常无需修改。Docker 容器内挂载点已预设。

| 配置 | 默认 | 说明 |
|------|------|------|
| `READFLOW_HOST` | `0.0.0.0` | 监听地址，`0.0.0.0` 放开到局域网 |
| `READFLOW_PORT` | `8765` | 端口 |
| `READFLOW_DATA_DIR` | `data/`（容器内 `/data`） | 数据库 + 封面目录 |
| `READFLOW_LIBRARY_DIR` | `books-library/`（容器内 `/books-library`） | 书库目录 |
| `ZHIPU_API_KEY` | — | 智谱 API key，书籍信息主数据源 |
| `GOOGLE_BOOKS_API_KEY` | — | Google Books 兜底数据源（可选） |

API key 写在 `.env` 文件里（不入 git）：

```
ZHIPU_API_KEY=你的智谱key
GOOGLE_BOOKS_API_KEY=你的googlekey
```

## 数据备份

所有数据在两个目录里，备份这两个即可：

- `books-library/` — 原始 epub 文件
- `data/` — 数据库（`readflow.db`）+ 封面图

Docker 用户对应两个 volume 挂载点。

## 安全提示

- **无鉴权，单用户自托管**：默认绑 `0.0.0.0`，同一局域网内任何设备都能访问你的书库和阅读数据。只在可信内网部署；若暴露到公网或半信任网络，请自行加反向代理 + 鉴权。
- 只解析你自己放入书库的 epub 文件。
- API key 在 `.env` 里，已加入 `.gitignore`，切勿提交到 git。
