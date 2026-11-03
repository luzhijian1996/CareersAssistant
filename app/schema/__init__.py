"""数据 schema 层：画像 / 经历 / 项目的形状定义。

与 app/storage（怎么存）严格分开：本层不 import sqlite3、不认识数据库。
"""

from __future__ import annotations

from .common import Outcome, Period, new_id
from .experience import ExpKind, Experience
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
    "new_id",
]
