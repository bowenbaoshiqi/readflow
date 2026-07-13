"""直接启动入口:python -m app  或容器 CMD ["python","-m","app"]。

不用 uvicorn CLI:省一层,host/port 直接从 config(env)读。
单 worker、无 reload——多 worker 会起多个 watcher 重复入库 + 抢 DB。
"""
from __future__ import annotations

import uvicorn

from .config import HOST, PORT


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        workers=1,
        reload=False,
    )


if __name__ == "__main__":
    main()
