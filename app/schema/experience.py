"""经历 Experience：一段被雇佣 / 实习 / 求学的过程。

与 Project 形状接近，区别只在 `company + title + kind` 对 `name + description + url`。
按用户要求保持两个独立 schema，语义更直观；若日后需要统一，
共享字段（period / summary / my_work / outcomes）已完全同名同结构。

约定：所有字段都必须有默认值（文档式 JSON 落库，靠默认值做前向兼容）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .common import Outcome, Period


class ExpKind(StrEnum):
    """经历类型。education 也收进来：学历既是画像 basics 的硬条件，也是一段时间线。"""

    WORK = "work"
    INTERN = "intern"
    EDUCATION = "education"
    UNKNOWN = "unknown"


class Experience(BaseModel):
    """一段经历。"""

    exp_id: str = ""
    kind: ExpKind = ExpKind.UNKNOWN
    company: str = ""
    title: str = ""
    period: Period = Field(default_factory=Period)
    summary: str = ""
    my_work: list[str] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


__all__ = ["ExpKind", "Experience"]
