"""集中配置:v0.3 让服务可跨设备部署。

所有路径/网络项从环境变量读默认值,但仍是模块级可变属性——
测试用 monkeypatch.setattr 覆盖它们来隔离 DB/书库/封面目录,
所以不能用常量或函数内局部变量。

容器内约定挂载点(见 docker-compose.yml):
  /data          -> DB + 封面
  /books-library -> 书库原文件(watcher 监听源)
宿主路径由 compose 配,容器内固定。
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(var: str, default: Path) -> Path:
    """从 env 读路径,无则用 default。不做 mkdir(由调用方负责)。"""
    v = os.environ.get(var)
    return Path(v) if v else default


# 网络:绑 0.0.0.0 放开到 LAN(单用户边界,无鉴权——v0.3 显式取舍)。
HOST = os.environ.get("READFLOW_HOST", "0.0.0.0")
PORT = int(os.environ.get("READFLOW_PORT", "8765"))

# 数据目录:DB + 封面图。默认仓库内 data/,容器内 /data。
DATA_DIR = _env_path("READFLOW_DATA_DIR", _REPO_ROOT / "data")
DB_PATH = DATA_DIR / "readflow.db"
COVER_DIR = DATA_DIR / "covers"

# 书库目录:watcher 监听源 + epub 原文件存放。默认仓库内,容器内 /books-library。
LIBRARY_DIR = _env_path("READFLOW_LIBRARY_DIR", _REPO_ROOT / "books-library")

# 静态资源:打进镜像,不外置。
STATIC_DIR = Path(__file__).resolve().parent / "static"
