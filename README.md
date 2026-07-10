# 书舟 ReadFlow

基于 NAS 的私人阅读服务。当前 v0.1:监听入库 + 在线阅读 epub + 划线 + 阅读进度。

## 当前版本 (v0.1)

- ✅ 监听 `books-library/` 目录,epub 新文件自动入库(文件 hash 去重)
- ✅ ebooklib 提取书名/作者/封面
- ✅ foliate-js 渲染 epub(CFI 定位、分页、暗色模式)
- ✅ 阅读进度持久化(spine_index + CFI,跨设备精准)
- ✅ 选中文字划线(CFI 锚点,重排版不错位)
- 🚧 后续版本:格式转换(mobi/azw3/pdf/txt→epub)、外部元数据、全文搜索、每日总结、推荐、AI 解释/翻译

## 开发环境

```bash
# 安装依赖(虚拟环境在 .venv/,不污染系统)
uv sync

# 启动开发服务器
uv run uvicorn app.main:app --reload

# 打开 http://localhost:8000
# 把 epub 文件放入 books-library/ 即自动入库
```

## 架构

| 层 | 选型 |
|---|---|
| 后端 | Python FastAPI + SQLite |
| 文件监听 | watchdog + 定时全量扫描兜底 |
| epub 解析 | ebooklib |
| 阅读器前端 | foliate-js (MIT, git submodule) |

## 目录

```
app/           后端(FastAPI)
  main.py      入口
  db.py        SQLite schema
  ingest.py    ebooklib 解析 + 去重
  watcher.py   watchdog 监听
  routes/      API 路由
  static/      前端(foliate-js + reader.js + css)
books-library/ 书库目录(放 epub)
data/          sqlite db + 封面(本地,不入库)
```

## 安全

- ebooklib 有未修路径遍历漏洞,只解析自己放入书库的文件
- epub 可含脚本内容,foliate-js 要求配 CSP(自托管可信任书源)
