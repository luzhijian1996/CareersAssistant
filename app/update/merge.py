"""三档合并：把 ProposalPlan 与用户裁决算成一份可直接落库的 CommitPlan。

四条铁律：

1. **本层不写库、不认识 sqlite、不调 LLM。** 输入只有内存对象
   （ProfileBundle / ProposalPlan / Decision），输出是一份算好的清单；
   怎么执行由上层按「先条目、后画像」的顺序决定（evidence 校验要求条目先存在）。
2. **画像只会被算成稀疏 patch。** 提案没碰的字段绝不进 patch；数组一律给
   **最终全量列表**——存储层对数组是纯覆盖，所以"并集"必须在这里算完。
3. **合并语义在本层，不在存储层。** 存储层保持"数组整体替换"这一条简单规则；
   需要知道用户意图的算法（逐项裁决 / 并集 / 手改值）全部在这里做完，只提交一次。
4. **每个动作都要能追溯到 target。** 未知 target、缺裁决、动作与该档位的规则不匹配，
   一律抛 MergeError 并列出清单。这里不做"猜一个默认值"的兜底——自动裁决
   正是这条链路最容易被橡皮图章化的地方。

一条与 diff 呼应的约定：**`removed` 桶默认 `keep_old`**，即"不勾选就不会删"。
本层尊重它，所以只有显式 `accept_new` 才会让既有内容消失。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..schema import Basics, Experience, ProfileBundle, Project, Skill, SkillLevel, value_key
from .diff import (
    ENTRY_LIST_FIELDS,
    ID_FIELD,
    Bucket,
    ChangeTier,
    EntryChange,
    EntryState,
    FieldChange,
    ProposalPlan,
    is_blank,
)
from .errors import MergeError
from .keys import skill_key
from .proposal import SCALAR_LIST_FIELDS, Question, SourceInfo

# 条目类型 -> 落库模型
_MODELS: dict[str, type[Experience] | type[Project]] = {
    "experience": Experience,
    "project": Project,
}

# manual 只对标量档位开放：给列表字段"手改一个值"没有意义，只会掩盖裁决意图
_MANUAL_TIERS = (ChangeTier.SCALAR,)


class DecisionAction(StrEnum):
    """用户对一项变更的裁决动作。"""

    ACCEPT_NEW = "accept_new"  # 采纳提案值（对 removed 桶意味着"确实要删"）
    KEEP_OLD = "keep_old"  # 保留现状（对 added 桶意味着"不要这条"）
    UNION = "union"  # 仅条目：读旧条目 → 并集 → 整条写回
    MANUAL = "manual"  # 仅标量：用 value 里给的值


class Decision(BaseModel):
    """一项裁决。`target` 必须来自 `targets(plan)`，不接受手写路径字符串外的定位。"""

    model_config = ConfigDict(frozen=True)

    target: str
    action: DecisionAction
    value: Any = None


class CommitPlan(BaseModel):
    """执行清单。上层照这个清单写库即可，不需要再做任何判断。

    落库顺序固定：`experiences` / `projects` 先写（拿到 id），再写 `profile_patch`。
    """

    model_config = ConfigDict(frozen=True)

    source: SourceInfo
    profile_patch: dict[str, Any] = Field(default_factory=dict)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    # 审计线索：三项列表都是 target 字符串，人读得懂、也够定位回 diff
    applied: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def is_noop(self) -> bool:
        """没有任何要写的内容——上层据此跳过写入，不产生空版本。"""
        return not (self.profile_patch or self.experiences or self.projects)

    @property
    def entry_count(self) -> int:
        return len(self.experiences) + len(self.projects)

    @property
    def required_questions(self) -> list[Question]:
        """还没回答且必须回答的问题。commit 节点据此决定要不要停下来追问。"""
        return [item for item in self.questions if item.required]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "applied": len(self.applied),
            "skipped": len(self.skipped),
            "dropped": len(self.dropped),
            "entries": self.entry_count,
            "patch_fields": len(self.profile_patch),
        }

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ------------------------------------------------------------------ 定位与裁决


def entry_target(change: EntryChange) -> str:
    """条目的裁决 target。用 `kind::key`，因为 key 里本身带 `|` 和 `:`。"""
    return f"{change.kind}::{change.key}"


def change_target(change: FieldChange | EntryChange) -> str:
    return entry_target(change) if isinstance(change, EntryChange) else change.path


def decidable(plan: ProposalPlan) -> list[FieldChange | EntryChange]:
    """所有还能被裁决的项：画像变更 + 非 untouched 的条目。"""
    return [*plan.changes, *plan.actionable]


def targets(plan: ProposalPlan) -> list[str]:
    """给 CLI / 测试用的合法 target 清单。"""
    return [change_target(item) for item in decidable(plan)]


def allowed_actions(change: FieldChange | EntryChange) -> frozenset[DecisionAction]:
    """该允许哪些动作。批量裁决与逐项裁决共用这一条规则。"""
    allowed = {DecisionAction.ACCEPT_NEW, DecisionAction.KEEP_OLD}
    if isinstance(change, EntryChange):
        if change.state is EntryState.UPDATED:
            allowed.add(DecisionAction.UNION)
        return frozenset(allowed)
    if change.tier in _MANUAL_TIERS or (change.tier is ChangeTier.SKILL and change.sub == "level"):
        allowed.add(DecisionAction.MANUAL)
    return frozenset(allowed)


def auto_decide(plan: ProposalPlan) -> list[Decision]:
    """按 `default_action` 生成批量裁决。

    刻意跳过 `requires_decision=True` 的项（它们没有默认值），所以这个函数
    永远不会替用户决定"技能熟练度取哪个"或"这次覆盖要不要丢内容"。
    """
    return [
        Decision(target=change_target(change), action=DecisionAction(change.default_action))
        for change in decidable(plan)
        if change.default_action
    ]


def merge_decisions(base: Iterable[Decision], *extra: Decision) -> list[Decision]:
    """按 target 覆盖：后面的裁决顶掉前面的。

    CLI 的常规做法是先用 `auto_decide` 铺一遍默认，再把用户改过的项覆盖进来。
    不经过这里就会撞上"同一项被裁决两次"那条校验。
    """
    by_target = {item.target: item for item in base}
    for item in extra:
        by_target[item.target] = item
    return list(by_target.values())


# ---------------------------------------------------------------------- 合并实现


def _picked(action: DecisionAction, value: Any, change: FieldChange) -> Any:
    return value if action is DecisionAction.MANUAL else change.new


def _brief(text: str, limit: int = 120) -> str:
    """折成一行：pydantic 的报错带换行，直接打出来会把渲染冲乱。"""
    return " ".join(text.split())[:limit]


def _as_level(value: Any) -> str:
    try:
        return SkillLevel(str(value)).value
    except ValueError as error:
        options = ", ".join(item.value for item in SkillLevel)
        raise MergeError(f"熟练度 {value!r} 不合法（可选：{options}）") from error


def _as_skill(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MergeError(f"技能提案必须是对象，收到 {type(value).__name__}")
    try:
        return Skill.model_validate(dict(value)).model_dump(mode="json")
    except ValidationError as error:
        raise MergeError(f"技能提案不合法：{_brief(str(error))}") from error


def _element_key(item: Any) -> str:
    """条目内数组元素的判同键：Outcome 取 text，其余按值。与 diff 同一套规则。"""
    if isinstance(item, Mapping):
        return value_key(item.get("text", ""))
    return value_key(item)


def _union_list(old_items: list[Any], new_items: list[Any]) -> list[Any]:
    """并集：旧顺序保留，新增元素追加在尾部。空串元素不配拥有位置。"""
    merged = list(old_items)
    seen = {_element_key(item) for item in old_items}
    for item in new_items:
        key = _element_key(item)
        if not key or key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


def _union_mapping(old_map: Mapping[str, Any], new_map: Mapping[str, Any]) -> dict[str, Any]:
    """嵌套对象（period）逐键合并：新值为空就保旧值，不整体覆盖。"""
    merged = dict(old_map)
    for key, value in new_map.items():
        if is_blank(value) and not is_blank(merged.get(key)):
            continue
        merged[key] = value
    return merged


def _union_entry(kind: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """整条条目的并集：数组取并集、对象逐键合、标量新值优先但空值不顶掉旧值。

    这是 `union` 动作的全部语义，也是"适配器漏填一个字段"不会被当成"要清空"的原因。
    """
    merged = dict(old)
    id_field = ID_FIELD[kind]
    for field, value in new.items():
        if field == id_field:
            continue
        if field in ENTRY_LIST_FIELDS[kind]:
            merged[field] = _union_list(list(old.get(field) or []), list(value or []))
        elif isinstance(value, Mapping):
            merged[field] = _union_mapping(old.get(field) or {}, value)
        elif is_blank(value):
            merged[field] = old.get(field, value)
        else:
            merged[field] = value
    return merged


def _apply_basics(
    bundle: ProfileBundle,
    changes: list[FieldChange],
    calls: dict[str, tuple[DecisionAction, Any]],
) -> dict[str, Any]:
    """basics 子字段逐个落位。education 是子模型，patch 里也得保持嵌套。"""
    patch: dict[str, Any] = {}
    for change in changes:
        action, value = calls[change_target(change)]
        if action is DecisionAction.KEEP_OLD:
            continue
        final = _picked(action, value, change)
        if change.sub.startswith("education."):
            parent, child = change.sub.split(".", 1)
            patch.setdefault(parent, {})[child] = final
        else:
            patch[change.sub] = final
    if not patch:
        return {}
    # 只做类型检查，不返回 merged：写库的是稀疏 patch，未出现的字段由存储层保留
    try:
        Basics.model_validate({**bundle.profile.basics.model_dump(mode="json"), **patch})
    except ValidationError as error:
        raise MergeError(f"basics 合并结果不合法：{_brief(str(error))}") from error
    return patch


def _apply_scalar_list(
    current: list[str],
    changes: list[FieldChange],
    calls: dict[str, tuple[DecisionAction, Any]],
) -> list[str] | None:
    """标量列表：新增按值去重追加，删除只在用户明确 accept_new 时生效。"""
    final = list(current)
    dirty = False
    for change in changes:
        action, _ = calls[change_target(change)]
        if action is DecisionAction.KEEP_OLD:
            continue
        if change.bucket is Bucket.ADDED:
            key = change.key or value_key(change.new)
            if key and key not in {value_key(item) for item in final}:
                final.append(change.new)
                dirty = True
        elif change.bucket is Bucket.REMOVED:
            kept = [item for item in final if value_key(item) != change.key]
            if len(kept) != len(final):
                final = kept
                dirty = True
    return final if dirty else None


def _filter_refs(refs: list[str], known_ids: set[str]) -> tuple[list[str], list[str]]:
    """把证据拆成“能挂上”与“条目不存在”两部分。存储层会因一条悬空证据拒掉整个写入。"""
    kept, lost = [], []
    for ref in refs:
        (kept if ref in known_ids else lost).append(ref)
    return kept, lost


def _apply_skills(
    bundle: ProfileBundle,
    changes: list[FieldChange],
    calls: dict[str, tuple[DecisionAction, Any]],
    known_ids: set[str],
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """技能档：新名字追加、evidence 并集、level 只按裁决取。返回 (最终列表, dropped)。

    `known_ids` 用来拦悬空证据：证据指向的条目必须"库里已有"或"本轮即将写入"。
    存储层也会拦，但那是一整次写入失败；在这里剔掉并记进 dropped，用户至少能
    拿到其余改动，并看得见哪条证据被丢了。
    """
    skills = [item.model_dump(mode="json") for item in bundle.profile.skills]
    index = {skill_key(item["name"]): item for item in skills}
    dropped: list[str] = []
    dirty = False

    for change in changes:
        action, value = calls[change_target(change)]
        if action is DecisionAction.KEEP_OLD:
            continue

        if change.sub == "level":
            item = index.get(change.key)
            if item is None:
                dropped.append(f"{change.path}：找不到同名技能，熟练度无处可写")
                continue
            level = _as_level(_picked(action, value, change))
            if item["level"] != level:
                item["level"] = level
                dirty = True
            continue

        if change.sub == "evidence":
            item = index.get(change.key)
            if item is None:
                dropped.append(f"{change.path}：找不到同名技能，证据无处可挂")
                continue
            ref = str(change.new if change.bucket is Bucket.ADDED else change.old)
            if change.bucket is Bucket.ADDED:
                if ref not in known_ids:
                    dropped.append(f"{change.path}：条目 {ref} 不存在或已退役，这条证据不写入")
                    continue
                if ref not in item["evidence"]:
                    item["evidence"] = [*item["evidence"], ref]
                    dirty = True
            elif ref in item["evidence"]:
                item["evidence"] = [r for r in item["evidence"] if r != ref]
                dirty = True
            continue

        # 整条技能新增（新名字，或名为空但用户已确认）
        new_item = _as_skill(change.new)
        key = skill_key(new_item["name"])
        if key and key in index:
            # diff 已按 key 判过，走到这里说明同一批提案里有重复名字
            dropped.append(f"{change.path}：与已有技能同名，不重复追加")
            continue
        kept, lost = _filter_refs(list(new_item["evidence"]), known_ids)
        for ref in lost:
            dropped.append(f"{change.path}.evidence[{ref}]：条目 {ref} 不存在或已退役，证据不写入")
        if lost:
            new_item["evidence"] = kept
        skills.append(new_item)
        if key:
            index[key] = new_item
        dirty = True

    return (skills if dirty else None), dropped


def _apply_entries(
    entries: list[EntryChange],
    calls: dict[str, tuple[DecisionAction, Any]],
) -> dict[str, list[Any]]:
    """条目档：整条 upsert。命中库里的条目一律沿用库里的 id。

    沿用旧 id 是必须的：库里可能是随机 id 的手工条目，同内容再导入算出的是
    确定性 id，拿确定性 id 去 upsert 会插入第二行——判同靠 key，落库靠 id。
    """
    written: dict[str, list[Any]] = {"experience": [], "project": []}

    for change in entries:
        action, _ = calls[entry_target(change)]
        if action is DecisionAction.KEEP_OLD:
            continue
        id_field = ID_FIELD[change.kind]
        payload = dict(change.new or {})
        if action is DecisionAction.UNION:
            payload = _union_entry(change.kind, change.old or {}, payload)
        # 新增用确定性 id；命中旧行就用旧行的主键，保证更新的是同一行
        payload[id_field] = str((change.old or {}).get(id_field) or "") or change.entry_id
        try:
            written[change.kind].append(_MODELS[change.kind].model_validate(payload))
        except ValidationError as error:
            raise MergeError(
                f"{change.kind} 条目 [{change.label}] 合并结果不合法：{_brief(str(error))}"
            ) from error

    return written


# ---------------------------------------------------------------------- 主入口


def build_commit_plan(
    bundle: ProfileBundle,
    plan: ProposalPlan,
    decisions: Iterable[Decision] = (),
) -> CommitPlan:
    """纯函数：把对账结果 + 裁决算成执行清单。

    裁决可以只给一部分——有 `default_action` 的项自动补上，
    但 `requires_decision=True` 的项没有默认值，缺一个就整批拒绝（MergeError），
    绝不"顺手用第一个动作"替用户拍板。
    """
    items = decidable(plan)
    by_target: dict[str, FieldChange | EntryChange] = {}
    duplicated: list[str] = []
    for item in items:
        target = change_target(item)
        if target in by_target:
            duplicated.append(target)
        by_target[target] = item
    if duplicated:
        # diff 自己产出了重复 target，属于本层内部错误，不能让它被当成用户裁决
        raise MergeError(f"对账结果里有重复 target：{sorted(set(duplicated))}")

    given: dict[str, Decision] = {}
    for item in decisions:
        previous = given.get(item.target)
        if previous is not None and previous != item:
            # 静默 last-wins 会把上游的一个 bug 藏成"用户选错了"
            raise MergeError(f"同一项被裁决了两次且动作不同：{item.target}")
        given[item.target] = item
    unknown = sorted(set(given) - set(by_target))
    if unknown:
        raise MergeError(f"裁决指向不存在的项：{unknown}")

    # 第一轮：把裁决与默认动作合成 (action, value)；manual 的值先原样拿着
    calls: dict[str, tuple[DecisionAction, Any]] = {}
    undecided: list[str] = []
    for target, item in by_target.items():
        decision = given.get(target)
        if decision is not None:
            calls[target] = (decision.action, decision.value)
        elif item.default_action:
            calls[target] = (DecisionAction(item.default_action), None)
        else:
            undecided.append(target)
    if undecided:
        raise MergeError(
            f"{len(undecided)} 项必须逐项裁决，没有默认值：" + "；".join(sorted(undecided))
        )

    # 第二轮：动作与档位是否搭配，搭不了的一律拒，不就地改成"能跑"的动作
    illegal: list[str] = []
    manual_value: dict[str, Any] = {}
    for target, (action, value) in calls.items():
        change = by_target[target]
        if action not in allowed_actions(change):
            illegal.append(f"{target} 不支持 {action}")
        elif action is DecisionAction.MANUAL and is_blank(value):
            # 手改成一个空值 = 想清空该字段，必须走"提案给空值 + accept_new"这条明路
            illegal.append(f"{target} 选了 manual 但不带值；想清空请走提案空值 + accept_new")
        elif action is DecisionAction.MANUAL:
            manual_value[target] = value
    if illegal:
        raise MergeError("裁决不合法：" + "；".join(sorted(illegal)))

    # 第三轮：只有 manual 项保留手改值，其他项一律置 None，免得下游误读
    calls = {t: (a, manual_value.get(t)) for t, (a, _) in calls.items()}

    # 先算条目：本轮要写入的 id 也算"存在"，才能给新技能挂上证据
    written = _apply_entries(plan.actionable, calls)
    known_ids = set(bundle.entry_ids)
    known_ids.update(item.exp_id for item in written["experience"])
    known_ids.update(item.proj_id for item in written["project"])

    patch: dict[str, Any] = {}
    dropped: list[str] = []
    consumed: set[str] = set()

    basics_changes = [c for c in plan.changes if c.field == "basics"]
    consumed.update(change_target(c) for c in basics_changes)
    if basics_changes:
        basics_patch = _apply_basics(bundle, basics_changes, calls)
        if basics_patch:
            patch["basics"] = basics_patch

    skill_changes = [c for c in plan.changes if c.field == "skills"]
    consumed.update(change_target(c) for c in skill_changes)
    if skill_changes:
        skills, skill_dropped = _apply_skills(bundle, skill_changes, calls, known_ids)
        dropped += skill_dropped
        if skills is not None:
            patch["skills"] = skills

    for field in SCALAR_LIST_FIELDS:
        field_changes = [c for c in plan.changes if c.field == field]
        if not field_changes:
            continue
        consumed.update(change_target(c) for c in field_changes)
        final = _apply_scalar_list(getattr(bundle.profile, field), field_changes, calls)
        if final is not None:
            patch[field] = final

    # 不变量：每一项画像变更都必须被某个合并分支消费。漏了就当场拒，
    # 不能让它出现在 applied 里却根本进不了库
    leftover = sorted(set(by_target) - consumed - {change_target(e) for e in plan.actionable})
    if leftover:
        raise MergeError(f"这些变更本层不会落任何写入，先补合并实现：{leftover}")

    applied = sorted(t for t, (a, _) in calls.items() if a is not DecisionAction.KEEP_OLD)
    skipped = sorted(t for t, (a, _) in calls.items() if a is DecisionAction.KEEP_OLD)

    return CommitPlan(
        source=plan.source,
        profile_patch=patch,
        experiences=written["experience"],
        projects=written["project"],
        questions=list(plan.questions),
        applied=applied,
        skipped=skipped,
        dropped=dropped,
        notes=list(plan.notes),
    )


__all__ = [
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


if __name__ == "__main__":
    # 冒烟测试：直接 python -m app.update.merge
    # 全程手写 fixture + 内存对象：不连数据库、不调 LLM、不读写 .data/
    import json
    import sys

    from ..schema import (
        Degree,
        Education,
        ExpKind,
        Outcome,
        Period,
        Profile,
        month,
        normalize,
        normalize_url,
    )
    from .diff import build_plan
    from .errors import ProfileUpdateError, ScopeError, UnkeyableError
    from .keys import (
        experience_id,
        experience_key,
        is_deterministic_id,
        project_id,
        project_key,
        skill_key,
    )
    from .proposal import (
        SCOPE_RULES,
        Proposal,
        Question,
        ScopeRule,
        SourceInfo,
        SourceKind,
        check_scope,
    )

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    def step(title: str) -> None:
        print(f"\n{'=' * 68}\n{title}\n{'-' * 68}")

    def expect(error_cls, action, hint: str) -> str:
        try:
            action()
        except error_cls as error:
            # 折成一行：pydantic / toml 的原始报错带换行，直接打出来会把分页冲乱
            brief = " ".join(str(error).split())[:110]
            print(f"已拦截[{hint}]：{brief}")
            return brief
        raise AssertionError(f"{hint} 未被拦截")

    # ------------------------------------------------------------------ fixture

    def make_bundle() -> ProfileBundle:
        """一个"已经被手录过一轮"的库里现状。"""
        return ProfileBundle(
            profile=Profile(
                basics=Basics(
                    name="张三",
                    years_of_experience=4,
                    education=Education(degree=Degree.BACHELOR, school="某大学"),
                ),
                skills=[
                    Skill(name="Python", level=SkillLevel.EXPERT, evidence=["exp_old"]),
                    Skill(name="需求访谈", level=SkillLevel.UNKNOWN, evidence=[]),
                ],
                soft_skills=["沟通"],
                languages=["中文"],
            ),
            experiences=[
                Experience(
                    exp_id="exp_old",  # 库里是随机 id（手工录入），不是确定性 id
                    kind=ExpKind.WORK,
                    company="字节",
                    title="后端工程师",
                    period=Period(start="2021-07", end="2024-12"),
                    summary="订单服务",
                    my_work=["负责订单服务"],
                    outcomes=[Outcome(text="QPS 提升 30%", quantified=True)],
                    domains=["电商"],
                )
            ],
            projects=[
                Project(
                    proj_id="proj_old",
                    name="old-repo",
                    url="https://github.com/me/old-repo",
                    my_work=["维护"],
                    capabilities=["Python"],
                )
            ],
            version=7,
            persisted=True,
        )

    NEW_REPO = Project(
        name="new-repo",
        url="https://github.com/me/new-repo/",  # 带尾斜杠：与库里的写法不同但同一个仓库
        summary="CLI 工具",
        role="独立作者",
        my_work=["写 README"],
        capabilities=["Go"],
    )
    # 适配器必须自己算确定性 id：画像里的 evidence 要能指到它（还没落库的新条目）
    NEW_REPO_ID = project_id(NEW_REPO)

    def github_proposal() -> Proposal:
        return Proposal(
            source=SourceInfo(kind=SourceKind.GITHUB, ref="https://github.com/me/new-repo"),
            profile_patch={
                "skills": [
                    {"name": "Go", "level": "proficient", "evidence": ["ghost_1"]},
                    # 名字大小写不同 = 同一个技能；evidence 不含 exp_old = 会落进 removed 桶
                    {"name": "python", "level": "expert", "evidence": [NEW_REPO_ID]},
                ]
            },
            projects=[NEW_REPO],
            questions=[Question(text="这个仓库里你具体负责哪部分？", target="project::new-repo")],
        )

    def file_proposal() -> Proposal:
        return Proposal(
            source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf"),
            profile_patch={
                "basics": {
                    "name": "张三丰",
                    "years_of_experience": 4,  # 同值 → 不该产生变更
                    "phone": "13800000000",  # 库里为空 → ADDED
                    "education": {"major": "计算机"},
                },
                "soft_skills": ["沟通", "跨部门协作"],
            },
            experiences=[
                Experience(
                    kind=ExpKind.WORK,
                    company=" 字节 ",
                    title="后端工程师",
                    period=Period(start="2021/7", end="2024-12"),  # 斜杠写法，仍判同一条
                    summary="订单服务与结算",
                    my_work=["负责订单服务", "带两人"],
                    outcomes=[],  # 适配器没抽到成果 → 整条覆盖会丢掉库里那条量化成果
                    domains=["电商", "支付"],
                ),
                Experience(
                    kind=ExpKind.WORK,
                    company="新公司",
                    title="Agent 工程师",
                    period=Period(start="2025-03"),
                    my_work=["画像 schema"],
                ),
            ],
        )

    def level_proposal(level: str) -> Proposal:
        """只改熟练度的提案：evidence 与库里一致，避免干扰。"""
        return Proposal(
            source=SourceInfo(kind=SourceKind.GITHUB, ref="https://github.com/me/old-repo"),
            profile_patch={"skills": [{"name": "python", "level": level, "evidence": ["exp_old"]}]},
        )

    bundle = make_bundle()

    # ---------------------------------------------------------------------- 用例

    step("1. 归一化：写法差异折叠，语义差异不猜")
    assert normalize(" \uff22\uff59\uff54\uff45\uff24\uff41\uff4e\uff43\uff45 ") == "bytedance"
    assert month("2021年7月") == "2021-07" and month("2021.7") == "2021-07"
    assert month("至今") == "至今" and month("") == ""
    assert normalize_url("HTTPS://WWW.GitHub.com/me/repo.git/") == "github.com/me/repo"
    assert skill_key("Python") == skill_key("  python ")
    assert experience_key(Experience(company="字节", title="后端工程师", period=Period(start="2021/7"))) \
        == experience_key(Experience(company=" 字节", title="后端工程师", period=Period(start="2021-07")))
    assert experience_key(Experience(company="字节", title="ByteDance 后端工程师")) \
        != experience_key(Experience(company="字节", title="后端工程师"))
    print("中英文职位写法不同 = 两条不同经历：模糊归一交给 LLM 与用户，代码不沾")

    step("2. 确定性 id：同内容重跑同值，且与随机 id 一眼可辨")
    assert experience_id(Experience(company="字节", title="后端工程师", period=Period(start="2021/7"))) \
        == experience_id(Experience(company="字节", title="后端工程师", period=Period(start="2021-07")))
    assert is_deterministic_id(NEW_REPO_ID) and NEW_REPO_ID.startswith("proj_")
    assert not is_deterministic_id("exp_a1b2c3d4"), "随机 id 被误判成确定性 id"
    assert not is_deterministic_id("exp_old"), "手工 id 被误判成确定性 id"
    print(f"库里 exp_old / proj_old 都不受幂等保护；新条目用 {NEW_REPO_ID}")

    step("3. 算不出判同键就拒收：无法判同就不能保证幂等")
    expect(UnkeyableError, lambda: experience_key(Experience(company="某司", title="")), "经历缺 title")
    expect(UnkeyableError, lambda: project_key(Project(name="", url="")), "项目无名无 url")

    step("4. 作用域是白名单：越权提案根本不进对账，不是抽出来再过滤掉")
    overreach = Proposal(
        source=SourceInfo(kind=SourceKind.GITHUB, ref="https://github.com/me/new-repo"),
        profile_patch={"basics": {"name": "李四"}},
    )
    expect(ScopeError, lambda: build_plan(bundle, overreach), "github 想改 basics")
    for field in ("preferences", "talk_points"):
        blocked = check_scope(
            Proposal(source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf"), profile_patch={field: {}})
        )
        assert blocked, f"{field} 竟然被放行了"
        print(f"{field}：任何来源的白名单里都没有它（默认永不提案）")
    assert any("存储层维护" in msg for msg in check_scope(
        Proposal(source=SourceInfo(kind=SourceKind.FILE, ref="r"), profile_patch={"version": 9})
    ))
    print("profile_patch 里出现 version 也被判越权")

    step("5. 元素级 diff：三桶拆开，removed 默认不勾选，冲突项不给默认")
    plan = build_plan(bundle, github_proposal())
    for change in plan.changes:
        print(f"  [{change.bucket.value}] {change.path} → 默认={change.default_action or '无'}")
    removed = [c for c in plan.changes if c.bucket is Bucket.REMOVED]
    assert removed and all(c.default_action == "keep_old" for c in removed), "removed 桶没默认不勾选"
    assert plan.conflicts == [], "这份提案里没有必须逐项裁决的项"
    assert [e.state for e in plan.entries] == [EntryState.NEW]
    print(f"unchanged={plan.unchanged_field_count}（Python 的 level 同值，没单独成一条变更）")

    step("6. 默认裁决 → 提交计划：并集、追加、悬空证据被剔掉并留痕")
    commit = build_commit_plan(bundle, plan, auto_decide(plan))
    print(f"patch={sorted(commit.profile_patch)} 条目={commit.entry_count} 统计={commit.stats}")
    skills = commit.profile_patch["skills"]
    assert [item["name"] for item in skills] == ["Python", "需求访谈", "Go"]
    by_name = {item["name"]: item for item in skills}
    assert by_name["Python"]["evidence"] == ["exp_old", NEW_REPO_ID], by_name["Python"]
    assert by_name["Go"]["evidence"] == [], "悬空证据必须被剔掉再写入"
    assert any("ghost_1" in text for text in commit.dropped), commit.dropped
    assert commit.projects[0].proj_id == NEW_REPO_ID and commit.experiences == []
    assert "basics" not in commit.profile_patch, "提案没碰的字段绝不进 patch"
    assert f"skills[Python].evidence[exp_old]" in commit.skipped
    assert commit.required_questions and commit.required_questions[0].text.startswith("这个仓库")

    step("7. removed 桶：只有显式 accept_new 才真删（勾选式确认的全部语义）")
    target = "skills[Python].evidence[exp_old]"
    commit7 = build_commit_plan(
        bundle, plan, merge_decisions(auto_decide(plan), Decision(target=target, action=DecisionAction.ACCEPT_NEW))
    )
    python7 = {item["name"]: item for item in commit7.profile_patch["skills"]}["Python"]
    assert python7["evidence"] == [NEW_REPO_ID]
    assert target in commit7.applied and target not in commit7.skipped

    step("8. 条目命中库里旧行：沿用库里的 id，绝不插出第二行")
    plan8 = build_plan(bundle, file_proposal())
    hit = [e for e in plan8.entries if e.state is EntryState.UPDATED][0]
    assert hit.requires_decision and hit.default_action == "" and hit.drops
    print(f"命中 {hit.label}（键 {hit.key}）：整条覆盖会丢掉 {hit.drops}")
    commit8 = build_commit_plan(
        bundle, plan8,
        merge_decisions(auto_decide(plan8), Decision(target=entry_target(hit), action=DecisionAction.ACCEPT_NEW)),
    )
    written = commit8.experiences[0]
    assert written.exp_id == "exp_old", "UPDATED 条目必须沿用库里主键，否则插出重复行"
    assert written.outcomes == [], "accept_new 就是整条覆盖，旧成果确实没了"
    assert written.summary == "订单服务与结算"
    assert is_deterministic_id(commit8.experiences[1].exp_id), "新增条目走确定性 id"
    assert commit8.profile_patch["basics"] == {
        "name": "张三丰",
        "phone": "13800000000",
        "education": {"major": "计算机"},
    }, commit8.profile_patch["basics"]
    assert commit8.profile_patch["soft_skills"] == ["沟通", "跨部门协作"]
    assert "skills" not in commit8.profile_patch and "languages" not in commit8.profile_patch
    assert all("years_of_experience" not in c.path for c in plan8.changes), "同值字段不该产生变更"

    step("9. union：读旧条目 → 并集 → 整条写回，一条成果都不丢")
    commit9 = build_commit_plan(
        bundle, plan8,
        merge_decisions(auto_decide(plan8), Decision(target=entry_target(hit), action=DecisionAction.UNION)),
    )
    merged = commit9.experiences[0]
    assert merged.exp_id == "exp_old"
    assert [item.text for item in merged.outcomes] == ["QPS 提升 30%"], merged.outcomes
    assert merged.outcomes[0].quantified is True
    assert merged.my_work == ["负责订单服务", "带两人"]
    assert merged.domains == ["电商", "支付"]
    assert merged.period.start == "2021/7", "适配器给什么存什么：判同比归一化后的键，落库不重写原值"

    step("10. 没默认值的项：少一个裁决就整批拒绝，绝不顺手取第一个动作")
    expect(MergeError, lambda: build_commit_plan(bundle, plan8, auto_decide(plan8)), "漏掉条目裁决")
    expect(MergeError, lambda: build_commit_plan(bundle, plan, [
        Decision(target="basics.nope", action=DecisionAction.KEEP_OLD)
    ]), "裁决指向不存在的项")
    expect(MergeError, lambda: build_commit_plan(bundle, plan, [
        Decision(target="experience::字节|后端工程师|2021-07", action=DecisionAction.KEEP_OLD)
    ]), "裁决了对账结果里根本没有的条目")
    same = Decision(target="skills[Python].evidence[exp_old]", action=DecisionAction.KEEP_OLD)
    expect(MergeError, lambda: build_commit_plan(bundle, plan, [same, Decision(
        target=same.target, action=DecisionAction.ACCEPT_NEW)
    ]), "同一项被裁决两次且动作不同")

    step("11. 动作与档位必须搭配：union 只给条目，manual 只给标量")
    expect(MergeError, lambda: build_commit_plan(bundle, plan, merge_decisions(
        auto_decide(plan), Decision(target=entry_target(plan.actionable[0]), action=DecisionAction.UNION)
    )), "union 用在新增条目上")
    list_plan = build_plan(bundle, Proposal(
        source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf"), profile_patch={"soft_skills": ["沟通", "英语"]}
    ))
    expect(MergeError, lambda: build_commit_plan(bundle, list_plan, [
        Decision(target="soft_skills[英语]", action=DecisionAction.MANUAL, value="雅思")
    ]), "列表档不接受手改单个值")

    step("12. 熟练度冲突：不给默认、auto_decide 不覆盖，manual 还得是个合法档位")
    plan12 = build_plan(bundle, level_proposal("familiar"))
    assert [c.path for c in plan12.conflicts] == ["skills[Python].level"]
    assert all(c.target != "skills[Python].level" for c in auto_decide(plan12))
    expect(MergeError, lambda: build_commit_plan(bundle, plan12, auto_decide(plan12)), "熟练度冲突未裁决")
    commit12 = build_commit_plan(bundle, plan12, [
        Decision(target="skills[Python].level", action=DecisionAction.MANUAL, value="proficient")
    ])
    assert commit12.profile_patch["skills"][0]["level"] == "proficient"
    expect(MergeError, lambda: build_commit_plan(bundle, plan12, [
        Decision(target="skills[Python].level", action=DecisionAction.MANUAL, value="godlike")
    ]), "手改成不存在的档位")
    expect(MergeError, lambda: build_commit_plan(bundle, plan12, [
        Decision(target="skills[Python].level", action=DecisionAction.MANUAL)
    ]), "manual 不带值")

    step("13. 幂等：把这一轮的产物喂回去，重跑不再产生任何写入")
    after = bundle.model_copy(update={"projects": [*bundle.projects, *commit.projects]})
    again = build_plan(after, Proposal(
        source=SourceInfo(kind=SourceKind.GITHUB, ref="https://github.com/me/new-repo"), projects=[NEW_REPO]
    ))
    assert again.changes == [] and again.untouched_entry_count == 1, [c.path for c in again.changes]
    noop = build_commit_plan(after, again)
    assert noop.is_noop() and not noop.applied
    print("同内容重跑 = UNTOUCHED + is_noop：上层据此跳过写入，不产生空版本")

    step("14. 空提案 / 库里脏数据：都不该让整次导入失败")
    empty = build_commit_plan(bundle, build_plan(bundle, Proposal(
        source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf")
    )))
    assert empty.is_noop() and empty.stats["patch_fields"] == 0
    dirty = bundle.model_copy(update={"experiences": [*bundle.experiences, Experience(exp_id="exp_junk", company="某司")]})
    plan_dirty = build_plan(dirty, github_proposal())
    assert any("无法判同" in text for text in plan_dirty.notes), plan_dirty.notes
    print("库里缺 title 的手工条目被跳过并写进 notes，其余改动照常对账")

    step("15. 两份产物都能 JSON 化（现在喂 CLI，将来同一份喂前端）")
    plan_json = json.dumps(plan.to_payload(), ensure_ascii=False)
    commit_json = json.dumps(commit.to_payload(), ensure_ascii=False)
    assert '"added"' in plan_json and NEW_REPO_ID in commit_json
    print(f"ProposalPlan {len(plan_json)} 字节 / CommitPlan {len(commit_json)} 字节，无不可序列化字段")

    step("16. 白名单加了字段但忘了补 handler：对账阶段就当场拒")
    saved_rule = SCOPE_RULES[SourceKind.FILE]
    SCOPE_RULES[SourceKind.FILE] = ScopeRule(
        profile_fields=frozenset({*saved_rule.profile_fields, "talk_points"}),
        entry_collections=saved_rule.entry_collections,
    )
    try:
        expect(ProfileUpdateError, lambda: build_plan(bundle, Proposal(
            source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf"),
            profile_patch={"talk_points": {"self_intro": "五年后端"}},
        )), "talk_points 进了白名单却没有合并实现")
    finally:
        SCOPE_RULES[SourceKind.FILE] = saved_rule
    assert check_scope(Proposal(
        source=SourceInfo(kind=SourceKind.FILE, ref="resume.pdf"), profile_patch={"talk_points": {}}
    )), "恢复白名单后 talk_points 竟被放行"
    print("恢复默认后 talk_points 依旧越权：两道守卫各管一段")

    step("17. 不变量：没人消费的变更不准产出一份“看起来会写进去”的计划")
    orphan = ProposalPlan(source=SourceInfo(kind=SourceKind.GITHUB, ref="r"), changes=[
        FieldChange(
            path="derived.seniority", tier=ChangeTier.SCALAR, bucket=Bucket.ADDED,
            new="senior", field="derived",
        )
    ])
    expect(MergeError, lambda: build_commit_plan(bundle, orphan), "这项变更不会落任何写入")

    step("18. 依赖方向：本层不碰 sqlite3 / langchain / langgraph，也不开文件")
    banned = [name for name in ("sqlite3", "langchain", "langgraph", "app.storage") if name in sys.modules]
    assert not banned, f"app/update 不该把这些拉进进程：{banned}"
    print("干净：整条链路可以脱离数据库与大模型跑")

    print(f"\n{'=' * 68}\n全部通过：归一化 / 判同键 / 确定性 id / 作用域 / 三桶 diff / "
          f"三档合并 / 默认不勾选 / 悬空证据剔除 / 幂等重跑 / 两道不变量守卫\n{'=' * 68}")
