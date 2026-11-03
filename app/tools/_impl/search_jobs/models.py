"""岗位搜索的领域模型。

只描述"搜到的东西长什么样、这次检索处于什么状态"，
不掺任何 Agent / 框架相关概念，方便脱离 LLM 直接单测。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceStatus(StrEnum):
    """一次检索的结果状态。领域错误都不抛异常，用状态码表达。"""

    SUCCESS = "success"
    NO_RESULTS = "no_results"
    NETWORK_UNAVAILABLE = "network_unavailable"
    RISK_CONTROL = "risk_control"
    LOGIN_REQUIRED = "login_required"
    CONTRACT_ERROR = "contract_error"
    SOURCE_UNAVAILABLE = "source_unavailable"
    UNSUPPORTED_CITY = "unsupported_city"
    INVALID_ARGUMENT = "invalid_argument"

    @property
    def ok(self) -> bool:
        return self in {SourceStatus.SUCCESS, SourceStatus.NO_RESULTS}


class Job(BaseModel):
    """归一化后的岗位。列表页没有 JD 全文，字段只保留可核实信息。"""

    model_config = ConfigDict(frozen=True)

    external_id: str
    title: str
    company: str = ""
    salary_raw: str = ""
    salary_min_k: int | None = Field(default=None, ge=0)
    salary_max_k: int | None = Field(default=None, ge=0)
    location: str = ""
    experience_raw: str = ""
    experience_min_years: int | None = Field(default=None, ge=0)
    experience_max_years: int | None = Field(default=None, ge=0)
    education_raw: str = ""
    skills: tuple[str, ...] = ()
    summary: str = ""
    apply_url: str = ""
    source: str = "猎聘"
    matched_skills: tuple[str, ...] = ()


class SearchOutcome(BaseModel):
    """一次检索的完整结果：岗位 + 诊断 + 给模型看的人话提示。"""

    model_config = ConfigDict(frozen=True)

    status: SourceStatus
    keyword: str = ""
    city: str = ""
    jobs: tuple[Job, ...] = ()
    discovered: int = 0
    dropped: int = 0
    elapsed_ms: int = 0
    message: str = ""
    available_cities: tuple[str, ...] = ()
    detail_level: str = "list_card"

    @property
    def ok(self) -> bool:
        return self.status.ok
