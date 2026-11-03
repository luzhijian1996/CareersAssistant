"""SQLite 连接与建表。

库文件默认落在用户目录（~/.careersassistant/profile.db），与岗位搜索缓存分库，
不放工程根目录：WAL 会额外产生 -wal / -shm 附属文件，容易误提交。
WAL + busy_timeout 是并发前提——Agent 的异步任务与前端请求可能同时读写。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 表结构版本，写入 PRAGMA user_version；高于它的文件会被拒绝打开
SCHEMA_VERSION = 1

DEFAULT_DB_PATH = Path.home() / ".careersassistant" / "profile.db"
DB_PATH_ENV = "CAREERS_PROFILE_DB"

_DDL = """
CREATE TABLE IF NOT EXISTS profile (
    profile_id  TEXT PRIMARY KEY,
    data        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS experience (
    -- 更新 profile 行时务必用 ON CONFLICT DO UPDATE：INSERT OR REPLACE 会“先删再插”，
    -- 下面的 ON DELETE CASCADE 会把经历全部带走
    exp_id      TEXT    PRIMARY KEY,
    profile_id  TEXT    NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    data        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
    proj_id     TEXT    PRIMARY KEY,
    profile_id  TEXT    NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    data        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- 审计表：每次写入前的旧状态整份留档，供 diff 预览与回滚。
-- 刻意不建外键：画像行被清空后，历史仍需可回滚。
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    data        TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_version ON audit(profile_id, version);
"""


class SchemaVersionError(RuntimeError):
    """库文件由更高版本的程序创建，降级打开会读坏数据。"""


def utc_now() -> str:
    """统一的 UTC 时间串。updated_at / version 只允许存储层生成，
    否则 last-write-wins 的先后判定会失真。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_db_path(override: str | Path | None = None) -> Path:
    """优先级：显式入参 > 环境变量 CAREERS_PROFILE_DB > 用户目录默认路径。

    环境变量是给单测和"换一台机器/多套数据"用的，不要求改代码。
    """
    if override:
        return Path(override).expanduser()
    from_env = os.getenv(DB_PATH_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_DB_PATH


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立连接并确保表存在。调用方负责 close()。"""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：Agent 通过 asyncio.to_thread 调本层，
    # 真正的串行化由 ProfileStore 内部的锁负责
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"库文件结构版本 {current} 高于当前程序支持的 {SCHEMA_VERSION}，请升级程序后再读"
        )
    if current == 0:
        conn.executescript(_DDL)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return
    # 低版本升级占位：未来的 ALTER TABLE 按 current 逐级往上迁移写在这里
    conn.executescript(_DDL)
    conn.commit()


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_DB_PATH",
    "DB_PATH_ENV",
    "SchemaVersionError",
    "connect",
    "resolve_db_path",
    "utc_now",
]
