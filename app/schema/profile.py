"""画像 Profile：唯一的可编辑事实源。

改动本文件前先读三条约定：

1. **每个字段都必须有默认值。** 存储是文档式 JSON 落库，旧数据 + 新增字段
   完全靠默认值兼容，加字段不需要迁移脚本；一旦有必填字段，历史数据就读不出来。
2. **不保留来源元数据。** 更新采用 last-write-wins（谁最后写谁生效）+ patch 语义，
   因此没有 source / locked / confidence / trust_level：画像里每个当前值
   就是用户认定的事实，HR 沟通可自由引用。
3. **skills[].evidence 是唯一的防编造锚点。** 它指向 Experience.exp_id /
   Project.proj_id，由 app/storage 在写入和删除时校验；反向关系不落库，
   查询时现算，避免双向双写不一致。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .experience import Experience
from .project import Project

# 只支持单画像：profile_id 恒为该值，但保留列，方便日后平滑扩展多画像
DEFAULT_PROFILE_ID = "default"


class Degree(StrEnum):
    """学历。"""

    BACHELOR = "bachelor"
    MASTER = "master"
    PHD = "phd"
    OTHER = "other"
    UNKNOWN = "unknown"


class JobType(StrEnum):
    """雇佣类型。"""

    FULLTIME = "fulltime"
    PARTTIME = "parttime"
    INTERNSHIP = "intern"
    UNKNOWN = "unknown"


class SkillLevel(StrEnum):
    """熟练度。UNKNOWN 供"只知技能名、不知程度"的场景使用，不要拿它冒充 expert。"""

    EXPERT = "expert"
    PROFICIENT = "proficient"
    FAMILIAR = "familiar"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    """资历等级，derived 区字段，由年限 + 经历推算，不接受手工写入。"""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    EXPERT = "expert"
    UNKNOWN = "unknown"


class Education(BaseModel):
    """最高学历。"""

    degree: Degree = Degree.UNKNOWN
    school: str = ""
    major: str = ""
    graduation_year: int = 0


class Basics(BaseModel):
    """基本信息。投递与 HR 联系所需的最小集合。"""

    name: str = ""
    phone: str = ""
    email: str = ""
    location: str = ""
    years_of_experience: int = Field(default=0, ge=0)
    education: Education = Field(default_factory=Education)


class Preferences(BaseModel):
    """求职意向。岗位推荐的召回条件，全部字段前端可改。"""

    target_titles: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(default_factory=list)
    salary_expect: str = ""
    job_type: JobType = JobType.UNKNOWN
    remote_ok: bool = False
    industries: list[str] = Field(default_factory=list)
    career_goal: str = ""


class Skill(BaseModel):
    """一项技能。evidence 为空表示暂无经历支撑，JD 匹配时按弱信号处理。"""

    name: str = ""
    level: SkillLevel = SkillLevel.UNKNOWN
    evidence: list[str] = Field(default_factory=list)


class TalkPoints(BaseModel):
    """对外沟通口径。由用户在交互中确认，Agent 引用前不应自行改写。"""

    self_intro: str = ""
    reason_for_leaving: str = ""


class Derived(BaseModel):
    """派生区：机器算、前端只读。允许落库，但随时可被重算覆盖。"""

    seniority: Seniority = Seniority.UNKNOWN
    domain_tags: list[str] = Field(default_factory=list)
    capability_summary: str = ""
    generated_at: str = ""


class ProfileMeta(BaseModel):
    """元数据。updated_at 由存储层写入，是 last-write-wins 的判定依据。

    completeness 驱动交互：低于阈值时 Agent 主动向用户追问，
    它不落库为准、可随时重算。
    """

    updated_at: str = ""
    completeness: float = Field(default=0.0, ge=0, le=1)


class Profile(BaseModel):
    """用户画像。"""

    profile_id: str = DEFAULT_PROFILE_ID
    basics: Basics = Field(default_factory=Basics)
    preferences: Preferences = Field(default_factory=Preferences)
    skills: list[Skill] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    talk_points: TalkPoints = Field(default_factory=TalkPoints)
    derived: Derived = Field(default_factory=Derived)
    meta: ProfileMeta = Field(default_factory=ProfileMeta)


class ProfileBundle(BaseModel):
    """一次读取的完整视图：画像 + 全部经历 + 全部项目。

    程序启动只需 `load_bundle()` 一次，三个 schema 全部就位，不必重新采集。
    """

    profile: Profile = Field(default_factory=Profile)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    version: int = 0
    persisted: bool = False

    @property
    def entry_ids(self) -> set[str]:
        """所有可被 skills.evidence 引用的 id 集合。"""
        return {item.exp_id for item in self.experiences if item.exp_id} | {
            item.proj_id for item in self.projects if item.proj_id
        }

    def skills_referencing(self, entry_id: str) -> list[str]:
        """反向查询：哪些技能的证据依赖这条经历/项目（删除条目前必须先问）。"""
        return [
            skill.name
            for skill in self.profile.skills
            if entry_id in skill.evidence
        ]


__all__ = [
    "DEFAULT_PROFILE_ID",
    "Degree",
    "JobType",
    "SkillLevel",
    "Seniority",
    "Education",
    "Basics",
    "Preferences",
    "Skill",
    "TalkPoints",
    "Derived",
    "ProfileMeta",
    "Profile",
    "ProfileBundle",
]
