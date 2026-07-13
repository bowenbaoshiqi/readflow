# 书舟 ReadFlow v0.2

私人 NAS 阅读服务。把你的电子书放在自己硬盘里，用浏览器阅读、划线、记录进度。

> 当前版本 v0.2：epub 阅读基础上新增排版设置、书籍详情页、外部元数据自动补全（豆瓣简介 + 评分）。

---

## 功能

### 阅读核心（v0.1）

- **书库监听器**：把 epub 文件放入 `books-library/` 目录，自动入库
- **在线阅读器**：基于 [foliate-js](https://github.com/johnfactotum/foliate-js) 渲染 epub，支持翻页/目录
- **划线与复制**：选中文字划线，划线数据持久化；支持复制选中文本
- **阅读进度**：自动保存章节位置与进度百分比，跨会话/跨设备恢复
- **单文件数据库**：所有数据在 `data/readflow.db`，方便备份

### 排版设置（v0.2）

- **字号/行距/边距/字体**：字号 12-28px、行距 1.2-2.8、页边距窄/中/宽、字体霞鹜文楷
- 实时预览、localStorage 持久化，跨会话恢复

### 书籍详情页 + 外部元数据（v0.2）

- **书籍详情页**：封面、简介、豆瓣评分、标签、ISBN、阅读进度一站式展示
- **自动补全元数据**：入库后异步拉取豆瓣简介和评分，无需手动填
  - 数据源优先级：**智谱 Web Search（搜豆瓣摘要）→ GLM 大模型解析**为主源；Google Books 兜底
  - 查询用 `{书名} {作者} 豆瓣 简介 评分`，GLM 解析时校验摘要确实是本书（防误存无关内容）
  - 失败不阻断入库：元数据补全失败的书照常可读，状态标 `not_found`/`failed`
- **封面按内容 hash 命名**：封面文件用 `file_hash` 命名而非自增 id，避免删书/重入库时串图

## 快速开始

```bash
uv sync                          # 安装依赖到 .venv/
cp .env.example .env             # 复制环境变量模板,填入 API key
uv run uvicorn app.main:app --port 8765      # 启动服务
```

浏览器打开 `http://localhost:8765`，把 epub 放进 `books-library/` 即可开始阅读。

详细用户指南请见：[docs/getting-started.md](docs/getting-started.md)

## 环境变量

元数据补全需要智谱 API key（[获取](https://open.bigmodel.cn/)），配置在 `.env`（不入 git）：

```
ZHIPU_API_KEY=你的智谱key          # 主数据源:Web Search + GLM 解析
GOOGLE_BOOKS_API_KEY=你的googlekey  # 兜底数据源(可选)
```

无 key 时元数据补全自动降级，不影响阅读；epub 内嵌的简介/ISBN 等仍会显示。

## 技术栈

- 后端：Python FastAPI + SQLite
- 阅读器前端：[foliate-js](https://github.com/johnfactotum/foliate-js)（MIT）
- epub 解析：ebooklib
- 文件监听：watchdog
- 元数据：智谱 Web Search + GLM 大模型、Google Books API
- 虚拟环境：uv

## 测试

```bash
uv run pytest tests/ --ignore=tests/test_reader_e2e.py
```

当前有 149 个后端/接口单元测试，全部通过。

## 后续计划

- v0.3：格式转换（mobi/azw3/pdf/txt → epub）、全文搜索
- 更远：AI 每日总结、推荐、选中文字 AI 解释/翻译

## 安全提示

- 本项目为单用户自托管设计，默认仅监听本机（`127.0.0.1`）
- 仅在声明的可信内网范围开放访问
- ebooklib 有未公开修复的路径遍历安全漏洞，只应解析你自己放入书库的文件
- **API key 存在 `.env`，已加入 `.gitignore`，切勿提交到 git**

## License

书舟自身代码保留所有权利，暂未指定开源协议。使用的第三方库遵循各自许可证。
