"""更新画像这条链路的确定性内核：判同键、提案对象、元素级 diff、三档合并。

与 app/storage 同样的规矩：不 import langchain / langgraph、不 import sqlite3、
不开线程、不读环境变量、不 print，四个模块全程只处理内存对象，
所以整层可以脱离数据库和 LLM 直接用 fixture 跑测试。

依赖方向是单向的，破了就没法单独测：

    errors  ←  keys  ←  proposal  ←  diff  ←  merge

值归一化（normalize / month / normalize_url / value_key）不在本层，它下沉到了
`app/schema/normalize.py`：`app/derive` 也要用同一套规则，不能为了去空格反过来
依赖导入链路。本层只转出判同键与确定性 id。

上层（LangGraph 子图 / CLI）只允许调 `build_plan` 与 `build_commit_plan` 这一对，
中间的 target、bucket、tier 都是给渲染层看的，不是给调用方自己拼的。
"""

from .diff import (
    ENTRY_LIST_FIELDS,
    HANDLED_FIELDS,
    ID_FIELD,
    Bucket,
    ChangeTier,
    EntryChange,
    EntryState,
    FieldChange,
    ProposalPlan,
    build_plan,
    is_blank,
)
from .errors import MergeError, ProfileUpdateError, ScopeError, UnkeyableError
from .keys import (
    experience_id,
    experience_key,
    is_deterministic_id,
    project_id,
    project_key,
    skill_key,
)
from .merge import (
    CommitPlan,
    Decision,
    DecisionAction,
    allowed_actions,
    auto_decide,
    build_commit_plan,
    change_target,
    decidable,
    entry_target,
    merge_decisions,
    targets,
)
from .proposal import (
    ENTRY_COLLECTIONS,
    PROFILE_FIELDS,
    RESERVED_KEYS,
    SCOPE_RULES,
    SCALAR_LIST_FIELDS,
    Proposal,
    Question,
    ScopeRule,
    SourceInfo,
    SourceKind,
    check_scope,
)

__all__ = [
    # 异常
    "MergeError",
    "ProfileUpdateError",
    "ScopeError",
    "UnkeyableError",
    # 判同键与确定性 id（值归一化在 app/schema/normalize.py，本层不转发）
    "experience_id",
    "experience_key",
    "is_deterministic_id",
    "project_id",
    "project_key",
    "skill_key",
    # 提案
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
    # 对账
    "ENTRY_LIST_FIELDS",
    "HANDLED_FIELDS",
    "ID_FIELD",
    "Bucket",
    "ChangeTier",
    "EntryChange",
    "EntryState",
    "FieldChange",
    "ProposalPlan",
    "build_plan",
    "is_blank",
    # 合并与提交计划
    "CommitPlan",
    "Decision",
    "DecisionAction",
    "allowed_actions",
    "auto_decide",
    "build_commit_plan",
    "change_target",
    "decidable",
    "entry_target",
    "merge_decisions",
    "targets",
]
