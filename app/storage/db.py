"""SQLite 连接与建表。

库文件默认落在仓库根的 `.data/profile.db`。路径解析优先级：
显式入参 > 环境变量 CAREERS_PROFILE_DB > 本地配置文件 config.local.toml > 代码默认。

想用本地配置接管默认路径，就在仓库根建 config.local.toml（该文件不进 git，也不会自动生成）：

    [storage]
    profile_db = "D:/some/where/profile.db"

把库放在项目目录是为了让数据跟着项目走（直接可见、随手备份、多套数据并行）；
代价是它和 git 仓库同生命周期，所以三道保险缺一不可：
① 外层 `.gitignore` 屏蔽 `.data/` 与 `*.db*`；② `.data/` 内自动生成只含 `*` 的 .gitignore 自屏蔽；
③ 纪律——画像存的是姓名 / 手机号 / 邮箱，`git add -f` 一下就是隐私事故。
生产部署请一律用环境变量把库指到仓库外：打包后 `parents[2]` 可能落在只读目录。
WAL 会额外产生 -wal / -shm 附属文件，所以屏蔽按后缀而不是单个文件名。

WAL + busy_timeout 是并发前提——Agent 的异步任务与前端请求可能同时读写。
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

# 表结构版本，写入 PRAGMA user_version；高于它的文件会被拒绝打开
# v2：experience / project 增加 retired / retired_at，支持软删除（退役可恢复）
SCHEMA_VERSION = 2

# 仓库根：本文件位于 <root>/app/storage/db.py，往上两层。不用 cwd——否则从哪个
# 目录启动就会把库散在哪
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR_NAME = ".data"
DEFAULT_DB_PATH = PROJECT_ROOT / DATA_DIR_NAME / "profile.db"
DB_PATH_ENV = "CAREERS_PROFILE_DB"
# 本地长期配置，本身不进 git；不存在是正常状态，不会自动创建
CONFIG_FILENAME = "config.local.toml"
# 配置文件里本层只认这一个键，多一个都报错：静默失效的配置比报错难查得多
_CONFIG_ALLOWED_KEYS = frozenset({"profile_db"})

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
    updated_at  TEXT    NOT NULL,
    retired     INTEGER NOT NULL DEFAULT 0,
    retired_at  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS project (
    proj_id     TEXT    PRIMARY KEY,
    profile_id  TEXT    NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    data        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    retired     INTEGER NOT NULL DEFAULT 0,
    retired_at  TEXT    NOT NULL DEFAULT ''
);

-- 审计表：每次写入前的旧状态整份留档，供 diff 预览与回滚。
-- 刻意不建外键：画像行被清空后，历史仍需可回滚。
-- 注：只覆盖 profile 文档。条目的历史靠“软删除 + 可恢复”提供，不进审计。
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

# 索引单独一段，必须在补列之后执行：存量 v1 库的 retired 列是 ALTER 加上去的，
# 建表脚本里就引用它会让老库直接打不开
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_experience_retired ON experience(profile_id, retired);
CREATE INDEX IF NOT EXISTS idx_project_retired ON project(profile_id, retired);
"""

# 存量库需要追加的列（v1 → v2）。与 _DDL 保持同步，幂等补齐
_MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "experience": {
        "retired": "INTEGER NOT NULL DEFAULT 0",
        "retired_at": "TEXT NOT NULL DEFAULT ''",
    },
    "project": {
        "retired": "INTEGER NOT NULL DEFAULT 0",
        "retired_at": "TEXT NOT NULL DEFAULT ''",
    },
}


class SchemaVersionError(RuntimeError):
    """库文件由更高版本的程序创建，降级打开会读坏数据。"""


class StorageConfigError(RuntimeError):
    """本地配置文件语法、键名或取值不合法。"""


def utc_now() -> str:
    """统一的 UTC 时间串。updated_at / version 只允许存储层生成，
    否则 last-write-wins 的先后判定会失真。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_path() -> Path:
    """本地配置文件的位置。固定读仓库根，不做目录搜索。"""
    return PROJECT_ROOT / CONFIG_FILENAME


def _read_config_db_path(path: Path) -> Path | None:
    """从本地配置里取库路径。文件不存在返回 `None`——没配就是不用。"""
    if not path.is_file():
        return None
    try:
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise StorageConfigError(f"配置文件 {path} 无法解析：{error}") from error
    section = loaded.get("storage")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise StorageConfigError(f"{path}: [storage] 必须是表，不能是 {type(section).__name__}")
    unknown = sorted(set(section) - _CONFIG_ALLOWED_KEYS)
    if unknown:
        raise StorageConfigError(
            f"{path}: [storage] 不支持的键 {unknown}，本层只认 {sorted(_CONFIG_ALLOWED_KEYS)}"
        )
    raw = section.get("profile_db")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise StorageConfigError(f"{path}: [storage].profile_db 必须是非空字符串")
    return Path(raw).expanduser()


def resolve_db_path(
    override: str | Path | None = None, *, config_file: str | Path | None = None
) -> Path:
    """优先级：显式入参 > 环境变量 CAREERS_PROFILE_DB > 本地配置文件 > 仓库根默认。

    环境变量面向临时场景（跑测试、换一台机器、生产部署），不改任何文件；
    配置文件面向“我这台机器固定这么放”的长期设置。
    `config_file` 仅供测试注入，业务代码不要传。
    """
    if override:
        return Path(override).expanduser()
    from_env = os.getenv(DB_PATH_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    configured = _read_config_db_path(Path(config_file) if config_file else config_path())
    if configured is not None:
        return configured
    return DEFAULT_DB_PATH


def _ensure_private_dir(directory: Path) -> None:
    """建数据目录，并在里面放一个只含 `*` 的 .gitignore 做自屏蔽。

    外层规则被改坏、或以后往这个目录加了别的库文件时，目录自己能兜住。
    写不进去就跳过：目录只读真正会失败的是建库那一步，不该被这个附属动作挡住。
    """
    directory.mkdir(parents=True, exist_ok=True)
    guard = directory / ".gitignore"
    if guard.exists():
        return
    try:
        guard.write_text("*\n", encoding="utf-8")
    except OSError:
        return


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立连接并确保表存在。调用方负责 close()。"""
    path = resolve_db_path(db_path)
    _ensure_private_dir(path.parent)
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
    # DDL 全量幂等（CREATE ... IF NOT EXISTS），先建表再补列，最后建索引
    conn.executescript(_DDL)
    _add_missing_columns(conn)
    conn.executescript(_DDL_INDEXES)
    if current != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给存量库幂等补列。NOT NULL 的列必须带默认值，存量行会按默认值回填。"""
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


__all__ = [
    "SCHEMA_VERSION",
    "PROJECT_ROOT",
    "DATA_DIR_NAME",
    "DEFAULT_DB_PATH",
    "DB_PATH_ENV",
    "CONFIG_FILENAME",
    "SchemaVersionError",
    "StorageConfigError",
    "connect",
    "config_path",
    "resolve_db_path",
    "utc_now",
]
