"""提案对象：适配器唯一的产物，也是"尚未落库的改动意图"的容器。

三条约定：

1. **全部 frozen。** Proposal 要进 Graph State，可变对象会让"谁改了它"无从追溯；
   需要改动时用 `model_copy(update=...)` 产生新实例。
2. **本层不落库，所以允许必填字段。** `app/schema` 那条"每个字段都必须有默认值"
   是给文档式 JSON 做前向兼容用的；这里对象存活期只有一次导入，
   必填反而能挡住"忘了标来源"这种会毁掉幂等提示的错误。
3. **作用域是白名单，不是黑名单，且默认永不含 `preferences` / `talk_points`。**
   越权字段属于"构造出来就非法"：`check_scope` 判违规、`build_plan` 拒绝处理。
   刻意不做"抽出来再过滤掉"——那会让产出方以为自己写进去了。
   将来要放开某个字段，动作是往白名单里加一项，而不是加一个 if 开关。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Experience, Profile, Project

# 画像里"元素是裸字符串"的列表字段：按值判同，默认并集。
# 定在本模块而不是 diff：两个来源白名单也要用它，而 diff 已经依赖本模块，
# 反方向 import 会把依赖方向绕成一个环
SCALAR_LIST_FIELDS = ("soft_skills", "languages", "certifications")

# 与 app/storage 的 RESERVED_KEYS 同义。这里刻意不 import 过来：
# app/update 不允许依赖 app/storage（连 sqlite3 的传递依赖都不要），
# 三个字符串的重复换来的是"本层可脱离数据库单测"。
RESERVED_KEYS = frozenset({"profile_id", "updated_at", "version"})

PROFILE_FIELDS = frozenset(Profile.model_fields)

# 条目集合名。它们不在 profile_patch 里，但同样受作用域约束——
# GitHub 提案不该能凭空造出一段工作经历。
ENTRY_COLLECTIONS = ("experiences", "projects")


class SourceKind(StrEnum):
    """提案来源。不留 UNKNOWN：来源不明确就不该构造出 Proposal。"""

    GITHUB = "github"
    FILE = "file"


class ScopeRule(BaseModel):
    """某类来源允许触碰的范围。"""

    model_config = ConfigDict(frozen=True)

    profile_fields: frozenset[str] = frozenset()
    entry_collections: frozenset[str] = frozenset()


# 白名单。`preferences`（意向城市/薪资/职业目标）与 `talk_points`（沟通口径）
# 在任何来源的白名单里都不存在：简历里的意向是一两年前的旧想法，
# 沟通口径是用户在对话里确认过的，文件无权改写。
SCOPE_RULES: dict[SourceKind, ScopeRule] = {
    SourceKind.GITHUB: ScopeRule(
        profile_fields=frozenset({"skills"}),
        entry_collections=frozenset({"projects"}),
    ),
    # 简历里确实写了软技能 / 外语 / 证书，不给放行就等于强迫用户手录
    SourceKind.FILE: ScopeRule(
        profile_fields=frozenset({"basics", "skills", *SCALAR_LIST_FIELDS}),
        entry_collections=frozenset(ENTRY_COLLECTIONS),
    ),
}


class SourceInfo(BaseModel):
    """这次改意从哪来。`ref` 同时是重复导入提示与"识别结果回显"的依据。"""

    model_config = ConfigDict(frozen=True)

    kind: SourceKind
    ref: str = ""
    retrieved_at: str = ""


class Question(BaseModel):
    """抽取阶段决定不了、必须问用户的问题。

    这是 GitHub 路径的命门：API 能证明仓库存在、有哪些 commit，
    但拿不到"你在这个项目里干了什么"。没有这个字段，"宁问不猜"就没有落点。
    """

    model_config = ConfigDict(frozen=True)

    text: str
    target: str = ""  # 绑在哪个 path / 条目 id 上，空表示全局问题
    required: bool = True


class Proposal(BaseModel):
    """一次采集的产物。不写库、不算 diff、不认识 CLI。"""

    model_config = ConfigDict(frozen=True)

    source: SourceInfo
    # 稀疏 patch：只放"确实抽到的"字段，未出现的字段一律不动（与存储层语义一致）
    profile_patch: dict[str, Any] = Field(default_factory=dict)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def collection_size(self, name: str) -> int:
        return len(getattr(self, name))

    def is_empty(self) -> bool:
        """没有任何候选内容——适配器"没读到东西"与"读失败"是两件事，靠这个区分。"""
        return not (
            self.profile_patch or self.experiences or self.projects or self.questions
        )


def check_scope(proposal: Proposal) -> list[str]:
    """返回违规清单（空 = 合法）。

    只判两件事：来源白名单、字段名是否真实存在。
    嵌套层的未知字段留给存储层拒——不在这里复制一份递归校验，
    两处规则迟早会漂开。
    """
    rule = SCOPE_RULES[proposal.source.kind]
    violations: list[str] = []

    for key in proposal.profile_patch:
        if key in RESERVED_KEYS:
            violations.append(f"profile_patch.{key} 由存储层维护，任何来源都不得写入")
        elif key not in PROFILE_FIELDS:
            violations.append(f"Profile 没有字段 {key}（profile_patch.{key}）")
        elif key not in rule.profile_fields:
            violations.append(
                f"{proposal.source.kind} 来源无权触碰 profile_patch.{key}"
                f"（允许：{sorted(rule.profile_fields)}）"
            )

    for name in ENTRY_COLLECTIONS:
        if proposal.collection_size(name) and name not in rule.entry_collections:
            violations.append(
                f"{proposal.source.kind} 来源无权提出 {name}"
                f"（允许：{sorted(rule.entry_collections)}）"
            )

    return violations


__all__ = [
    "ENTRY_COLLECTIONS",
    "PROFILE_FIELDS",
    "RESERVED_KEYS",
    "SCOPE_RULES",
    "SCALAR_LIST_FIELDS",
    "Proposal",
    "Question",
    "ScopeRule",
    "SourceInfo",
    "SourceKind",
    "check_scope",
]
