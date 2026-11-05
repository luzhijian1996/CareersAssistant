"""数据 schema 层：画像 / 经历 / 项目的形状定义，以及值对象的比较语义。

与 app/storage（怎么存）严格分开：本层不 import sqlite3、不认识数据库。
`normalize.py` 只放"同一个东西的不同写法怎么折叠成同一个值"；
判同键与确定性 id 在 app/update/keys.py，本层不认识提案。
"""

from __future__ import annotations

from .common import Outcome, Period, new_id
from .experience import ExpKind, Experience
from .normalize import month, normalize, normalize_url, value_key
from .profile import (
    DEFAULT_PROFILE_ID,
    Basics,
    Degree,
    Derived,
    Education,
    JobType,
    Preferences,
    Profile,
    ProfileBundle,
    ProfileMeta,
    Seniority,
    Skill,
    SkillLevel,
    TalkPoints,
)
from .project import Project

__all__ = [
    "DEFAULT_PROFILE_ID",
    "Basics",
    "Degree",
    "Derived",
    "Education",
    "ExpKind",
    "Experience",
    "JobType",
    "Outcome",
    "Period",
    "Preferences",
    "Profile",
    "ProfileBundle",
    "ProfileMeta",
    "Project",
    "Seniority",
    "Skill",
    "SkillLevel",
    "TalkPoints",
    "month",
    "new_id",
    "normalize",
    "normalize_url",
    "value_key",
]
