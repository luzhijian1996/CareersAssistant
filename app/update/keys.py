"""判同键与确定性 id：整条链路里唯一允许"识别同一条数据"的地方。

归一化不在本文件，它下沉到了 `app/schema/normalize.py`：派生区的标签去重要用
同一套规则，而 `app/derive` 不该为了"去掉一个空格"反过来依赖本层。两处各写一份
归一化迟早会漂开，所以只留一份。本文件只负责"用哪些字段组键"。

三条约定，破任何一条都会出静默数据事故：

1. **判同用 key，落库用 id。** 库里可能已有随机 id 的旧条目（手工建的、或早期版本灌的），
   同内容再导入会算出确定性 id，按 id 找旧行必然找不到 → 插入重复条目。
   所以匹配一律走 key，id 只当主键用。
2. **只做确定性等价，不做模糊相似。** 折叠规则见 `schema.normalize`；
   "字节跳动" 与 "ByteDance" 不算同一条，这类判断属于 LLM 或用户裁决。
3. **确定性 id 用 12 位 hex**，与 `schema.new_id` 的 8 位在长度上就区分开，
   两类 id 永不意外撞行；日志里也能一眼看出这条是被幂等管理的。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..schema import Experience, Project, month, normalize, normalize_url
from .errors import UnkeyableError

# 确定性 id 的哈希长度：12 位（48 bit），且与随机 id 的 8 位长度不同
_ID_HEX = 12

__all__ = [
    "experience_key",
    "project_key",
    "skill_key",
    "experience_id",
    "project_id",
    "is_deterministic_id",
]


def experience_key(item: Experience) -> str:
    """一段经历的判同键：公司 + 职位 + 起月。

    company 与 title 缺一即拒绝判同：只靠"起月 + 职位"会把不同公司的同职位
    合并成一条，那是丢数据，不是省一次录入。
    """
    company = normalize(item.company)
    title = normalize(item.title)
    if not company or not title:
        raise UnkeyableError(
            f"经历缺 company 或 title（company={item.company!r}, title={item.title!r}），"
            "无法判同，请先补全再导入"
        )
    return f"{company}|{title}|{month(item.period.start)}"


def project_key(item: Project) -> str:
    """一个项目的判同键：优先 url，url 为空退化用 name。

    退化路径不保证跨来源幂等（两个同名不同实质的项目会撞键），所以前缀标出
    用的是哪种依据，让 diff 与 confirm 能区分对待。
    """
    url = normalize_url(item.url)
    if url:
        return f"url:{url}"
    name = normalize(item.name)
    if not name:
        raise UnkeyableError("项目既没有 url 也没有 name，无法判同")
    return f"name:{name}"


def skill_key(name: Any) -> str:
    """技能判同键。空名不拒绝——它只会在 diff 里落进"未命名技能"，不该因此炸。"""
    return normalize(name)


def _make_id(prefix: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_ID_HEX]
    return f"{prefix}_{digest}"


def experience_id(item: Experience) -> str:
    """由内容算出的稳定 id：同内容重跑必得同一个值，因此 upsert 天然幂等。"""
    return _make_id("exp", experience_key(item))


def project_id(item: Project) -> str:
    return _make_id("proj", project_key(item))


def is_deterministic_id(value: str) -> bool:
    """区分确定性 id 与随机 id（长度不同）。用于诊断"库里哪些条目不受幂等保护"。"""
    parts = str(value).split("_")
    return len(parts) == 2 and len(parts[1]) == _ID_HEX and all(
        char in "0123456789abcdef" for char in parts[1]
    )
