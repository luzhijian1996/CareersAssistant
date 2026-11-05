"""派生区重算：从已有事实算出机器字段，并列出"还缺什么"。

定位：这是**共享函数**，不是子图节点。前端 tab 手改完要调它，更新画像子图的
recompute 节点也包它，两条路必须算出同一个值——所以它不能挂在 `app/update` 上
（手改画像那条路不该依赖"导入提案"这条链路）。

四条铁律：

1. **不写库、不接收 `ProfileStore`、不调 LLM。** 只吃 `ProfileBundle`，只吐稀疏 patch。
   落不落、什么时候落由调用方决定。
2. **算不出来的字段一律不进 patch。** `capability_summary` 这轮不输出（等接 LLM），
   绝不能拿 `""` 占位——存储层对 dict 是递归合并，一个空串会把将来 LLM 填好的值清掉。
   这条和 update 层"`removed` 桶默认不勾选"是同一个原则。
3. **同输入必同输出。** 不读当前时间、不用集合遍历顺序、不引随机。
   `generated_at` 因此**不由本函数填**：它是时间戳，每次重算都变，会击穿存储层
   "空/同值 patch 不产版本"那条验收。要填也只能由调用方在确认其他字段真变了之后填。
4. **只读用户事实区。** 兜底算出来的年限不回写 `basics.years_of_experience`，
   只在 `gaps` 里提示让用户确认；机器区（`derived` / `meta.completeness`）才由本层写。

依赖：只允许 `app/schema`。不许 import `app.update`（那是导入提案那条链路）、
不许 import `app/storage`、不许碰 langchain / langgraph / sqlite3。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from ..schema import ProfileBundle, Seniority, month, value_key

__all__ = [
    "Gap",
    "GapRegion",
    "Recomputed",
    "SLOTS",
    "YearsSource",
    "completeness_of",
    "domain_tags_of",
    "effective_years",
    "recompute",
    "seniority_of",
    "timeline_years",
]

# 资历分档边界（年）：<=2 junior / 3..5 mid / 6..9 senior / >=10 expert
_JUNIOR_MAX = 2
_MID_MAX = 5
_SENIOR_MAX = 9


class GapRegion(StrEnum):
    """缺口属于哪块地。这是追问文案生成器的白名单输入。"""

    PROPOSABLE = "proposable"  # 既能被导入提案，也能主动问
    USER_FACT = "user_fact"  # 只能问，任何来源永不提案（意向、沟通口径）
    MACHINE = "machine"  # 不问也不提案：那是我们自己还没实现，不是用户缺信息


class YearsSource(StrEnum):
    """年限从哪来。`NONE` 表示两个来源都给不出，此时 seniority 只能是 UNKNOWN。"""

    DECLARED = "declared"  # 用户填的 basics.years_of_experience
    TIMELINE = "timeline"  # 兜底：经历时间线合并后的跨度
    NONE = "none"


class Gap(BaseModel):
    """一条缺口。三元组，刻意不留自由文本：下游要能按 slot 路由、按 region 过滤。"""

    model_config = ConfigDict(frozen=True)

    slot: str
    ask_key: str
    region: GapRegion


class Recomputed(BaseModel):
    """重算结果。`to_patch()` 才是落库用的东西，其余字段是给调用方看的诊断。"""

    model_config = ConfigDict(frozen=True)

    seniority: Seniority = Seniority.UNKNOWN
    domain_tags: list[str] = Field(default_factory=list)
    completeness: float = 0.0
    gaps: list[Gap] = Field(default_factory=list)
    years_used: int = 0
    years_source: YearsSource = YearsSource.NONE

    def to_patch(self) -> dict[str, Any]:
        """稀疏 patch：只含机器区。capability_summary 与 generated_at 永远不在这里。"""
        return {
            "derived": {
                "seniority": self.seniority.value,
                # 数组在存储层是整体覆盖，所以这里给的就是最终全量
                "domain_tags": list(self.domain_tags),
            },
            "meta": {"completeness": self.completeness},
        }

    @property
    def askable_gaps(self) -> list[Gap]:
        """去掉机器区：`MACHINE` 的缺口是"我们还没实现"，拿去问用户是耍赖。"""
        return [gap for gap in self.gaps if gap.region is not GapRegion.MACHINE]

    @property
    def user_fact_gaps(self) -> list[Gap]:
        """只能靠对话补齐的缺口（意向、口径）：导入永远不会替用户填这些。"""
        return [gap for gap in self.gaps if gap.region is GapRegion.USER_FACT]


# ------------------------------------------------------------------ 年限与资历


def _month_index(value: str) -> int | None:
    """把起止时间转成"第几个月"的整数。识别不了就返回 None，不猜。"""
    text = month(value)
    if len(text) == 7 and text[:4].isdigit() and text[4] == "-" and text[5:].isdigit():
        return int(text[:4]) * 12 + int(text[5:]) - 1
    return None


def _merged_months(spans: list[tuple[int, int]]) -> int:
    """区间合并后求总月数（含首月）。

    必须先合并再相加：在职读书、在职创业这类并行时段直接相加会把年限灌水，
    而灌水 3 年足以把 mid 抬成 senior，那是给简历编造资历。
    """
    total = 0
    cursor_start = cursor_end = -1
    for start, end in sorted(spans):
        if cursor_start < 0:
            cursor_start, cursor_end = start, end
            continue
        if start > cursor_end + 1:  # 中间断了一个月以上，结算上一段
            total += cursor_end - cursor_start + 1
            cursor_start, cursor_end = start, end
        else:
            cursor_end = max(cursor_end, end)
    if cursor_start >= 0:
        total += cursor_end - cursor_start + 1
    return total


def timeline_years(bundle: ProfileBundle) -> int:
    """从经历时间线兜底推年限。起止有一头不明确就不计入这一段。

    为什么不把"至今"当作现在：读本机的钟会让"同输入同输出"失效，重跑一次
    分数就变，版本也跟着抖。宁可少算，把缺口交给用户确认。
    """
    spans: list[tuple[int, int]] = []
    for item in bundle.experiences:
        start = _month_index(item.period.start)
        end = _month_index(item.period.end)
        if start is None or end is None or end < start:
            continue
        spans.append((start, end))
    return _merged_months(spans) // 12


def effective_years(bundle: ProfileBundle) -> tuple[int, YearsSource]:
    """用户申报优先，为 0 时用时间线兜底。兜底值不回写，只用于派生。"""
    declared = bundle.profile.basics.years_of_experience
    if declared > 0:
        return declared, YearsSource.DECLARED
    fallback = timeline_years(bundle)
    if fallback > 0:
        return fallback, YearsSource.TIMELINE
    return 0, YearsSource.NONE


def seniority_of(years: int) -> Seniority:
    """按阈值分档。0 年 = 不知道，不是"应届 junior"——别替用户假设起点。"""
    if years <= 0:
        return Seniority.UNKNOWN
    if years <= _JUNIOR_MAX:
        return Seniority.JUNIOR
    if years <= _MID_MAX:
        return Seniority.MID
    if years <= _SENIOR_MAX:
        return Seniority.SENIOR
    return Seniority.EXPERT


# ------------------------------------------------------------------ 领域标签


def domain_tags_of(bundle: ProfileBundle) -> list[str]:
    """经历 domains + 项目 capabilities 的并集，去重后给出确定顺序。

    排序键 = (-出现次数, 归一化名)：次数让"主要方向"排在前面，归一化名兜住
    同频标签的稳定次序。展示用的写法取首次出现的原文，所以大小写由库里决定。
    """
    collected: dict[str, list[Any]] = {}
    for tags in (
        [item.domains for item in bundle.experiences]
        + [item.capabilities for item in bundle.projects]
    ):
        for raw in tags:
            key = value_key(raw)
            if not key:
                continue
            entry = collected.setdefault(key, [0, raw])
            entry[0] += 1
    ordered = sorted(collected.items(), key=lambda pair: (-pair[1][0], pair[0]))
    return [str(entry[1]) for _, entry in ordered]


# ------------------------------------------------------------------ 完整度槽位


def _blank(value: Any) -> bool:
    """"这个位置还没有内容"。空串 / None / 空列表；`False` 是有效选择值，不算空。

    刻意不从 `app/update` 引同名函数：derive 不依赖 update（那条依赖是反的）。
    5 行的重复换依赖方向干净；若出现第三个使用者，再下沉到 `app/schema`。
    """
    if isinstance(value, bool):
        return False
    if value is None or value == "" or value == 0:
        return True
    return isinstance(value, (list, tuple, dict)) and not value


def _score_contact(bundle: ProfileBundle) -> float:
    """联系方式四项：缺一项就投不了递，但按填充数给分，好让追问有轻重。"""
    basics = bundle.profile.basics
    filled = sum(1 for value in (basics.name, basics.phone, basics.email, basics.location) if not _blank(value))
    if filled >= 4:
        return 1.0
    return 0.5 if filled >= 2 else 0.0


def _score_years(bundle: ProfileBundle) -> float:
    """用户申报的年限满分；只有时间线兜底值算半分——数据能用，但没被用户确认过。"""
    years, source = effective_years(bundle)
    if source is YearsSource.DECLARED:
        return 1.0
    return 0.5 if years > 0 else 0.0


def _score_education(bundle: ProfileBundle) -> float:
    education = bundle.profile.basics.education
    has_school = not _blank(education.school)
    has_degree = education.degree.value != "unknown"
    if has_school and has_degree:
        return 1.0
    return 0.5 if (has_school or has_degree) else 0.0


def _score_skills_count(bundle: ProfileBundle) -> float:
    count = len(bundle.profile.skills)
    if count >= 3:
        return 1.0
    return 0.5 if count else 0.0


def _score_skills_evidence(bundle: ProfileBundle) -> float:
    """证据覆盖率。**零 evidence 一律 0 分**：一堆没有出处技能名，JD 匹配与
    HR 追问都撑不住，给它半分就是在给一个空画像打高分。
    """
    skills = bundle.profile.skills
    if not skills:
        return 0.0
    covered = sum(1 for skill in skills if skill.evidence)
    if not covered:
        return 0.0
    return 1.0 if covered * 2 >= len(skills) else 0.5


def _score_experiences(bundle: ProfileBundle) -> float:
    return 1.0 if bundle.experiences else 0.0


def _score_outcomes(bundle: ProfileBundle) -> float:
    """有没有成果、以及成果里有没有数字。`Outcome.quantified` 是 schema 里
    唯一被结构化出来的判断，这里就用它，不做文本猜测。
    """
    outcomes = [item for entry in bundle.experiences for item in entry.outcomes]
    if not outcomes:
        return 0.0
    return 1.0 if any(item.quantified for item in outcomes) else 0.5


def _score_projects(bundle: ProfileBundle) -> float:
    return 1.0 if bundle.projects else 0.0


def _score_direction(bundle: ProfileBundle) -> float:
    """求职意向：目标岗位 + 目标城市都有才算满分。这是岗位推荐的召回条件。"""
    preferences = bundle.profile.preferences
    has = [not _blank(preferences.target_titles), not _blank(preferences.target_cities)]
    if all(has):
        return 1.0
    return 0.5 if any(has) else 0.0


def _score_self_intro(bundle: ProfileBundle) -> float:
    return 0.0 if _blank(bundle.profile.talk_points.self_intro) else 1.0


class Slot(NamedTuple):
    """一个完整度槽位：名字、追问键、地权、权重、打分函数。"""

    name: str
    ask_key: str
    region: GapRegion
    weight: float
    scorer: Callable[[ProfileBundle], float]


# 权重合计 1.0。刻意不按"填了多少个字段"计数：那样一来，30 条没有证据的技能名
# 会把分数刷到接近满分，而那恰恰是一份没法用的画像。
SLOTS: tuple[Slot, ...] = (
    Slot("basics.contact", "ask.basics.contact", GapRegion.PROPOSABLE, 0.12, _score_contact),
    Slot("basics.years_of_experience", "ask.basics.years", GapRegion.PROPOSABLE, 0.08, _score_years),
    Slot("basics.education", "ask.basics.education", GapRegion.PROPOSABLE, 0.08, _score_education),
    Slot("skills.count", "ask.skills.count", GapRegion.PROPOSABLE, 0.10, _score_skills_count),
    Slot("skills.evidence", "ask.skills.evidence", GapRegion.PROPOSABLE, 0.20, _score_skills_evidence),
    Slot("experiences.present", "ask.experiences.present", GapRegion.PROPOSABLE, 0.10, _score_experiences),
    Slot("experiences.outcomes", "ask.experiences.outcomes", GapRegion.PROPOSABLE, 0.12, _score_outcomes),
    Slot("projects.present", "ask.projects.present", GapRegion.PROPOSABLE, 0.08, _score_projects),
    Slot("preferences.direction", "ask.preferences.direction", GapRegion.USER_FACT, 0.06, _score_direction),
    Slot("talk_points.self_intro", "ask.talk_points.self_intro", GapRegion.USER_FACT, 0.06, _score_self_intro),
)

# 机器区缺口：不参与打分（机器还没实现的摘要不该让用户觉得自己缺东西）
_CAPABILITY_SUMMARY_GAP = Gap(
    slot="derived.capability_summary",
    ask_key="ask.derived.capability_summary",
    region=GapRegion.MACHINE,
)


def completeness_of(bundle: ProfileBundle) -> tuple[float, list[Gap]]:
    """加权评分 + 缺口清单。返回值 round 到两位：浮点尾差会制造"看似变了"的版本。"""
    gaps: list[Gap] = []
    total = 0.0
    for slot in SLOTS:
        score = slot.scorer(bundle)
        total += score * slot.weight
        if score < 1.0:
            gaps.append(Gap(slot=slot.name, ask_key=slot.ask_key, region=slot.region))
    return round(total, 2), gaps


# ---------------------------------------------------------------------- 主入口


def recompute(bundle: ProfileBundle) -> Recomputed:
    """纯函数：`recompute(bundle) -> Recomputed`。

    调用方把 `to_patch()` 并进自己那次 `upsert_profile` 里，**一次写入**：
    分成两次提交会让一次编辑在审计里碎成两个版本，回滚也会滚出半条改动。
    """
    years, source = effective_years(bundle)
    score, gaps = completeness_of(bundle)
    if _blank(bundle.profile.derived.capability_summary):
        gaps.append(_CAPABILITY_SUMMARY_GAP)
    return Recomputed(
        seniority=seniority_of(years),
        domain_tags=domain_tags_of(bundle),
        completeness=score,
        gaps=gaps,
        years_used=years,
        years_source=source,
    )


if __name__ == "__main__":
    # 冒烟测试：直接 python -m app.derive.recompute
    # 手写 fixture，全程内存对象——不连库、不调 LLM、不读写 .data/
    import json
    import sys

    from ..schema import (
        Basics,
        Degree,
        Derived,
        Education,
        Experience,
        Outcome,
        Period,
        Profile,
        Project,
        Skill,
        SkillLevel,
    )

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    def step(title: str) -> None:
        print(f"\n{'=' * 68}\n{title}\n{'-' * 68}")

    def make_bundle(**overrides: Any) -> ProfileBundle:
        """一份"什么都缺"的基线，各用例只改自己那一项。"""
        profile = Profile(
            basics=Basics(name="张三", years_of_experience=0, education=Education()),
            skills=[],
        )
        bundle = ProfileBundle(profile=profile, experiences=[], projects=[])
        return bundle.model_copy(update=overrides) if overrides else bundle

    def full_bundle() -> ProfileBundle:
        """十项槽位全满的一份画像。"""
        return ProfileBundle(
            profile=Profile(
                basics=Basics(
                    name="张三",
                    phone="13800000000",
                    email="z@ex.com",
                    location="杭州",
                    years_of_experience=7,
                    education=Education(degree=Degree.BACHELOR, school="某大学", major="计算机"),
                ),
                preferences={"target_titles": ["后端工程师"], "target_cities": ["杭州"]},
                skills=[
                    Skill(name="Python", level=SkillLevel.EXPERT, evidence=["exp_1"]),
                    Skill(name="Go", level=SkillLevel.PROFICIENT, evidence=["proj_1"]),
                    Skill(name="需求访谈", level=SkillLevel.FAMILIAR, evidence=["exp_1"]),
                ],
                talk_points={"self_intro": "七年后端，近两年做 Agent"},
                # 摘要刻意留空：槽位全满的画像也该只剩"机器还没生成摘要"这一条缺口
                derived={"seniority": "junior", "capability_summary": ""},
            ),
            experiences=[
                Experience(
                    exp_id="exp_1",
                    company="某科技",
                    title="后端工程师",
                    period=Period(start="2019-03", end="2024-02"),
                    outcomes=[Outcome(text="QPS 提升 30%", quantified=True)],
                    domains=["电商"],
                )
            ],
            projects=[Project(proj_id="proj_1", name="工具库", capabilities=["Python"])],
            version=12,
            persisted=True,
        )

    step("1. 空画像：不报错，seniority=UNKNOWN，分数低，缺口齐全")
    empty = make_bundle()
    result1 = recompute(empty)
    assert result1.seniority is Seniority.UNKNOWN and result1.years_used == 0
    assert result1.completeness == 0.0, result1.completeness
    assert len(result1.gaps) == len(SLOTS) + 1, [g.slot for g in result1.gaps]
    print(f"空画像 completeness={result1.completeness}，缺口 {len(result1.gaps)} 项")

    step("2. 满分画像：completeness=1.0，只剩机器区那一条缺口")
    full = full_bundle()
    result2 = recompute(full)
    assert result2.completeness == 1.0, result2.completeness
    assert [g.slot for g in result2.gaps] == ["derived.capability_summary"], result2.gaps
    assert result2.askable_gaps == [] and len(result2.user_fact_gaps) == 0
    print(f"十项槽位全满 → completeness={result2.completeness}，无可追问缺口")

    step("3. 阈值 2/5/9 的边界：逐档检查")
    for years, expected in (
        (0, Seniority.UNKNOWN),
        (1, Seniority.JUNIOR),
        (2, Seniority.JUNIOR),
        (3, Seniority.MID),
        (5, Seniority.MID),
        (6, Seniority.SENIOR),
        (9, Seniority.SENIOR),
        (10, Seniority.EXPERT),
        (25, Seniority.EXPERT),
    ):
        assert seniority_of(years) is expected, (years, seniority_of(years))
    print("<=2 junior / 3..5 mid / 6..9 senior / >=10 expert；0 年 = 不知道，不冒充应届")

    step("4. 兜底分支：年限没填，用经历跨度推，且不回写 basics")
    fallback = make_bundle(
        experiences=[
            Experience(
                exp_id="exp_1",
                company="某科技",
                title="后端工程师",
                period=Period(start="2019-03", end="2024-02"),
            )
        ]
    )
    result4 = recompute(fallback)
    assert result4.years_source is YearsSource.TIMELINE and result4.years_used == 5, result4
    assert result4.seniority is Seniority.MID
    assert "basics.years_of_experience" in [g.slot for g in result4.gaps], result4.gaps
    assert "basics" not in result4.to_patch(), "机器不许写用户事实区"
    assert result4.to_patch()["meta"]["completeness"] < 1.0
    print(f"兜底算出 {result4.years_used} 年 → {result4.seniority.value}；只进 gaps 让用户确认，不回写")

    step("5. 并行时段不灌水：区间合并后再算；起止缺一头不计入")
    overlap = make_bundle(
        experiences=[
            Experience(company="A", title="后端", period=Period(start="2020-01", end="2023-12")),
            Experience(company="B", title="在读", period=Period(start="2022-01", end="2022-12")),
        ]
    )
    assert timeline_years(overlap) == 4, timeline_years(overlap)
    ongoing = make_bundle(
        experiences=[Experience(company="A", title="后端", period=Period(start="2020-01"))]
    )
    assert timeline_years(ongoing) == 0, "进行中的经历没有当前时间可读，宁可少算"
    gap_between = make_bundle(
        experiences=[
            Experience(company="A", title="后端", period=Period(start="2020-01", end="2020-12")),
            Experience(company="B", title="创业", period=Period(start="2022-01", end="2022-12")),
        ]
    )
    assert timeline_years(gap_between) == 2, timeline_years(gap_between)
    print("重叠 48+12 个月算 4 年（不是 5 年）；断开的两段各 1 年，合计 2 年")

    step("6. domain_tags：去重、频次降序、同频按归一化名，顺序与输入写法无关")
    tags_bundle = make_bundle(
        experiences=[
            Experience(company="A", title="后端", domains=["电商", " 电商 ", "支付"]),
            Experience(company="B", title="架构", domains=["toB", "电商"]),
        ],
        projects=[Project(name="库", capabilities=["Python", "支付"])],
    )
    assert domain_tags_of(tags_bundle) == ["电商", "支付", "Python", "toB"], domain_tags_of(tags_bundle)
    # 只打乱条目顺序、内容一个不动：结果必须逐字相同，否则落库会制造无意义版本
    shuffled = tags_bundle.model_copy(
        update={
            "experiences": list(reversed(tags_bundle.experiences)),
            "projects": list(reversed(tags_bundle.projects)),
        }
    )
    assert domain_tags_of(shuffled) == ["电商", "支付", "Python", "toB"], domain_tags_of(shuffled)
    print("全半角与空白折叠后同一个标签只留一个；顺序 = (-次数, 归一化名)，与条目顺序无关")

    step("7. 零 evidence 必须低分：30 条空技能 < 3 条带证据的技能")
    noisy = make_bundle(
        profile=Profile(
            basics=Basics(name="张三", phone="1", email="2", location="3", years_of_experience=7),
            skills=[Skill(name=f"技术{i}") for i in range(30)],
        )
    )
    scored = make_bundle(
        profile=Profile(
            basics=Basics(name="张三", phone="1", email="2", location="3", years_of_experience=7),
            skills=[
                Skill(name="Python", evidence=["exp_1"]),
                Skill(name="Go", evidence=["exp_1"]),
                Skill(name="MySQL", evidence=["exp_1"]),
            ],
        )
    )
    noisy_score = recompute(noisy).completeness
    scored_score = recompute(scored).completeness
    assert scored_score - noisy_score >= 0.15, (noisy_score, scored_score)
    print(f"30 条零证据 = {noisy_score}；3 条带证据 = {scored_score}（权重 0.20 全给了半分/零分档）")

    step("8. 幂等：同一份画像重跑，patch 一模一样（不给存储层制造空版本）")
    first = recompute(full_bundle()).to_patch()
    second = recompute(full_bundle()).to_patch()
    assert first == second, (first, second)
    assert recompute(empty).to_patch() == recompute(empty).to_patch()
    print(f"重跑两次 patch 相同：{json.dumps(first, ensure_ascii=False)[:72]}…")

    step("9. patch 只含机器区：generated_at 与 capability_summary 都不在其中")
    patch = recompute(full_bundle()).to_patch()
    assert sorted(patch) == ["derived", "meta"], sorted(patch)
    assert sorted(patch["derived"]) == ["domain_tags", "seniority"], sorted(patch["derived"])
    assert "capability_summary" not in patch["derived"] and "generated_at" not in patch["derived"]
    assert "basics" not in patch and "preferences" not in patch and "talk_points" not in patch
    assert 0.0 <= patch["meta"]["completeness"] <= 1.0
    print("调用方把这份 patch 并进自己那次 upsert_profile：一次编辑 = 一个版本")

    step("10. region 三档：MACHINE 的缺口被 askable_gaps 过滤掉，且不参与打分")
    # model_copy 不重新校验，update 里必须放构造好的子模型，塞 dict 会让属性读炸
    with_summary = full_bundle().model_copy(
        update={
            "profile": full_bundle().profile.model_copy(
                update={"derived": Derived(seniority="mid", capability_summary="已有摘要")}
            )
        }
    )
    without_summary = with_summary.model_copy(
        update={
            "profile": with_summary.profile.model_copy(
                update={"derived": Derived(seniority="mid", capability_summary="")}
            )
        }
    )
    gaps_without = recompute(without_summary).gaps
    assert any(g.region is GapRegion.MACHINE for g in gaps_without)
    assert not any(g.region is GapRegion.MACHINE for g in recompute(without_summary).askable_gaps)
    assert recompute(with_summary).completeness == recompute(without_summary).completeness == 1.0
    print("摘要没生成是机器自己的事：报缺口但不问用户、也不扣完整度")

    step("11. 用户事实区：意向与口径缺了要问，但任何来源都不会提案")
    askable = [g.slot for g in recompute(empty).askable_gaps]
    assert "preferences.direction" in askable and "talk_points.self_intro" in askable, askable
    regions = {g.slot: g.region for g in recompute(empty).gaps}
    assert regions["talk_points.self_intro"] is GapRegion.USER_FACT
    assert regions["skills.evidence"] is GapRegion.PROPOSABLE
    print(f"可追问缺口 {len(askable)} 项，其中 USER_FACT {len(recompute(empty).user_fact_gaps)} 项只能靠对话补")

    step("12. 结果可 JSON 化（喂 CLI / 前端，不留不可序列化字段）")
    payload = json.dumps(recompute(full_bundle()).model_dump(mode="json"), ensure_ascii=False)
    assert '"machine"' in payload and '"completeness"' in payload
    print(f"Recomputed {len(payload)} 字节，无枚举对象残留")

    step("13. 依赖方向：derive 不碰 update / storage / sqlite3 / langchain")
    banned = [
        name
        for name in ("sqlite3", "langchain", "langgraph", "app.storage", "app.update")
        if name in sys.modules
    ]
    assert not banned, f"app/derive 不该把这些拉进进程：{banned}"
    print("干净：归一化已从 update 下沉到 schema，手改 tab 那条路不依赖导入链路")

    print(f"\n{'=' * 68}\n全部通过：阈值分档 / 年限兜底不回写 / 区间合并不灌水 / 标签确定顺序 / "
          f"零证据低分 / 幂等重跑 / patch 只含机器区 / 缺口三元组\n{'=' * 68}")
