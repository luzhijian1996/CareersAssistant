"""app/update 的统一异常。

与 app/storage 同风格：上层一次捕获 ProfileUpdateError 基类即可，
不用认识 pydantic、也不用区分到哪一层炸的。

单独成文件的原因：keys.py 是依赖最底层的模块，它要抛 UnkeyableError；
若把基类定义在 keys.py 里，上层就得反向 import 它；定义在 proposal.py 里，
keys.py 又用不上。异常是横切的，不跟着任何一个业务模块走。
"""

from __future__ import annotations


class ProfileUpdateError(ValueError):
    """更新画像这条链路上的可预期失败，统一继承 ValueError。"""


class UnkeyableError(ProfileUpdateError):
    """缺关键字段，算不出判同键——无法判同就不能保证幂等，宁可拒收。"""


class ScopeError(ProfileUpdateError):
    """提案越权：来源无权触碰的字段出现在了 profile_patch 里。"""


class MergeError(ProfileUpdateError):
    """裁决不完整，或提交计划自相矛盾（证据指向未写入的条目等）。"""


__all__ = ["ProfileUpdateError", "UnkeyableError", "ScopeError", "MergeError"]
