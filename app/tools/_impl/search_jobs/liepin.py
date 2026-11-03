"""猎聘 HTTP 连接器。

两段式握手：GET 搜索页拿 XSRF-TOKEN → POST 官方搜索接口。
无需登录、无需逆向签名，但必须用 curl_cffi 模拟 Chrome 的 TLS 指纹，
普通 requests / urllib 会被软拦。

本文件是本 tool 里唯一做网络 IO 的地方。所有失败都转成
LiepinError(status, message)，由 service 决定提示话术 —— 不把底层
异常或上游响应正文抛给上层，也不写进日志。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from urllib.parse import quote

from .models import Job, SourceStatus

SEARCH_PAGE = "https://www.liepin.com/zhaopin/"
API_URL = "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job"
# 注意：这里刻意不定义 User-Agent。curl_cffi 的 impersonate="chrome" 会带上一整套
# 版本自洽的指纹头（UA、sec-ch-ua、sec-ch-ua-platform 等）。如果只覆盖 UA 而不动
# sec-ch-ua，两者版本对不上，反而会被判定为伪装：实测页面降级成 1KB 空壳、接口
# 直接返回 {"flag":0}。所以任何请求都不要手动塞 User-Agent。
PAGE_SIZE = 40
DEFAULT_TIMEOUT = 12.0
_REQUEST_TIMEOUT = 10.0

# 同一进程内的最小请求间隔（预防性）。单次检索只有 GET+POST 两个请求，
# 但 Agent 会连续调工具，温柔一点比被风控后再补救便宜。
_MIN_INTERVAL = 1.5
_throttle_lock = threading.Lock()
_last_request_at = 0.0

# 命中这些词说明拿到的是验证/WAF 页，而不是业务数据
_CAPTCHA_MARKERS = (
    "captcha",
    "验证码",
    "安全验证",
    "访问过于频繁",
    "滑动验证",
    "请完成验证",
)

_SALARY_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[kK]")
_EXP_RANGE = re.compile(r"(\d+)\s*[-~至]\s*(\d+)\s*年")
_EXP_ABOVE = re.compile(r"(\d+)\s*年以上")


class LiepinError(RuntimeError):
    """带领域状态的失败，调用方据 status 决定"重试 / 换城市 / 去验证"。"""

    def __init__(self, status: SourceStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status}:{message}")


def _session_factory():
    """惰性导入 curl_cffi：缺依赖只影响本连接器，不影响 app.tools 导入。"""
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(impersonate="chrome")


# ── 解析辅助（纯函数，可单独测） ─────────────────────────────────────────────


def parse_salary_k(raw: str) -> tuple[int | None, int | None]:
    """把 "30-50k·14薪" 解析成 (30, 50)。

    只解析以 k/K 标注的月薪；"万"、"面议"、单位不明一律返回 (None, None)
    —— 不做保守估算，避免把年薪当成月薪。
    """
    match = _SALARY_RANGE.search(raw or "")
    if not match:
        return None, None
    low, high = int(float(match.group(1))), int(float(match.group(2)))
    if low > high:
        low, high = high, low
    return low, high


def parse_experience(raw: str) -> tuple[int | None, int | None]:
    """把 "3-5年" / "5年以上" / "经验不限" 解析成 (下限, 上限)。"""
    text = raw or ""
    match = _EXP_RANGE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _EXP_ABOVE.search(text)
    if match:
        return int(match.group(1)), None
    return None, None


def _throttle() -> None:
    """限制本进程向猎聘发起请求的频率（阻塞式，调用方应已在子线程中）。"""
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _budget(deadline: float) -> float:
    """单个请求的超时 = min(单次上限, 本轮剩余预算)。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise LiepinError(SourceStatus.NETWORK_UNAVAILABLE, "本轮检索时间预算已用完，请稍后重试")
    return min(_REQUEST_TIMEOUT, left)


def _check_http(response) -> None:
    status = getattr(response, "status_code", 0) or 0
    if status == 401:
        raise LiepinError(SourceStatus.LOGIN_REQUIRED, "猎聘要求登录后才能检索")
    if status in (403, 429):
        raise LiepinError(SourceStatus.RISK_CONTROL, "猎聘限制了本次访问（403/429），请稍后再试")
    if status >= 500:
        raise LiepinError(SourceStatus.SOURCE_UNAVAILABLE, "猎聘服务暂时不可用，请稍后重试")
    if status >= 400:
        raise LiepinError(SourceStatus.SOURCE_UNAVAILABLE, "猎聘拒绝了本次请求")


def _cookie_token(session, response) -> str:
    for source in (getattr(session, "cookies", None), getattr(response, "cookies", None)):
        try:
            token = source.get("XSRF-TOKEN", "") if source is not None else ""
        except Exception:
            token = ""
        if token:
            return str(token)
    return ""


def _handshake(session, search_url: str, deadline: float) -> str:
    """GET 搜索页：既拿校验令牌，也天然充当联网探测。"""
    _throttle()
    try:
        # 不带任何自定义头，完全交给 impersonate 的指纹
        landing = session.get(search_url, timeout=_budget(deadline))
    except LiepinError:
        raise
    except Exception as error:
        # DNS / 连接被拒 / 超时 / 代理失败 / TLS 错误都落在这里
        raise LiepinError(
            SourceStatus.NETWORK_UNAVAILABLE,
            "无法连接猎聘，本工具需要联网，请检查网络或代理后重试",
        ) from error
    _check_http(landing)
    token = _cookie_token(session, landing)
    if not token:
        raise LiepinError(
            SourceStatus.RISK_CONTROL, "猎聘会话初始化失败（未取得校验令牌），请稍后重试"
        )
    return token


def _search_headers(search_url: str, token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "X-Client-Type": "web",
        "X-Requested-With": "XMLHttpRequest",
        "X-Fscp-Version": "1.1",
        "X-Fscp-Fe-Version": "",
        "X-Fscp-Std-Info": '{"client_id":"40108"}',
        "X-Fscp-Trace-Id": str(uuid.uuid4()),
        "X-Fscp-Bi-Stat": f'{{"location":"{search_url}"}}',
        "X-XSRF-TOKEN": token,
        "Origin": "https://www.liepin.com",
        "Referer": search_url,
    }


def _search_body(keyword: str, city_code: str, page: int, page_size: int) -> dict:
    return {
        "data": {
            "mainSearchPcConditionForm": {
                "city": city_code,
                "dq": city_code,
                "pubTime": "",
                "currentPage": page,
                "pageSize": page_size,
                "key": keyword,
                "suggestTag": "",
                "workYearCode": "0",
                "compId": "",
                "compName": "",
                "compTag": "",
                "industry": "",
                "salaryCode": "",
                "jobKind": "",
                "compScale": "",
                "compKind": "",
                "compStage": "",
                "eduLevel": "",
                "salaryLow": "",
                "salaryHigh": "",
            },
            "passThroughForm": {
                "scene": "input",
                "skId": uuid.uuid4().hex,
                "fkId": uuid.uuid4().hex,
                "ckId": uuid.uuid4().hex,
            },
        }
    }


def _echo_city(inner: dict) -> tuple[str, str]:
    """读出猎聘回显的筛选城市 (dqCode, dqName)；字段缺失时返回空串。"""
    subscribe = inner.get("jobSubscribeInfo")
    if not isinstance(subscribe, dict):
        return "", ""
    code = str(subscribe.get("dqCode") or "").strip()
    name = str(subscribe.get("dqName") or "").strip()
    return code, name


def _same_city(left: str, right: str) -> bool:
    """两个城市名是否指向同一城市（容忍“杭州 / 杭州市”这类后缀差异）。"""
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def _verify_city_echo(
    city_code: str, city_name: str, echo_code: str, echo_name: str
) -> None:
    """用响应回显的 (dqCode, dqName) 反查上游是否按我们要的城市筛选。

    两项检查各自能抓一种失败，缺一不可：

    1) echo_code 与请求编码不符 —— 编码被服务端丢弃或替换（城市筛选未生效）。
    2) echo_name 与期望城市名不符 —— 码表自身写错了。**这一项才是关键**：
       实测请求 080020 时，服务端会忠实按 080020 筛选并回显 dqCode=080020，
       只比编码永远自洽通过；必须拿城市名比对，才能发现“这个码其实是合肥”。

    回显缺失、或未指定城市名时一律放过，交给 service 的地点过滤兜底。
    实测全国请求 dq=410 会回显 (410, 全国)，编码自洽，不需要额外豁免。
    """
    if echo_code and city_code and echo_code != city_code:
        raise LiepinError(
            SourceStatus.CONTRACT_ERROR,
            f"猎聘按「{echo_name or echo_code}」返回结果，与请求的城市编码 {city_code} 不符，"
            "城市筛选可能未生效，请稍后重试或改用城市不限",
        )
    if city_name and echo_name and not _same_city(echo_name, city_name):
        raise LiepinError(
            SourceStatus.CONTRACT_ERROR,
            f"城市编码已失效：「{city_code}」被猎聘解析为「{echo_name}」，而不是期望的"
            f"「{city_name}」，请核对城市码表后再检索",
        )


def _cards(response) -> tuple[list[dict], tuple[str, str]]:
    """取出岗位卡片列表与回显城市，并区分"风控 / 结构变更 / 真没结果"。"""
    try:
        payload = response.json()
    except Exception as error:
        body = str(getattr(response, "text", "") or "")[:5000].casefold()
        if any(marker in body for marker in _CAPTCHA_MARKERS):
            raise LiepinError(
                SourceStatus.RISK_CONTROL,
                "猎聘要求完成人机验证，请在浏览器打开猎聘处理后重试",
            ) from error
        raise LiepinError(
            SourceStatus.CONTRACT_ERROR, "猎聘返回的不是 JSON，响应结构可能已变化"
        ) from error

    if not isinstance(payload, dict):
        raise LiepinError(SourceStatus.CONTRACT_ERROR, "猎聘响应结构已变化")

    if payload.get("flag") != 1:
        raise LiepinError(
            SourceStatus.RISK_CONTROL,
            "猎聘未接受本次检索（可能触发频率限制），请稍后重试或减少连续检索",
        )

    outer = payload.get("data")
    inner = outer.get("data") if isinstance(outer, dict) else None
    echo = _echo_city(inner) if isinstance(inner, dict) else ("", "")
    cards = inner.get("jobCardList") if isinstance(inner, dict) else None
    if isinstance(cards, list):
        return [card for card in cards if isinstance(card, dict)], echo
    if isinstance(inner, dict):
        # 有些响应会把总数直接写 0，这种情况是真的没结果，不是结构变化
        totals = [inner.get(key) for key in ("totalCount", "numTotal", "total")]
        if any(isinstance(value, int) and value == 0 for value in totals):
            return [], echo
    # 字段缺失 ≠ 没有岗位，必须报结构问题而不是返回空
    raise LiepinError(
        SourceStatus.CONTRACT_ERROR,
        "猎聘未返回岗位列表字段（可能是响应结构变化或被限流，不代表没有岗位）",
    )


def _to_job(card: dict) -> Job | None:
    job = card.get("job") or {}
    comp = card.get("comp") or {}
    if not isinstance(job, dict) or not isinstance(comp, dict):
        return None

    title = str(job.get("title") or "").strip()
    if not title:
        return None

    link = str(job.get("link") or "").strip()
    if link.startswith("/"):
        link = "https://www.liepin.com" + link

    salary_raw = str(job.get("salary") or "").strip()
    salary_min_k, salary_max_k = parse_salary_k(salary_raw)
    experience_raw = str(job.get("requireWorkYears") or "").strip()
    experience_min, experience_max = parse_experience(experience_raw)
    education_raw = str(job.get("requireEduLevel") or "").strip()

    labels = job.get("labels") or []
    skills = (
        tuple(str(label).strip() for label in labels[:8] if str(label).strip())
        if isinstance(labels, list)
        else ()
    )

    location = str(job.get("dq") or "").strip()
    industry = str(comp.get("compIndustry") or "").strip()

    # 列表接口没有 JD 正文：用结构化字段拼一段摘要给排序/展示用，
    # 同时让上层知道 detail_level=list_card，不是全文。
    parts = [
        title,
        f"经验:{experience_raw}" if experience_raw else "",
        f"学历:{education_raw}" if education_raw else "",
        f"技能:{'、'.join(skills)}" if skills else "",
        f"行业:{industry}" if industry else "",
    ]

    return Job(
        external_id=str(job.get("jobId") or link or title),
        title=title,
        company=str(comp.get("compName") or "").strip(),
        salary_raw=salary_raw,
        salary_min_k=salary_min_k,
        salary_max_k=salary_max_k,
        location=location,
        experience_raw=experience_raw,
        experience_min_years=experience_min,
        experience_max_years=experience_max,
        education_raw=education_raw,
        skills=skills,
        summary="；".join(part for part in parts if part),
        apply_url=link,
    )


# ── 对外入口 ────────────────────────────────────────────────────────────────


def fetch_jobs(
    keyword: str,
    city_code: str = "",
    *,
    city_name: str = "",
    page: int = 0,
    page_size: int = PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Job]:
    """检索一页岗位。失败抛 LiepinError；成功但无结果返回空列表。

    city_name 不参与请求，仅用于校验上游回显的城市是否与期望一致。
    """
    if not 0 <= page <= 200:
        raise ValueError("page 必须在 0~200 之间")

    deadline = time.monotonic() + timeout
    search_url = f"{SEARCH_PAGE}?key={quote(keyword)}"
    if city_code:
        search_url += f"&dqs={city_code}"

    session = None
    try:
        session = _session_factory()
        token = _handshake(session, search_url, deadline)
        try:
            response = session.post(
                API_URL,
                headers=_search_headers(search_url, token),
                json=_search_body(keyword, city_code, page, page_size),
                timeout=_budget(deadline),
            )
        except LiepinError:
            raise
        except Exception as error:
            raise LiepinError(
                SourceStatus.NETWORK_UNAVAILABLE, "猎聘检索请求失败，请检查网络后重试"
            ) from error
        _check_http(response)
        cards, (echo_code, echo_name) = _cards(response)
        _verify_city_echo(city_code, city_name, echo_code, echo_name)
    finally:
        closer = getattr(session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    jobs = [_to_job(card) for card in cards]
    return [job for job in jobs if job is not None]
