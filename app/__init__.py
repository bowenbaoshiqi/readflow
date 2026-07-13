"""书舟 ReadFlow - 基于 NAS 的私人阅读服务。"""
import os
from pathlib import Path

__version__ = "0.1.0"


def _load_env() -> None:
    """从项目根 .env 加载环境变量(最小实现,不引 python-dotenv)。

    只读 KEY=VALUE 行,不覆盖已存在的环境变量(系统已设的优先)。
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()
