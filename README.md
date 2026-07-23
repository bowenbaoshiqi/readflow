# 书舟 ReadFlow

私人 NAS 阅读服务。把电子书放在自己的硬盘里，用浏览器阅读、记录进度、划线。同一局域网内的手机、平板、Kindle 浏览器都能直接访问。

> v0.4：AI 第二大脑——每日自动从你的阅读中提炼知识卡片、发现知识盲点、推荐补缺书籍。
> v2.5：可靠记录阅读日志、修复知识卡片断档与 EPUB 字号覆盖问题；镜像已发布 GHCR，群晖 NAS 可直接拉取部署。

---

## 功能

### 阅读

- **书库**：把 `.epub` 文件放进书库目录，自动扫描入库，自动补全简介/评分/标签
- **跨设备阅读**：手机、平板、Kindle 浏览器都能直接访问，进度自动同步
- **划线**：选中文字即可划线，重排版/换设备不错位
- **排版可调**：字号 / 行距 / 边距 / 字体
- **Kindle 友好**：自动识别 Kindle，切换为翻页模式（点屏幕翻页，顶部唤出工具栏）

### AI 知识卡片

每天自动为你生成三类卡片：

- **知识点**：从你昨天读过的内容里提炼具体知识点
- **盲点**：分析你的阅读结构，指出你缺失的视角或领域
- **推荐**：针对每个知识点和盲点，推荐一本补缺书，附上推荐理由

在主页点「知识卡片」查看：

- 三个 tab 切换：知识点 / 盲点 / 推荐
- 按日期浏览：顶部列出有卡片的日期，点击切换；最近的日期直达，更早的按月折叠
- 推荐卡片会标注是「盲点推荐」还是「知识点推荐」，并关联对应的知识点/盲点
- 支持关键词搜索

---

## 首次使用

> **群晖 NAS 用户**：直接拉镜像部署，跳到下方 [部署到群晖 NAS](#部署到群晖-nas拉取镜像推荐) 章节，不用自己构建。
> 本节面向从源码构建 / 本地开发的用户。

### 1. 填入 API key

AI 功能需要智谱 API key：

- 到 https://open.bigmodel.cn/ 注册并创建 API key
- 复制 `.env.example` 为 `.env`，填入 key：

```
ZHIPU_API_KEY=你的智谱key
GOOGLE_BOOKS_API_KEY=你的googlekey   # 可选，书籍信息兜底
```

> 无 key 也能阅读，但不会生成知识卡片，书籍信息也会降级。

### 2. 启动

```bash
git submodule update --init     # 首次必须执行
cp .env.example .env            # 填入 API key
docker compose up -d
```

浏览器访问 `http://<运行机器的IP>:8765`。

不用 Docker：

```bash
uv sync
uv run python -m app
```

### 3. 开始阅读

- 把 `.epub` 文件放进 `books-library/`，刷新书架即可看到
- 点封面 → 「开始阅读」
- 正常阅读即可，系统会自动记录你读过的内容作为 AI 提炼的原料
- 第二天凌晨自动生成知识卡片，在主页「知识卡片」查看

---

## 部署到群晖 NAS（拉取镜像，推荐）

不想自己构建镜像？直接拉公开镜像到群晖跑，最省事。镜像已发布到 GitHub Container Registry（公开，免登录拉取）。

适合：群晖 Synology NAS（DSM 7.2+，Intel/AMD 处理器）。手机/平板在同一局域网就能通过 NAS 的 IP 访问。

### 前置：装好 Container Manager

DSM → 套件中心 → 搜索安装 **Container Manager**（群晖的 Docker）。装好后开启 SSH：控制面板 → 终端机和 SNMP → 勾「启用 SSH 功能」。

### 第 1 步：建目录 + 放书

SSH 登录 NAS（`ssh 你的用户名@NAS的IP`），建两个目录：

```bash
# 注意：群晖存储空间可能是 /volume1、/volume2、/volume3……
# 用你 NAS 实际的那个（在 File Station 里看，或 ls /volume* 确认）
sudo mkdir -p /volume1/docker/readflow/data
sudo mkdir -p /volume1/books-library
sudo chown -R $USER /volume1/docker/readflow /volume1/books-library
```

把你的 `.epub` 文件放进 `/volume1/books-library/`（File Station 拖拽上传，或从电脑 `scp` 过去）。

### 第 2 步：写 docker-compose.yml + .env

在 `/volume1/docker/readflow/` 下新建 `docker-compose.yml`（File Station 新建文本文件，或 `vi`），内容原样粘贴：

```yaml
services:
  readflow:
    image: ghcr.io/bowenbaoshiqi/readflow:v2.5
    container_name: readflow
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - /volume1/docker/readflow/data:/data
      - /volume1/books-library:/books-library
    env_file:
      - .env
    environment:
      - READFLOW_HOST=0.0.0.0
      - READFLOW_PORT=8765
      - TZ=Asia/Shanghai        # 修定时任务时区，否则凌晨 2 点会偏到上午 10 点
```

> **路径注意**：上面 `/volume1/` 要换成你 NAS 实际的存储卷。在 File Station 看你的共享文件夹归属哪个 volume（很多人是 `/volume1`，多盘位或改过的是 `/volume2`、`/volume3`）。`volumes:` 里左右两边的路径都得改对。

同目录下建 `.env` 文件，填入智谱 key：

```
ZHIPU_API_KEY=你的智谱key
GOOGLE_BOOKS_API_KEY=你的googlekey   # 可选
```

收紧权限（防其他用户读到 key）：

```bash
chmod 600 /volume1/docker/readflow/.env
```

### 第 3 步：启动

```bash
cd /volume1/docker/readflow
sudo docker compose up -d      # 自动拉镜像 + 启动，首次几十秒到一两分钟
sudo docker compose logs -f readflow   # 看日志
```

看到 `Uvicorn running on http://0.0.0.0:8765` + `Application startup complete` 就成了（`Ctrl+C` 退出查看日志，不会停容器）。

### 第 4 步：访问

浏览器（手机或电脑）打开：

```
http://你NAS的IP:8765
```

看到书架 + 书自动入库 = 成功。第二天凌晨 2 点自动生成知识卡片，在主页「知识卡片」查看。

> **DSM 防火墙**：若 NAS 开了防火墙（控制面板 → 安全性 → 防火墙），加一条规则放行 8765 端口（来源选你的局域网网段）。

### 升级到新版本

代码更新后会有新镜像。NAS 上两条命令升级（数据不丢）：

```bash
cd /volume1/docker/readflow
sudo docker compose pull        # 拉新镜像
sudo docker compose up -d       # 用新镜像重建容器
```

> 想固定版本：把 `docker-compose.yml` 里的 `:v2.5` 换成指定 tag（如 `:sha-<提交号>`）再 `up -d`，可精确回滚。

### 常见问题

**Q: 启动报 `Bind mount failed: ... does not exist`**
A: `docker-compose.yml` 里的 `/volume1/...` 路径在你 NAS 上不存在。改成实际的卷（`/volume2`、`/volume3` 等），并确保 `data/` 目录已建（`mkdir -p`）。

**Q: 知识卡片什么时候生成？**
A: 每天凌晨 2 点（北京时间，靠 `TZ=Asia/Shanghai`）。当天读过书，第二天才有卡片。没填 `ZHIPU_API_KEY` 则不生成。

**Q: 拉镜像报权限错误 / `no matching manifest`？**
A: 镜像是 `linux/amd64`（Intel/AMD NAS）。若 NAS 是 ARM 架构（如部分 DS4xxj）拉不动，需自己构建 arm64 镜像。镜像公开，正常 `docker compose up -d` 会自动拉，无需 `docker login`。

---

## 数据迁移（从另一台机器搬过来）

换机器部署时，把旧数据搬过来，不用重新积累知识卡片。

在**旧机器**上导出（Docker 环境）：

```bash
# 旧机器停容器，让数据库把内存数据写回磁盘
docker compose stop

# 导出 data 卷（DB + 封面）到一个 tar 包
docker run --rm -v 旧机器的data卷名:/data -v "$PWD":/backup alpine \
  tar czf /backup/readflow-data-backup.tar.gz -C /data .
# 产物 readflow-data-backup.tar.gz（通常 <1MB）
```

把 `readflow-data-backup.tar.gz` 拷到新 NAS 的 `/volume1/docker/readflow/`。

在**新 NAS** 上解压（覆盖空库）：

```bash
cd /volume1/docker/readflow
sudo docker compose down                              # 停容器
sudo rm -f data/readflow.db data/readflow.db-wal data/readflow.db-shm   # 清空空库
sudo rm -rf data/covers
sudo tar xzf readflow-data-backup.tar.gz -C data/     # 解压旧数据
sudo docker compose up -d                             # 重启
```

> 书的 epub 文件也要单独拷到新 NAS 的 `/volume1/books-library/`，文件名保持不变（DB 里存的是路径，改名会触发重新入库，进度可能丢）。

---

## 配置

所有配置有默认值，通常无需修改。

| 配置 | 默认 | 说明 |
|------|------|------|
| `READFLOW_HOST` | `0.0.0.0` | 监听地址，放开到局域网 |
| `READFLOW_PORT` | `8765` | 端口 |
| `READFLOW_DATA_DIR` | `data/` | 数据库 + 封面目录 |
| `READFLOW_LIBRARY_DIR` | `books-library/` | 书库目录 |
| `ZHIPU_API_KEY` | — | 智谱 API key，**AI 知识卡片必需** |
| `GOOGLE_BOOKS_API_KEY` | — | Google Books 兜底（可选） |

## 数据备份

备份这两个目录即可：

- `books-library/` — 原始 epub 文件
- `data/` — 数据库 + 封面图

群晖用户：把这两个目录加进 **Hyper Backup** 计划即可（数据库通常 <1MB，epub 也不大，备份成本极低）。NAS 上对应 `/volume1/docker/readflow/data/` 和 `/volume1/books-library/`。

## 安全提示

- **无鉴权，单用户自托管**：默认放开到局域网，只在可信内网部署；暴露公网请自行加反向代理 + 鉴权。
- 只解析你自己放入书库的 epub 文件。
- API key 在 `.env` 里，不入 git。
