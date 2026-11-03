"""项目 Project：一次有始有终的产出（开源、课程、比赛、公司内部项目皆适用）。

刻意不做技术栈 / stars 等程序员特化字段：
capabilities 用自由标签承载"Python"或"预算管控"，
url 只在有来源（GitHub）时填，展示与溯源都够用。

约定：所有字段都必须有默认值（文档式 JSON 落库，靠默认值做前向兼容）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import Outcome, Period


class Project(BaseModel):
    """一个项目。"""

    proj_id: str = ""
    name: str = ""
    summary: str = ""
    description: str = ""
    role: str = ""
    period: Period = Field(default_factory=Period)
    my_work: list[str] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    url: str = ""


__all__ = ["Project"]
