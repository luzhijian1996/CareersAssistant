"""对 LLM 暴露的 tool 集合。

新增一个 tool：在本目录加一个 xxx.py（内含 @tool），再追加到 TOOLS 即可。
底层实现一律放在 _impl/<tool>/ 下，本目录不出现任何实现细节。
"""

from __future__ import annotations

from .search_jobs import search_jobs

TOOLS = [search_jobs]

__all__ = ["TOOLS", "search_jobs"]
