"""画像 / 经历 / 项目三个 schema 共用的值对象。

只放跨 schema 反复出现的小结构，不含任何 Agent / 数据库概念，
方便脱离 LLM 与存储层直接单测。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class Period(BaseModel):
    """起止时间。

    缺失一律用空串表示"未知"，不用 None：下游（匹配、渲染、排序）判空成本
    远高于空串语义，空串落库后 JSON 结构也更稳定。
    """

    model_config = ConfigDict(frozen=True)

    start: str = ""
    end: str = ""


class Outcome(BaseModel):
    """一条成果。

    quantified（是否量化）是 schema 里唯一被结构化出来的判断：
    "有没有数字"直接决定 JD 匹配权重与 HR 沟通时的可引用性，
    其余内容都交给文本让 LLM 消化。
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    quantified: bool = False


def new_id(prefix: str) -> str:
    """生成条目 id。前缀让 id 自带类型信息（exp_ / proj_），日志里一眼可辨。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


__all__ = ["Period", "Outcome", "new_id"]
