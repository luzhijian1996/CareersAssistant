"""存储层：画像 / 经历 / 项目的 SQLite 持久化。

与 app/schema（数据长什么样）分工：本层只管怎么存、怎么合并、怎么回滚，
不含任何 LLM 与 Agent 概念，可直接单测。

给 LLM 用时在 app/tools 下另建薄工具包一层（参数容错 + 异常转 output 文本），
不要往这里 import langchain / langgraph。
"""

from __future__ import annotations

from .db import DEFAULT_DB_PATH, DB_PATH_ENV, SCHEMA_VERSION, SchemaVersionError, resolve_db_path
from .store import (
    AUDIT_KEEP,
    AuditRecord,
    DiffEntry,
    ProfileStore,
    ProfileStoreError,
    ProfileValidationError,
)

__all__ = [
    "AUDIT_KEEP",
    "DB_PATH_ENV",
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "AuditRecord",
    "DiffEntry",
    "ProfileStore",
    "ProfileStoreError",
    "ProfileValidationError",
    "SchemaVersionError",
    "resolve_db_path",
]
