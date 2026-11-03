"""猎聘城市码表（白名单）。

编码全部来自已验证样本。未列入的城市不做猜测：宁可直接返回
unsupported_city，也不要用错误的 dqs 去请求，否则会拿到"看起来正常
但和城市无关"的结果，比明确报错更糟。
"""

from __future__ import annotations

# 城市 → 猎聘 dqs 编码。取自猎聘搜索页筛选项（data-key="dq" / data-code），
# 已逐城实测校验：请求某编码后，返回岗位的 dq 字段应与该城市一致。
LIEPIN_CITY_CODES: dict[str, str] = {
    "北京": "010",
    "上海": "020",
    "天津": "030",
    "重庆": "040",
    "广州": "050020",
    "深圳": "050090",
    "南京": "060020",
    "苏州": "060080",
    "杭州": "070020",
    "武汉": "170020",
    "大连": "210040",
    "西安": "270020",
    "成都": "280020",
}

# 城市不限对应的编码（猎聘自己的“全国”筛选项）
NATIONWIDE_CODE = "410"

# 常见写法 → 标准城市名（含拼音），避免"杭州/杭州市"分裂成两次检索
_CITY_ALIASES: dict[str, str] = {
    "北京市": "北京",
    "上海市": "上海",
    "天津市": "天津",
    "重庆市": "重庆",
    "广州市": "广州",
    "深圳市": "深圳",
    "南京市": "南京",
    "苏州市": "苏州",
    "杭州市": "杭州",
    "武汉市": "武汉",
    "大连市": "大连",
    "西安市": "西安",
    "成都市": "成都",
    "beijing": "北京",
    "shanghai": "上海",
    "tianjin": "天津",
    "chongqing": "重庆",
    "guangzhou": "广州",
    "shenzhen": "深圳",
    "nanjing": "南京",
    "suzhou": "苏州",
    "hangzhou": "杭州",
    "wuhan": "武汉",
    "dalian": "大连",
    "xian": "西安",
    "chengdu": "成都",
}

SUPPORTED_CITIES: tuple[str, ...] = tuple(LIEPIN_CITY_CODES)


def resolve_city(name: str | None) -> tuple[str | None, str]:
    """把用户输入的城市解析成 (dqs 编码, 标准城市名)。

    - (", ") 表示未指定城市，按全国检索；
    - (None, 原样名称) 表示该城市不在白名单内，不应发起检索。
    """
    text = " ".join((name or "").split())
    if not text or text in {"全国", "不限", "城市不限", "all"}:
        return NATIONWIDE_CODE, ""
    key = _CITY_ALIASES.get(text.casefold(), text)
    if key in {"全国", "不限"}:
        return NATIONWIDE_CODE, ""
    code = LIEPIN_CITY_CODES.get(key)
    if code is None:
        return None, key
    return code, key
