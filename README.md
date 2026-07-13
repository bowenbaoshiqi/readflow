# 书舟 ReadFlow v0.1

私人 NAS 阅读服务。把你的电子书放在自己硬盘里，用浏览器阅读、划线、记录进度。

> 当前版本 v0.1：支持 epub 格式，包含书库首页、阅读器、划线、阅读进度持久化。

---

## 功能

- **书库监听器**：把 epub 文件放入 `books-library/` 目录，自动入库
- **在线阅读器**：基于 [foliate-js](https://github.com/johnfactotum/foliate-js) 渲染 epub，支持翻页/目录
- **划线与复制**：选中文字划线，划线数据持久化；支持复制选中文本
- **阅读进度**：自动保存章节位置与进度百分比，跨会话/跨设备恢复
- **排版设置**：字号(12-28px)、行距(1.2-2.8)、页边距(窄/中/宽)、字体(霞鹜文楷)，实时预览、localStorage 持久化
- **单文件数据库**：所有数据在 `data/readflow.db`，方便备份

## 快速开始

```bash
uv sync                          # 安装依赖到 .venv/
uv run uvicorn app.main:app --port 8765      # 启动服务
```

浏览器打开 `http://localhost:8765`，把 epub 放进 `books-library/` 即可开始阅读。

详细用户指南请见：[docs/getting-started.md](docs/getting-started.md)

## 技术栈

- 后端：Python FastAPI + SQLite
- 阅读器前端：[foliate-js](https://github.com/johnfactotum/foliate-js)（MIT）
- epub 解析：ebooklib
- 文件监听：watchdog
- 虚拟环境：uv

## 测试

```bash
uv run pytest tests/ --ignore=tests/test_reader_e2e.py
```

当前有 65 个后端/接口单元测试，全部通过。

## 后续计划

- v0.2：格式转换（mobi/azw3/pdf/txt → epub）、全文搜索、外部元数据补全
- 更远：AI 每日总结、推荐、选中文字 AI 解释/翻译

## 安全提示

- 本项目为单用户自托管设计，默认仅监听本机（`127.0.0.1`）
- 仅在声明的可信内网范围开放访问
- ebooklib 有未公开修复的路径遍历安全漏洞，只应解析你自己放入书库的文件

## License

书舟自身代码保留所有权利，暂未指定开源协议。使用的第三方库遵循各自许可证。
