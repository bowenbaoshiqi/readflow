"""测试数据库模块:schema 初始化、事务一致性、外键行为。"""
from __future__ import annotations

import sqlite3

from app import db


class TestInitDb:
    """数据库初始化"""

    def test_init_db_creates_tables(self, tmp_path, monkeypatch):
        """init_db 后应有 books/reading_progress/highlights 表。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "init.db")
        db.init_db()
        conn = db.get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {t["name"] for t in tables}
        assert "books" in names
        assert "reading_progress" in names
        assert "highlights" in names
        conn.close()

    def test_init_db_idempotent(self, tmp_path, monkeypatch):
        """重复 init_db 不应抛异常。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "idempotent.db")
        db.init_db()
        db.init_db()
        db.init_db()
        assert True


class TestConnection:
    """连接属性"""

    def test_get_conn_returns_row_factory(self, tmp_path, monkeypatch):
        """get_conn 返回 Row 工厂。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "conn.db")
        db.init_db()
        conn = db.get_conn()
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1
        conn.close()

    def test_get_conn_enables_wal(self, tmp_path, monkeypatch):
        """WAL 模式已启用。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "wal.db")
        db.init_db()
        conn = db.get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.upper() == "WAL"
        conn.close()

    def test_get_conn_enables_foreign_keys(self, tmp_path, monkeypatch):
        """外键约束已启用。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "fk.db")
        db.init_db()
        conn = db.get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()

    def test_get_conn_sets_busy_timeout(self, tmp_path, monkeypatch):
        """busy_timeout 已设:多设备并发写时自动重试而非立即 locked。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "busy.db")
        db.init_db()
        conn = db.get_conn()
        # PRAGMA busy_timeout 返回毫秒值
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert bt == 5000
        conn.close()


class TestTransactionContext:
    """事务上下文 db()"""

    def test_db_commits_on_success(self, tmp_path, monkeypatch):
        """无异常时自动 commit。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "tx.db")
        db.init_db()
        with db.db() as conn:
            conn.execute("INSERT INTO books(file_hash, original_path) VALUES('abc', '/x')")
        conn2 = db.get_conn()
        n = conn2.execute("SELECT COUNT(*) FROM books WHERE file_hash='abc'").fetchone()[0]
        assert n == 1
        conn2.close()

    def test_db_rollback_on_exception(self, tmp_path, monkeypatch):
        """异常时自动 rollback。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "rollback.db")
        db.init_db()
        try:
            with db.db() as conn:
                conn.execute("INSERT INTO books(file_hash, original_path) VALUES('def', '/y')")
                raise RuntimeError("模拟异常")
        except RuntimeError:
            pass
        conn2 = db.get_conn()
        n = conn2.execute("SELECT COUNT(*) FROM books WHERE file_hash='def'").fetchone()[0]
        assert n == 0
        conn2.close()


class TestSchemaIntegrity:
    """schema 约束"""

    def test_books_file_hash_unique(self, tmp_path, monkeypatch):
        """file_hash UNIQUE 约束生效。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "uniq.db")
        db.init_db()
        db.init_db()
        with db.db() as conn:
            conn.execute("INSERT INTO books(file_hash, original_path) VALUES('same', '/a')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO books(file_hash, original_path) VALUES('same', '/b')")

    def test_progress_cascade_delete(self, tmp_path, monkeypatch):
        """删除 books 时级联删除 reading_progress。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "cascade.db")
        db.init_db()
        with db.db() as conn:
            conn.execute("INSERT INTO books(file_hash, original_path) VALUES('casc', '/z')")
            conn.execute("INSERT INTO reading_progress(book_id, spine_index, percent) VALUES(1, 0, 0)")
            conn.execute("DELETE FROM books WHERE id=1")
        conn2 = db.get_conn()
        n = conn2.execute("SELECT COUNT(*) FROM reading_progress WHERE book_id=1").fetchone()[0]
        assert n == 0
        conn2.close()


import pytest
