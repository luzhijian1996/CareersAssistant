"""search_jobs：对 LLM 暴露的岗位搜索工具。

这一层刻意做薄：只负责参数容错归一化、调用 run_search、组装
(content, artifact) 双通道返回，不含任何检索/解析/过滤逻辑。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from ._impl.search_jobs import MAX_LIMIT, render, run_search

_DESCRIPTION = """在猎聘上检索岗位，返回岗位列表（标题/公司/薪资/地点/链接/技能标签）。

使用约定：
- keyword 只放岗位方向（如"Agent工程师"），不要把城市、薪资、经验写进 keyword，它们有独立参数。
- city 只接受白名单城市（北京、上海、天津、重庆、广州、深圳、南京、苏州、杭州、武汉、大连、西安、成都），
  留空表示城市不限；若传入不支持的城市，工具会返回可用城市列表而不执行检索。
- 返回的是列表页信息（标题、薪资范围、经验学历、技能标签），不是 JD 全文。
- 失败不会抛异常，而是返回状态：network_unavailable（需要联网）、risk_control（需完成人机验证）、
  contract_error（来源结构变化）、no_results（无匹配）。请据此决定重试、换城市还是告知用户。"""


class SearchJobsArgs(BaseModel):
    """参数契约。校验偏宽松：LLM 传错类型时降级为默认值，而不是报错浪费一轮。"""

    keyword: str = Field(
        description="目标岗位方向，例如“Agent工程师”“大模型应用工程师”。只写岗位，不含城市与薪资。"
    )
    city: str = Field(
        default="",
        description="中文城市名，例如“杭州”。留空表示城市不限。仅支持白名单城市。",
    )
    salary_min_k: int | None = Field(
        default=None,
        description="月薪下限，单位 K（千元）。只剔除下限可核实且低于该值的岗位，薪资面议的会保留。",
    )
    experience_max_years: int | None = Field(
        default=None, description="经验上限（年）。用于排除最低经验要求高于该年限的岗位。"
    )
    exclude_keywords: list[str] = Field(
        default_factory=list, description="需要排除的关键词，例如 [\"外包\",\"派遣\"]。"
    )
    limit: int = Field(default=10, description="返回条数，取值 1-20，默认 10。")
    page: int = Field(default=0, description="结果页码，从 0 开始，默认 0。用于翻页查看更多。")

    @field_validator("keyword", "city", mode="before")
    @classmethod
    def _to_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("salary_min_k", "experience_max_years", mode="before")
    @classmethod
    def _to_optional_int(cls, value: Any) -> int | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @field_validator("exclude_keywords", mode="before")
    @classmethod
    def _to_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part for part in (item.strip() for item in value.split(",")) if part]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp_limit(cls, value: Any) -> int:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 10
        return max(1, min(MAX_LIMIT, number))

    @field_validator("page", mode="before")
    @classmethod
    def _clamp_page(cls, value: Any) -> int:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0
        return max(0, min(200, number))


@tool("search_jobs", args_schema=SearchJobsArgs, response_format="content_and_artifact")
async def search_jobs(
    keyword: str = "",
    city: str = "",
    salary_min_k: int | None = None,
    experience_max_years: int | None = None,
    exclude_keywords: list[str] | None = None,
    limit: int = 10,
    page: int = 0,
) -> tuple[str, dict[str, Any]]:
    """在猎聘上检索岗位，返回岗位列表（content 给模型看，artifact 给程序用）。"""
    # curl_cffi 是同步库，必须挪出事件循环，否则会阻塞整个 Agent
    outcome = await asyncio.to_thread(
        run_search,
        keyword,
        city,
        salary_min_k=salary_min_k,
        experience_max_years=experience_max_years,
        exclude_keywords=tuple(exclude_keywords or ()),
        limit=limit,
        page=page,
    )
    return render(outcome), outcome.model_dump(mode="json")


if __name__ == "__main__":
    # 冒烟测试：不需要额外脚本，直接 python -m app.tools.search_jobs
    import json
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    from langchain_core.messages import ToolMessage

    from app.tools._impl.search_jobs import liepin

    def _split(result: Any) -> tuple[str, dict[str, Any]]:
        """content_and_artifact 模式下 invoke 返回 ToolMessage。"""
        if isinstance(result, ToolMessage):
            return str(result.content), dict(result.artifact or {})
        content, artifact = result
        return str(content), dict(artifact)

    _call_seq = iter(range(1, 1000))

    def show(title: str, *, keyword: str = "", **kwargs: Any) -> None:
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
        # 按 Agent 的真实方式调用：传 ToolCall，才能拿到 content + artifact
        call = {
            "type": "tool_call",
            "id": f"call_{next(_call_seq)}",
            "name": "search_jobs",
            "args": {"keyword": keyword, **kwargs},
        }
        content, artifact = _split(asyncio.run(search_jobs.ainvoke(call)))
        print(content)
        print("-" * 70)
        print(
            "artifact:",
            json.dumps(
                {
                    "status": artifact["status"],
                    "discovered": artifact["discovered"],
                    "returned": len(artifact["jobs"]),
                    "elapsed_ms": artifact["elapsed_ms"],
                    "detail_level": artifact["detail_level"],
                },
                ensure_ascii=False,
            ),
        )

    # 1) 正常检索
    show("1. 正常检索：Agent工程师 / 杭州 / 5 条", keyword="Agent工程师", city="杭州", limit=5)

    # 2) 过滤条件：排除外包 + 薪资下限
    show(
        "2. 过滤条件：Python / 北京 / 20K 起 / 排除外包",
        keyword="Python",
        city="北京市",
        salary_min_k=20,
        exclude_keywords=["外包"],
        limit=5,
    )

    # 3) 城市不限（全国编码 410）
    show("3. 城市不限：Agent工程师 / 不限城市", keyword="Agent工程师", limit=3)

    # 4) 城市不在白名单
    show("4. 城市不在白名单：三亚", keyword="前端工程师", city="三亚")

    # 5) 参数容错：类型全错
    print(f"\n{'=' * 70}\n5. 参数容错（limit/page 传字符串，关键字带城市）\n{'=' * 70}")
    print(SearchJobsArgs(keyword="Agent", limit="99", page="x", salary_min_k="abc"))

    # 6) 离线 / 风控 / 结构变更的判定验证（替换 session 工厂，不发真实请求）

    class _FakeResponse:
        def __init__(self, status_code: int, text: str = "", payload: Any = None) -> None:
            self.status_code = status_code
            self.text = text
            self._payload = payload
            self.cookies = _FakeCookies()

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    class _FakeCookies:
        def get(self, key: str, default: str = "") -> str:
            return "token-123" if key == "XSRF-TOKEN" else default

    class _OfflineSession:
        def get(self, *args: Any, **kwargs: Any):
            raise ConnectionError("getaddrinfo failed")

        def post(self, *args: Any, **kwargs: Any):
            raise ConnectionError("getaddrinfo failed")

        def close(self) -> None:
            pass

    class _CaptchaSession(_OfflineSession):
        def get(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "<html>请完成安全验证</html>")

        def post(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "<html>请完成安全验证</html>")

    class _BrokenContractSession(_CaptchaSession):
        def get(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "")

        def post(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "", {"flag": 1, "data": {"data": {}}})

    # 城市串位：请求杭州（070020），猎聘却回显按合肥（080020）筛选
    _WRONG_CITY_PAYLOAD = {
        "flag": 1,
        "data": {
            "data": {
                "jobSubscribeInfo": {"dqCode": "080020", "dqName": "合肥"},
                "jobCardList": [
                    {
                        "job": {
                            "title": "AI Agent 工程师",
                            "dq": "合肥-蜀山区",
                            "salary": "20-35k",
                            "link": "/job/7788.shtml",
                            "jobId": "7788",
                            "requireWorkYears": "3-5年",
                        },
                        "comp": {"compName": "合肥某科技"},
                    }
                ],
            }
        },
    }

    class _WrongCitySession(_OfflineSession):
        def get(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "")

        def post(self, *args: Any, **kwargs: Any):
            return _FakeResponse(200, "", _WRONG_CITY_PAYLOAD)

    original_factory = liepin._session_factory
    try:
        liepin._session_factory = _OfflineSession
        show("6. 断网：应返回 network_unavailable", keyword="Agent工程师", city="杭州")

        liepin._session_factory = _CaptchaSession
        show("7. 风控：应返回 risk_control", keyword="Agent工程师", city="杭州")

        liepin._session_factory = _BrokenContractSession
        show("8. 结构变更：应返回 contract_error", keyword="Agent工程师", city="杭州")

        liepin._session_factory = _WrongCitySession
        show(
            "9. 城市串位：请求杭州却回显合肥，应返回 contract_error",
            keyword="Agent工程师",
            city="杭州",
        )
    finally:
        liepin._session_factory = original_factory

    # 10) 纯函数断言
    print(f"\n{'=' * 70}\n10. 纯函数检查\n{'=' * 70}")
    assert liepin.parse_salary_k("30-50k·14薪") == (30, 50)
    assert liepin.parse_salary_k("面议") == (None, None)
    assert liepin.parse_salary_k("30-60万") == (None, None), "万为年薪，不做猜测"
    assert liepin.parse_experience("3-5年") == (3, 5)
    assert liepin.parse_experience("5年以上") == (5, None)
    assert liepin.parse_experience("经验不限") == (None, None)
    from app.tools._impl.search_jobs import cities
    from app.tools._impl.search_jobs.models import SourceStatus

    assert cities.resolve_city("杭州市") == ("070020", "杭州")
    assert cities.resolve_city("Hangzhou") == ("070020", "杭州")
    assert cities.resolve_city("") == (cities.NATIONWIDE_CODE, "")
    assert cities.resolve_city("三亚") == (None, "三亚")

    assert liepin._echo_city({"jobSubscribeInfo": {"dqCode": "070020", "dqName": "杭州"}}) == (
        "070020",
        "杭州",
    )
    assert liepin._echo_city({}) == ("", "")
    assert liepin._same_city("杭州市", "杭州") and not liepin._same_city("合肥", "杭州")

    # 不该报错：编码一致 / 回显缺失 / 城市不限（410 回显全国） / 未指定城市
    for code, name, echo_code, echo_name in (
        ("070020", "杭州", "070020", "杭州"),
        ("070020", "杭州", "", ""),
        (cities.NATIONWIDE_CODE, "", "410", "全国"),
        ("", "", "070020", "杭州"),
    ):
        liepin._verify_city_echo(code, name, echo_code, echo_name)

    # 必须报错：编码被替换，以及最阴险的“编码自洽但其实是别的城市”
    for code, name, echo_code, echo_name in (
        ("070020", "杭州", "410", "全国"),
        ("080020", "杭州", "080020", "合肥"),
    ):
        try:
            liepin._verify_city_echo(code, name, echo_code, echo_name)
        except liepin.LiepinError as error:
            assert error.status is SourceStatus.CONTRACT_ERROR, error
        else:
            raise AssertionError(
                f"串位未被拦截：请求 {code}/{name}，回显 {echo_code}/{echo_name}"
            )
    print("薪资/经验解析、城市归一化、城市回显校验：全部通过")

    # 11) 真实反证：假设码表把杭州错写成 080020（实为合肥），必须被拦下
    print(f"\n{'=' * 70}\n11. 编码失效反证（真实请求，验证不会被静默放行）\n{'=' * 70}")
    try:
        leaked = liepin.fetch_jobs("Agent", "080020", city_name="杭州")
        print(f"未被拦截，返回 {len(leaked)} 条：", [item.location for item in leaked[:3]])
    except liepin.LiepinError as error:
        print(f"已拦截：{error.status}")
        print(f"提示：{error.message}")
