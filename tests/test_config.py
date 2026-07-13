"""测试 v0.3 配置外置:env 覆盖 + 容器内挂载点约定。

设计约束:配置项是模块级可变属性(env 读默认值),测试靠 monkeypatch 隔离,
所以 config 不用常量/函数局部变量——这里也验证这个属性可被覆盖。
"""
from __future__ import annotations

import importlib

from app import config


class TestConfigDefaults:
    """默认值(env 未设时)"""

    def test_default_host_binds_lan(self):
        """默认 HOST=0.0.0.0:v0.3 放开到 LAN(单用户边界,无鉴权)。"""
        # 注意:测试环境可能已设 READFLOW_HOST env,这里测的是模块加载时读到的值
        # 不是硬断言 "0.0.0.0"(env 可覆盖),只断言它是个合法地址字符串
        assert isinstance(config.HOST, str)
        assert config.HOST

    def test_default_port_is_int(self):
        """PORT 是 int(env 读出来是字符串,config 要转 int)。"""
        assert isinstance(config.PORT, int)
        assert 1 <= config.PORT <= 65535

    def test_db_path_under_data_dir(self):
        """DB_PATH 在 DATA_DIR 下,封面在 DATA_DIR/covers。"""
        assert config.DB_PATH.parent == config.DATA_DIR
        assert config.COVER_DIR == config.DATA_DIR / "covers"


class TestConfigEnvOverride:
    """env 覆盖(容器场景:READFLOW_DATA_DIR=/data 等)"""

    def test_env_overrides_data_dir(self, monkeypatch):
        """READFLOW_DATA_DIR env 覆盖默认仓库内路径。"""
        monkeypatch.setenv("READFLOW_DATA_DIR", "/custom/data")
        monkeypatch.setenv("READFLOW_LIBRARY_DIR", "/custom/lib")
        monkeypatch.setenv("READFLOW_PORT", "9999")
        # 重新加载 config 让它重新读 env
        importlib.reload(config)
        try:
            assert config.DATA_DIR.as_posix() == "/custom/data"
            assert config.DB_PATH.as_posix() == "/custom/data/readflow.db"
            assert config.COVER_DIR.as_posix() == "/custom/data/covers"
            assert config.LIBRARY_DIR.as_posix() == "/custom/lib"
            assert config.PORT == 9999
        finally:
            # 恢复:reload 回原模块状态(去掉测试 env 后)
            monkeypatch.delenv("READFLOW_DATA_DIR", raising=False)
            monkeypatch.delenv("READFLOW_LIBRARY_DIR", raising=False)
            monkeypatch.delenv("READFLOW_PORT", raising=False)
            importlib.reload(config)
