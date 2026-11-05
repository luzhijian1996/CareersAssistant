"""元素级 diff：把 Proposal 与现状对账成 ProposalPlan。

为什么不复用存储层的 `diff_profile`：它给的是**整列表** old/new，
30 条技能的列表没人读得懂"到底是多了还是少了哪几条"。这里按判同键拆成三桶，
每一项单独可裁决、单独可渲染；`DiffEntry` 契约不动，`FieldChange` 是本层类型。

两条硬规矩，都是"不做就会出事"：

1. **`removed` 桶（仅旧有，提交后会消失）的 `default_action` 一律是 `keep_old`**，
   也就是"默认不勾选"。想删必须用户主动确认。因为画像字段虽有 audit 可回滚，
   条目却只有软删除（一代，`purge` 不可逆），"会删东西"的变更不给默认放行。
2. **`requires_decision=True` 的项不给任何默认值**，且批量裁决不许覆盖它。
   典型是同名技能的 `level` 冲突——自动取高就是编造。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Basics, Education, Experience, ProfileBundle, Project, Skill, value_key
from .errors import ProfileUpdateError, ScopeError, UnkeyableError
from .keys import experience_id, experience_key, project_id, project_key, skill_key
from .proposal import SCALAR_LIST_FIELDS, Proposal, Question, SourceInfo, check_scope

# 条目内部会被"整条覆盖"丢掉的列表字段
ENTRY_LIST_FIELDS = {
    "experience": ("my_work", "outcomes", "domains"),
    "project": ("my_work", "outcomes", "capabilities"),
}

# 条目类型 -> 主键字段名。merge 靠它把库里旧 id 回填到待写条目上，所以公开
ID_FIELD = {"experience": "exp_id", "project": "proj_id"}

# 本层已实现"逐字段对账 + 合并"的画像字段。往来源白名单里加新字段时，
# 必须先在这里补上 handler：顺序反了就会生成一份"提案里有、对账看得见、
# 却永远进不了 patch"的变更，那比直接报错隐蔽得多
HANDLED_FIELDS = ("basics", "skills", *SCALAR_LIST_FIELDS)


class ChangeTier(StrEnum):
    """变更档位。决定 confirm 给哪一种交互，也决定 merge 用哪档合并算法。"""

    SCALAR = "scalar"
    SCALAR_LIST = "scalar_list"
    SKILL = "skill"
    ENTRY = "entry"


class Bucket(StrEnum):
    """三桶。没有 unchanged——没变的不进列表，只计数（见 ProposalPlan.unchanged_field_count）。"""

    ADDED = "added"  # 仅新有
    REMOVED = "removed"  # 仅旧有：提交后会消失，默认不勾选
    CHANGED = "changed"  # 两边都有但值不同


class EntryState(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    UNTOUCHED = "untouched"  # 保留在列表里可查"为什么没更新"，渲染时默认折叠


class FieldChange(BaseModel):
    """画像里一处字段级变更。"""

    model_config = ConfigDict(frozen=True)

    path: str
    tier: ChangeTier
    bucket: Bucket
    old: Any = None
    new: Any = None
    # accept_new / keep_old / ""（空串 = 无默认，必须用户裁决）
    default_action: str = "accept_new"
    requires_decision: bool = False
    why: str = ""
    # 结构化定位。merge 靠这三样重建最终值，不去解析 path 字符串——
    # path 是给人看的，技能名里带 ] 就会把解析拆掉
    field: str = ""
    key: str = ""
    sub: str = ""


class EntryChange(BaseModel):
    """一条条目的变更状态。条目是整行 upsert，所以粒度是"整条"，但会列出 drops 明细。"""

    model_config = ConfigDict(frozen=True)

    state: EntryState
    kind: str  # experience / project
    key: str
    entry_id: str
    label: str
    old: dict[str, Any] | None = None
    new: dict[str, Any] | None = None
    default_action: str = "accept_new"
    requires_decision: bool = False
    drops: list[str] = Field(default_factory=list)  # 提交后会消失的元素，confirm 要高亮
    why: str = ""


class ProposalPlan(BaseModel):
    """对账总产物。可 JSON 序列化——现在喂 CLI，将来同一份直接喂前端。"""

    model_config = ConfigDict(frozen=True)

    source: SourceInfo
    changes: list[FieldChange] = Field(default_factory=list)
    entries: list[EntryChange] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unchanged_field_count: int = 0
    untouched_entry_count: int = 0

    @property
    def conflicts(self) -> list[Any]:
        """必须逐项裁决、不给默认放行的项。"""
        return [item for item in (*self.changes, *self.entries) if item.requires_decision]

    @property
    def removals(self) -> list[Any]:
        """会让既有数据消失的项：画像的 removed 桶 + 条目里的 drops。"""
        leaking = [change for change in self.changes if change.bucket is Bucket.REMOVED]
        leaking += [entry for entry in self.entries if entry.drops]
        return leaking

    @property
    def actionable(self) -> list[Any]:
        """需要呈现的条目：排除 untouched。"""
        return [entry for entry in self.entries if entry.state is not EntryState.UNTOUCHED]

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_plan(bundle: ProfileBundle, proposal: Proposal) -> ProposalPlan:
    """纯函数：只读，不写库、不调存储层。读库由上层负责，把 ProfileBundle 传进来。

    越权提案在这里就被拒（不是被过滤），所以后面的节点可以假设输入一定合法。
    """
    violations = check_scope(proposal)
    if violations:
        raise ScopeError("提案越权，未进入对账：" + "；".join(violations))

    changes: list[FieldChange] = []
    unchanged = 0
    current = bundle.profile

    for field, value in proposal.profile_patch.items():
        if field == "basics" and isinstance(value, Mapping):
            sub, same = _basics_changes(current.basics, value)
        elif field == "skills":
            sub, same = _skills_changes(current.skills, value)
        elif field in SCALAR_LIST_FIELDS:
            sub, same = _scalar_list_changes(field, getattr(current, field), value)
        else:
            # 走到这里 = 白名单放开了一个本层不会合并的字段（talk_points / derived / meta 等）。
            # 当场拒，不留"看得见但写不进去"的变更
            raise ProfileUpdateError(
                f"字段 {field} 已进入来源白名单，但 app/update 还没有它的对账与合并实现"
                f"（已实现：{list(HANDLED_FIELDS)}）"
            )
        changes += sub
        unchanged += same

    entries, entry_notes = _entry_changes(bundle, proposal)

    return ProposalPlan(
        source=proposal.source,
        changes=changes,
        entries=entries,
        questions=list(proposal.questions),
        notes=[*proposal.notes, *entry_notes],
        unchanged_field_count=unchanged,
        untouched_entry_count=sum(1 for item in entries if item.state is EntryState.UNTOUCHED),
    )


# ---------------------------------------------------------------------- 内部


def is_blank(value: Any) -> bool:
    """"这个位置还没有内容"。空串 / 0 / None / 空列表。

    不把 `False` 当空（它是有效选择值），也不把非空字符串的空白字符当空。
    merge 的 union 分支用同一个判空规则，两处不一致会算出不同的"空"。
    """
    if isinstance(value, bool):
        return False
    if value is None or value == "" or value == 0:
        return True
    return isinstance(value, (list, tuple, dict)) and not value


def _leaf_changes(
    path: str, old: Any, new: Any, tier: ChangeTier, *, field: str, sub: str = ""
) -> tuple[list[FieldChange], int]:
    """单个叶子值的比较。返回（变更列表，未变动计数）。"""
    if old == new:
        return [], 1
    if is_blank(old):
        return [
            FieldChange(
                path=path, tier=tier, bucket=Bucket.ADDED, old=old, new=new,
                field=field, sub=sub,
            )
        ], 0
    if is_blank(new):
        # 提案想清空一个已有值：默认保留旧值，用户主动确认才删
        return [
            FieldChange(
                path=path,
                tier=tier,
                bucket=Bucket.REMOVED,
                old=old,
                new=new,
                default_action="keep_old",
                why="提案未给出该字段的新值，默认保留旧值",
                field=field,
                sub=sub,
            )
        ], 0
    return [
        FieldChange(
            path=path, tier=tier, bucket=Bucket.CHANGED, old=old, new=new,
            field=field, sub=sub,
        )
    ], 0


def _basics_changes(
    current: Basics, patch: Mapping[str, Any], prefix: str = "basics"
) -> tuple[list[FieldChange], int]:
    """递归一层（education 是子模型）。更深的嵌套留在这里，不推广到任意深度。"""
    changes: list[FieldChange] = []
    unchanged = 0
    for key, value in patch.items():
        path = f"{prefix}.{key}"
        if key == "education" and isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                if sub_key not in Education.model_fields:
                    # 未知子字段交给存储层拒，这里不复制一份递归校验
                    continue
                sub_changes, sub_same = _leaf_changes(
                    f"{path}.{sub_key}",
                    getattr(current.education, sub_key),
                    sub_value,
                    ChangeTier.SCALAR,
                    field="basics",
                    sub=f"education.{sub_key}",
                )
                changes += sub_changes
                unchanged += sub_same
            continue
        if key not in Basics.model_fields:
            continue
        sub_changes, sub_same = _leaf_changes(
            path,
            getattr(current, key),
            value,
            ChangeTier.SCALAR,
            field="basics",
            sub=key,
        )
        changes += sub_changes
        unchanged += sub_same
    return changes, unchanged


def _scalar_list_changes(
    field: str, current: list[str], proposed: Any
) -> tuple[list[FieldChange], int]:
    """标量列表：按值判同，拆成"仅新有 / 仅旧有"。顺序变化不算变更。"""
    changes: list[FieldChange] = []
    unchanged = 0
    old_keys = {value_key(item): item for item in current}
    new_keys: dict[str, Any] = {}
    for item in proposed or []:
        key = value_key(item)
        if key and key not in new_keys:
            new_keys[key] = item

    for key, item in new_keys.items():
        if key in old_keys:
            unchanged += 1
            continue
        changes.append(
            FieldChange(
                path=f"{field}[{item}]",
                tier=ChangeTier.SCALAR_LIST,
                bucket=Bucket.ADDED,
                old=None,
                new=item,
                why="并集新增",
                field=field,
                key=key,
            )
        )
    for key, item in old_keys.items():
        if key in new_keys:
            continue
        changes.append(
            FieldChange(
                path=f"{field}[{item}]",
                tier=ChangeTier.SCALAR_LIST,
                bucket=Bucket.REMOVED,
                old=item,
                new=None,
                default_action="keep_old",
                why="提案里没有这一项；不勾选就不会被删",
                field=field,
                key=key,
            )
        )
    return changes, unchanged


def _skills_changes(
    current: list[Skill], proposed: Any
) -> tuple[list[FieldChange], int]:
    """技能按 name 判同，拆三档：新名字追加、evidence 并集、level 冲突必须逐条问。"""
    changes: list[FieldChange] = []
    unchanged = 0
    old_by_key = {skill_key(item.name): item for item in current}

    for item in proposed or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", ""))
        key = skill_key(name)

        if not key:
            changes.append(
                FieldChange(
                    path="skills[(未命名技能)]",
                    tier=ChangeTier.SKILL,
                    bucket=Bucket.ADDED,
                    old=None,
                    new=dict(item),
                    default_action="",
                    requires_decision=True,
                    why="技能名为空，无法判同，必须人工确认",
                    field="skills",
                )
            )
            continue

        matched = old_by_key.get(key)
        # 展示名沿用库里已有的写法：大小写差异是判同层面的事，不该让 target 跟着漂
        label = (matched.name if matched is not None and matched.name else name) or "(未命名技能)"
        if matched is None:
            changes.append(
                FieldChange(
                    path=f"skills[{label}]",
                    tier=ChangeTier.SKILL,
                    bucket=Bucket.ADDED,
                    old=None,
                    new=dict(item),
                    why="新技能",
                    field="skills",
                    key=key,
                )
            )
            continue

        touched = 0
        if "level" in item:
            proposed_level = str(item["level"])
            if proposed_level and proposed_level != matched.level.value:
                changes.append(
                    FieldChange(
                        path=f"skills[{label}].level",
                        tier=ChangeTier.SKILL,
                        bucket=Bucket.CHANGED,
                        old=matched.level.value,
                        new=proposed_level,
                        default_action="",
                        requires_decision=True,
                        why="熟练度冲突：自动取高就是编造，只能你定",
                        field="skills",
                        key=key,
                        sub="level",
                    )
                )
                touched += 1

        if "evidence" in item:
            old_refs = list(matched.evidence)
            new_refs = [str(ref) for ref in (item["evidence"] or [])]
            for ref in new_refs:
                if ref not in old_refs:
                    changes.append(
                        FieldChange(
                            path=f"skills[{label}].evidence[{ref}]",
                            tier=ChangeTier.SKILL,
                            bucket=Bucket.ADDED,
                            old=None,
                            new=ref,
                            why="同技能多证据是想要的结果，默认并入",
                            field="skills",
                            key=key,
                            sub="evidence",
                        )
                    )
                    touched += 1
            for ref in old_refs:
                if ref not in new_refs:
                    changes.append(
                        FieldChange(
                            path=f"skills[{label}].evidence[{ref}]",
                            tier=ChangeTier.SKILL,
                            bucket=Bucket.REMOVED,
                            old=ref,
                            new=None,
                            default_action="keep_old",
                            why="提案里没有这条证据；不勾选就保留",
                            field="skills",
                            key=key,
                            sub="evidence",
                        )
                    )
                    touched += 1

        if not touched:
            unchanged += 1

    return changes, unchanged


def _without_id(dump: dict[str, Any], id_field: str) -> dict[str, Any]:
    """去掉主键后再比内容：库里 id 是随机的、提案 id 是确定性算的，
    两者必然不同，不排掉就会把"内容完全一致"误判成一次变更。"""
    return {key: value for key, value in dump.items() if key != id_field}


def _index_by_key(kind: str, items: list[Any], key_fn: Any) -> tuple[dict[str, Any], list[str]]:
    """把库里现有条目按判同键索引。算不出键的脏数据跳过并记进 notes。

    不在这里抛：库里一条缺 title 的手工条目，没理由让整次导入失败。
    跳过的后果是那条无法被命中、新条目会作为新增写入，notes 里说清楚。
    """
    indexed: dict[str, Any] = {}
    notes: list[str] = []
    for item in items:
        try:
            indexed[key_fn(item)] = item
        except UnkeyableError as error:
            notes.append(f"库里有条目无法判同，已跳过：{error}")
    return indexed, notes


def _entry_changes(
    bundle: ProfileBundle, proposal: Proposal
) -> tuple[list[EntryChange], list[str]]:
    """条目侧：判同用 key（不是 id），命中就是覆盖同一条，没命中就是新增。"""
    result: list[EntryChange] = []
    notes: list[str] = []

    for kind, current_items, proposed_items in (
        ("experience", bundle.experiences, proposal.experiences),
        ("project", bundle.projects, proposal.projects),
    ):
        key_fn = experience_key if kind == "experience" else project_key
        id_fn = experience_id if kind == "experience" else project_id
        id_field = ID_FIELD[kind]

        current_by_key, skipped = _index_by_key(kind, current_items, key_fn)
        notes += skipped

        for item in proposed_items:
            key = key_fn(item)
            entry_id = id_fn(item)
            matched = current_by_key.get(key)
            label = _entry_label(kind, item)

            if matched is None:
                result.append(
                    EntryChange(
                        state=EntryState.NEW,
                        kind=kind,
                        key=key,
                        entry_id=entry_id,
                        label=label,
                        new=item.model_dump(mode="json"),
                        why="库里没有同键条目，作为新增写入",
                    )
                )
                continue

            old_dump = matched.model_dump(mode="json")
            new_dump = item.model_dump(mode="json")
            drops = _entry_drops(kind, old_dump, new_dump)
            # 忽略 id 后完全一致才算"没动"
            if _without_id(old_dump, id_field) == _without_id(new_dump, id_field):
                result.append(
                    EntryChange(
                        state=EntryState.UNTOUCHED,
                        kind=kind,
                        key=key,
                        entry_id=entry_id,
                        label=label,
                        old=old_dump,
                        new=new_dump,
                        why="内容与库里一致，不产生写入",
                    )
                )
                continue

            would_lose = bool(drops)
            result.append(
                EntryChange(
                    state=EntryState.UPDATED,
                    kind=kind,
                    key=key,
                    entry_id=entry_id,
                    label=label,
                    old=old_dump,
                    new=new_dump,
                    default_action="" if would_lose else "accept_new",
                    requires_decision=would_lose,
                    drops=drops,
                    why=(
                        "这次覆盖会让下列内容消失；条目没有多版本审计，必须你确认"
                        if would_lose
                        else "纯增补，不丢内容"
                    ),
                )
            )

    return result, notes


def _entry_drops(kind: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """这次整条覆盖会丢掉什么：列表元素减少，或文本被空值覆盖（适配器漏填 ≠ 要清空）。"""
    drops: list[str] = []
    for field in ENTRY_LIST_FIELDS[kind]:
        old_items = {_entry_value_key(item) for item in (old.get(field) or [])}
        new_items = {_entry_value_key(item) for item in (new.get(field) or [])}
        for lost in old_items - new_items:
            drops.append(f"{field}[{lost}]")
    for field, old_value in old.items():
        if field in ENTRY_LIST_FIELDS[kind] or field == ID_FIELD[kind]:
            continue
        if isinstance(old_value, str) and old_value.strip() and not str(new.get(field, "")).strip():
            drops.append(f"{field}(清空)")
    return drops


def _entry_value_key(item: Any) -> str:
    """条目内元素判同：Outcome 是对象，取 text；其余按值。"""
    if isinstance(item, Mapping):
        return value_key(item.get("text", ""))
    return value_key(item)


def _entry_label(kind: str, item: Experience | Project) -> str:
    if kind == "experience":
        return f"{item.company} · {item.title}".strip(" ·")
    return item.name or item.url


__all__ = [
    "Bucket",
    "ChangeTier",
    "EntryChange",
    "EntryState",
    "FieldChange",
    "ProposalPlan",
    "ENTRY_LIST_FIELDS",
    "HANDLED_FIELDS",
    "ID_FIELD",
    "build_plan",
    "is_blank",
]
