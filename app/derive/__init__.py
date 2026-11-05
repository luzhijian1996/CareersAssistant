"""派生区：从用户已经提供的事实里算出机器字段。

只有一层：`recompute`。依赖方向固定在 `app/schema` 之上，
**不 import `app.update`**——前端 tab 手改画像也要重算，那条路不该被
"导入提案"链路绑住；归一化因此已经下沉到 `app/schema/normalize.py`。

上层（子图节点 / CLI / tab 写入路径）只允许调这一对：

    recompute(bundle) -> Recomputed          # 纯算，不写库
    Recomputed.to_patch() -> dict            # 并进调用方那一次 upsert_profile

`Recomputed` 里的 `gaps` / `years_source` 是给调用方看的诊断，不进 patch。
"""

from .recompute import (
    SLOTS,
    Gap,
    GapRegion,
    Recomputed,
    YearsSource,
    completeness_of,
    domain_tags_of,
    effective_years,
    recompute,
    seniority_of,
    timeline_years,
)

__all__ = [
    "SLOTS",
    "Gap",
    "GapRegion",
    "Recomputed",
    "YearsSource",
    "completeness_of",
    "domain_tags_of",
    "effective_years",
    "recompute",
    "seniority_of",
    "timeline_years",
]
