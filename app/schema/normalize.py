"""值对象的归一化：把"同一个东西的不同写法"折叠成可比较的字符串。

放在 schema 层而不是 update 层：判同键（`app/update/keys.py`）和派生区的标签去重
（`app/derive`）都要用同一套规则，而 `app/derive` 不该为了"去掉一个空格"反过来
依赖 `app/update`。两处各写一份归一化迟早会漂开，所以只留一份。
本层不认识提案，也不认识数据库。

一条铁律：**只做确定性等价，不做模糊相似。** 大小写、全半角、空白、日期与 url
的写法差异可以折叠；"字节跳动" 与 "ByteDance"、"后端工程师" 与
"Backend Engineer" 不算同一个东西。代码一沾模糊匹配就会悄悄合并错数据，
且事后极难查；这类判断属于 LLM 或用户裁决。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_WS = re.compile(r"\s+")
# 2021-07 / 2021/7 / 2021.07 / 202107 之外的分隔写法
_MONTH_DASH = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_MONTH_CN = re.compile(r"^(\d{4})年(\d{1,2})月?$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")

__all__ = ["normalize", "month", "normalize_url", "value_key"]


def normalize(text: Any) -> str:
    """NFKC 全半角折叠 + 去首尾空白 + 小写 + 内部连续空白压成一个空格。"""
    folded = unicodedata.normalize("NFKC", "" if text is None else str(text))
    return _WS.sub(" ", folded.strip()).lower()


def month(value: Any) -> str:
    """把起止时间归一到 `YYYY-MM`；识别不了就退回 `normalize`，不强行造月份。

    空串保持空串——它表示"未知"，不是"1 月"。
    """
    text = normalize(value)
    if not text:
        return ""
    for pattern in (_MONTH_DASH, _MONTH_CN):
        matched = pattern.match(text)
        if matched:
            return f"{matched.group(1)}-{int(matched.group(2)):02d}"
    return text


def normalize_url(url: Any) -> str:
    """url 规范化：去协议 / www / 尾斜杠 / query / fragment / `.git` 后缀，host 折叠大小写。

    只规范"等价写法"，不改 host、不做大小写不敏感的路径比较——GitHub 的仓库名
    确实不区分大小写，但这条规则留给调用方决定，代码不猜。
    """
    text = normalize(url)
    if not text:
        return ""
    text = _SCHEME.sub("", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = text.rstrip("/")
    return re.sub(r"\.git$", "", text)


def value_key(value: Any) -> str:
    """标量列表元素的判同键：城市、语言、证书、领域标签、能力标签通用。"""
    return normalize(value)
