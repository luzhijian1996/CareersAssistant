"""search_jobs 的实现包。

对外只导出三个东西，tool 文件不需要了解内部结构：
run_search（执行检索）、render（渲染文本）、模型（返回结构）。
"""

from __future__ import annotations

from .models import Job, SearchOutcome, SourceStatus
from .service import MAX_LIMIT, render, run_search

__all__ = ["Job", "SearchOutcome", "SourceStatus", "MAX_LIMIT", "run_search", "render"]
