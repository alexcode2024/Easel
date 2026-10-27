#!/usr/bin/env python3
"""Fetch a Xiaohongshu creator's public profile + recent works.

Design notes (why it looks like this)
-------------------------------------
1. Logged-in first, anonymous fallback.  The account's persistent browser
   profile (``~/.easel-browser-profiles/XiaohongshuProfile``) holds the session
   created by the Web login flow. Measured behaviour on this machine:

     ctx.request.get() with profile      -> 200, full SSR (nickname + notes)
     ctx.request.get() without profile   -> 200, ~10 KB shell, no notes
     page.goto() with profile            -> redirect to /login  (blocked)

   So the data is reachable through the *request* API of a persistent context,
   never through page navigation. Anonymous is kept as a documented fallback but
   on this network it returns an empty shell.

2. Only the SSR-embedded first page is reliably obtainable.  Deeper pagination
   goes through ``/api/sns/web/v1/user_posted``, which requires the signed
   headers ``x-s`` / ``x-t`` that Easel does not implement (always HTTP 406).
   ``--limit`` therefore caps what we take, it does not promise that many.

3. Circuit breaker is absolute.  Any risk-control signal (300012, NeedVerify,
   login wall, expired session) stops the run immediately and is reported.
   Per skills/builtin/skill-xhs-analyzer/references/research-loop.md we never
   retry through a block -- that turns soft throttling into an hours-long ban.

4. Throttling.  Serial only, one request in flight, random jitter between
   requests. Defaults to the shared random 8-15 s request interval. Faster pacing
   is possible via ``--min-delay`` / ``--max-delay`` but prints a risk warning; the safety
   reference classes anything under 5 s as needing explicit user consent.

Usage
-----
    python xhs_fetch.py --user-id 55753cf4f5a2633511d5a652 [--limit 20]
                        [--details N] [--json OUT]
                        [--min-delay 8] [--max-delay 15]
                        [--anonymous] [--no-fallback]

Exit codes: 0 ok (partial counts as ok, see "complete"), 2 risk-control stop,
            3 nothing retrievable, 4 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
import xhs_access
import xhs_browser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROFILE_NAME = "XiaohongshuProfile"
PROFILE_ROOT = Path.home() / ".easel-browser-profiles"
UA = xhs_browser.USER_AGENT  # compatibility export; configuration lives in xhs_browser

STEADY_MIN = xhs_access.REQUEST_DELAY_MIN
STEADY_MAX = xhs_access.REQUEST_DELAY_MAX
FAST_CEILING = 5.0
DEFAULT_REQUEST_BUDGET = 8

# Markers that mean "stop now", never "retry".
# Deliberately specific: a bare "verify" matches ordinary bundle/JS identifiers
# and produced a false circuit-break during testing.
RISK_MARKERS = (
    "300012",
    "IP存在风险",
    "请切换可靠网络环境",
    "NeedVerify",
    "verify_page",
    "captcha",
    "访问频繁",
)
# The login wall shows up as a redirect target rather than a body marker.
RISK_URL_MARKERS = ("/login", "website-login", "error_code=")

# ----------------------------------------------------------------------------
# throttling
# ----------------------------------------------------------------------------


class Throttle:
    """Serial, jittered pacing. One request in flight, never parallel."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.last_at: float | None = time.time()
        self.count = 0

    def wait(self) -> None:
        if self.last_at is not None and not xhs_access.active():
            delay = random.uniform(self.min_delay, self.max_delay)
            time.sleep(delay)
        self.last_at = time.time()
        self.count += 1

    def describe(self) -> str:
        return f"{self.min_delay:g}-{self.max_delay:g}s jitter, serial"


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------


def _initial_state(html: str) -> dict | None:
    """从 SSR HTML 里取出 ``window.__INITIAL_STATE__``。

    不能用「非贪婪匹配到 ``</script>``」那种写法：笔记页的 state 有 4.7 万字符，
    只要**字符串值里出现 ``</script>``**（笔记正文常见），正则就会在那里截断、
    JSON 解析失败 → 整页数据被判成「没有」——正是详情页拿不到 interactInfo 的原因。
    这里用 ``raw_decode``：它按 JSON 语法走，字符串里的括号/标签都不会干扰。
    """
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not m:
        return None
    text = html[m.end():]
    # 只把**裸的** undefined 换成 null，别动字符串里的 "undefined"（仓库既有测试盯着这点）
    serialized = re.sub(
        r'"(?:\\.|[^"\\])*"|\bundefined\b',
        lambda token: "null" if token.group() == "undefined" else token.group(),
        text)
    try:
        state, _end = json.JSONDecoder().raw_decode(serialized)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _state_from_html(html: str) -> dict:
    """从 SSR HTML 里取 ``window.__INITIAL_STATE__``（YAML 解析，容 JS 语法）。

    用 YAML 而不是 JSON：state 里带 ``new Map([])`` 这类非 JSON 语法，
    JSON 解析器会直接失败（这正是本仓库此前读不到详情数据的原因）。
    做法参考 XHS-Downloader 项目。
    """
    if not html:
        return {}
    from lxml.etree import HTML
    from yaml import safe_load

    scripts = HTML(html).xpath("//script/text()")
    scripts.reverse()          # 页面前面还有别的脚本体，真身是最后一段
    raw = next((s for s in scripts if s.startswith("window.__INITIAL_STATE__")), "")
    if not raw:
        return {}
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "",
                     raw.lstrip("window.__INITIAL_STATE__="))
    cleaned = cleaned.replace("new Map([])", "[]").replace("undefined", "null")
    try:
        state = safe_load(cleaned)
    except Exception:  # noqa: BLE001 - 结构变化时不猜
        return {}
    return state if isinstance(state, dict) else {}


def _http_get(url: str, cookie: str = "", *, impersonate: str = xhs_browser.IMPERSONATE, timeout: int = 20):
    """带 TLS 指纹伪装的 HTTP GET。

    使用集中配置的 Chrome HTTP/TLS 模拟目标；该配置不保证平台接受请求。
    Cookie 与请求节奏仍由既有账号访问层管理。
    """
    from curl_cffi import requests as creq

    if impersonate != xhs_browser.IMPERSONATE:
        raise ValueError("小红书 HTTP 浏览器配置须统一，不能单独覆盖 impersonate")
    return xhs_access.request(
        creq.get, url, **xhs_browser.http_options("document"),
        cookies=parse_cookie(cookie) if cookie else None,
        timeout=timeout, verify=False, allow_redirects=True)


def parse_cookie(cookie: str) -> dict:
    """把浏览器里复制出来的 cookie 串解析成 dict（容忍换行/多余分号）。"""
    jar: dict[str, str] = {}
    for part in re.split(r"[;\n]", cookie or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            jar[name] = value.strip()
    return jar


def fetch_profile_http(user_id: str, cookie: str, *, impersonate: str = xhs_browser.IMPERSONATE,
                       timeout: int = 20) -> dict:
    """纯 HTTP 读主页（**带 cookie 才有真实数据**）——不用浏览器。

    实测对比（同账号）：
    - 不带 cookie：``loggedIn=False``、粉丝/互动是「1万+」占位值、作品**没有 ID**
    - 带真实 ``web_session``：``loggedIn=True``、粉丝精确值、作品 **ID 齐全**（还多 2 条）

    返回结构与 ``fetch()`` 一致（``profile`` / ``notes``），便于上层复用同一套解析。
    """
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    response = _http_get(url, cookie, impersonate=impersonate, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"主页 HTTP {response.status_code}")
    state = _state_from_html(response.text)
    if not state:
        raise RuntimeError("主页 SSR 解析失败（可能被风控或页面改版）")
    user = state.get("user") or {}
    page = user.get("userPageData") or {}
    basic = page.get("basicInfo") or {}

    def interaction(kind: str):
        for item in page.get("interactions") or []:
            if isinstance(item, dict) and item.get("type") == kind:
                return item.get("count")
        return None

    notes: list[dict] = []
    for group in user.get("notes") or []:
        if not isinstance(group, list):
            continue
        for entry in group:
            note = _normalize_note(entry)
            if note is not None:
                notes.append(note)
    profile = {
        "logged_in_per_ssr": bool(user.get("loggedIn")),
        "guest": bool((user.get("userInfo") or {}).get("guest")),
        "nickname": basic.get("nickname") or "",
        "red_id": basic.get("redId") or "",
        "desc": basic.get("desc") or "",
        "ip_location": basic.get("ipLocation") or "",
        "fans_raw": interaction("fans"),
        "interaction_raw": interaction("interaction"),
        "fans_num": _count_number(interaction("fans")),
        "liked_exact": page.get("liked"),
        "collected_exact": page.get("collected"),
        "posted_exact": page.get("posted"),
        "notes_in_ssr": len(notes),
    }
    return {"mode": "http+cookie", "profile": profile, "notes": notes,
            "request_log": [f"[i] 纯 HTTP 读主页（带 cookie）：{len(notes)} 条作品"]}


def fetch_note_detail(work_id: str, xsec_token: str = "", *, cookie: str = "",
                      impersonate: str = xhs_browser.IMPERSONATE, timeout: int = 20,
                      referer: str = "https://www.xiaohongshu.com/") -> dict:
    """纯 HTTP 读单条笔记的互动数据（点赞/收藏/评论/分享）——**不用浏览器、不用登录**。

    做法参考 XHS-Downloader（JoeanAmier）：
    1. ``curl_cffi`` + ``impersonate`` 伪装真实 Chrome 的 TLS 指纹 —— 这是关键，
       普通 HTTP 客户端（requests/urllib）的指纹会被小红书判风险（300012），
       而 Playwright 无头内核同样会被判；伪装指纹的纯 HTTP 反而最稳、最快。
    2. lxml 取出 ``window.__INITIAL_STATE__`` 那段脚本（取**最后**一段，页面前面还有别的）。
    3. **用 YAML 解析**：state 里带 ``new Map([])`` 这种非 JSON 语法，JSON 解析器会直接失败
       （这是本仓库此前读不到详情数据的直接原因）；替换成 ``[]`` 后 YAML 能正常解析。
    4. 详情在 ``note.noteDetailMap[-1].note.interactInfo``（顶层 ``note``，
       不是 ``AiNoteDetailStore`` —— 后者是空的 ``new Map([])``）。

    返回 ``{"liked","collected","comment","share","title","author","note_id"}``，
    数值已按「1.3万」这类展示串解析成整数；拿不到就返回空 dict（不编造）。
    """
    url = f"https://www.xiaohongshu.com/explore/{work_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_user"
    response = _http_get(url, cookie, impersonate=impersonate, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"笔记页 HTTP {response.status_code}")
    state = _state_from_html(response.text)
    detail_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if not isinstance(detail_map, dict) or not detail_map:
        return {}
    note = (list(detail_map.values())[-1] or {}).get("note") or {}
    info = note.get("interactInfo") or {}
    out = {
        "note_id": note.get("noteId") or work_id,
        "title": note.get("title") or "",
        "author": ((note.get("user") or {}).get("nickname")) or "",
        "liked": _count_number(info.get("likedCount")),
        "collected": _count_number(info.get("collectedCount")),
        "comment": _count_number(info.get("commentCount")),
        "share": _count_number(info.get("shareCount")),
    }
    return out if any(out[k] is not None for k in ("liked", "collected", "comment")) else {}


def _signed_api_get(endpoint: str, params: dict, cookie: str, *, timeout: int = 25,
                    referer: str = "https://www.xiaohongshu.com/"):
    """调 ``edith.xiaohongshu.com`` 的签名接口（xhshow 生成 x-s / x-s-common）。

    ⚠️ 必须用 ``Xhshow().build_url()`` 生成 URL：签名覆盖查询串，
    自己 urlencode 拼出来的参数顺序与库内部不一致时会 406（实测踩过）。
    """
    from curl_cffi import requests as creq

    jar = parse_cookie(cookie)
    signer = xhs_browser.signer()
    url = signer.build_url(endpoint, params)
    options = xhs_browser.http_options("api", referer=referer)
    options["headers"] = signer.sign_headers_get(uri=endpoint, cookies=jar, params=params) | options["headers"]
    return xhs_access.request(creq.get, url, **options, cookies=jar, timeout=timeout, verify=False,
                              allow_redirects=True)


def fetch_otherinfo(user_id: str, cookie: str) -> dict:
    """签名接口读账号指标（昵称/头像/粉丝/获赞/收藏/作品数）。

    为什么不用主页 SSR：**带 cookie 时小红书返回的是登录态 SPA 壳页**
    （实测 10KB、没有 ``__INITIAL_STATE__``），游客态才有完整 SSR。
    所以带 cookie 的取数必须走接口，指标也一样。
    """
    endpoint = "https://edith.xiaohongshu.com/api/sns/web/v1/user/otherinfo"
    params = {"target_user_id": user_id}
    response = _signed_api_get(endpoint, params, cookie,
                              referer=f"https://www.xiaohongshu.com/user/profile/{user_id}")
    if response.status_code != 200:
        raise RuntimeError(f"账号信息接口 HTTP {response.status_code}")
    body = response.json()
    data = body.get("data") or {}
    basic = data.get("basic_info") or data.get("basicInfo") or {}
    inter = {i.get("type"): i.get("count") for i in (data.get("interactions") or [])
             if isinstance(i, dict)}
    return {
        "nickname": basic.get("nickname") or "",
        "red_id": basic.get("red_id") or basic.get("redId") or "",
        "desc": basic.get("desc") or "",
        "avatar": basic.get("imageb") or basic.get("images") or basic.get("image") or "",
        "fans_raw": inter.get("fans"),
        "follows_raw": inter.get("follows"),
        "liked_exact": data.get("liked"),
        "collected_exact": data.get("collected"),
        "posted_exact": data.get("posted"),
    }


def fetch_profile_api(user_id: str, cookie: str, *, limit: int = 20,
                      threshold: tuple[float, float] = (STEADY_MIN, STEADY_MAX),
                      verbose: bool = False, request_budget: int = DEFAULT_REQUEST_BUDGET) -> dict:
    """带 cookie 的完整取数：**指标走 otherinfo 接口 + 作品走 user_posted 分页接口**。

    主页 SSR 在登录态下是空壳，这条路（本函数）才是带 cookie 时的正路。
    风控或限流时立即停止，不回退游客身份继续访问。
    """
    log: list[str] = []
    warnings: list[str] = []
    notes: list[dict] = []
    pages = 0
    listed_via_api = False
    try:
        throttle = Throttle(*threshold)
        throttle.wait()
        info = fetch_otherinfo(user_id, cookie)
        log.append(f"[i] 账号指标（otherinfo）：粉丝 {info.get('fans_raw')}，"
                   f"获赞 {info.get('liked_exact')}，收藏 {info.get('collected_exact')}，"
                   f"作品 {info.get('posted_exact')}")
    except Exception as error:  # noqa: BLE001
        if isinstance(error, xhs_access.AccessStopped) or xhs_access.is_risk_error(error):
            if isinstance(error, xhs_access.RequestBudgetExhausted):
                raise
            raise xhs_access.AccessStopped("账号指标触发平台校验，本轮停止") from error
        info = {}
        warnings.append(f"账号指标接口失败：{type(error).__name__}: {error}")
    try:
        paged = fetch_profile_paged(user_id, cookie, limit=limit, threshold=threshold,
                                    throttle=throttle,
                                    verbose=verbose, max_requests=max(0, request_budget - 1))
        notes, pages = paged["notes"], paged["pages"]
        listed_via_api = True
        log += paged.get("request_log") or []
        if paged.get("risk_control"):
            raise xhs_access.AccessStopped(f"列表接口命中风控信号：{paged['risk_control']}")
    except Exception as error:  # noqa: BLE001
        if isinstance(error, xhs_access.AccessStopped) or xhs_access.is_risk_error(error):
            if isinstance(error, xhs_access.RequestBudgetExhausted):
                raise
            raise xhs_access.AccessStopped("作品列表触发平台校验，本轮停止") from error
        logger = f"[!] 作品列表接口不可用（{type(error).__name__}: {error}）"
        log.append(logger)
        # 退回游客 SSR：显式不带 cookie，因为带 cookie 时主页是空壳
        try:
            guest = fetch_profile_http(user_id, "")
            notes = guest.get("notes") or []
            warnings.append("作品列表接口暂时被平台限流，本次用游客列表（有标题/封面/点赞，"
                            "但没有作品 ID：无原文链接、无法深挖）；稍后重试即可恢复")
            log.append(f"[i] 游客 SSR 退回成功：{len(notes)} 条")
        except Exception as guest_error:  # noqa: BLE001
            warnings.append(f"退回游客列表也失败：{type(guest_error).__name__}: {guest_error}")
    profile = {
        "logged_in_per_ssr": bool(info),          # 指标接口能返回即说明会话有效
        "guest": False,
        "nickname": info.get("nickname") or "",
        "red_id": info.get("red_id") or "",
        "desc": info.get("desc") or "",
        "avatar": info.get("avatar") or "",
        "fans_raw": info.get("fans_raw"),
        "interaction_raw": None,
        "fans_num": _count_number(info.get("fans_raw")),
        "liked_exact": info.get("liked_exact"),
        "collected_exact": info.get("collected_exact"),
        "posted_exact": info.get("posted_exact"),
        "notes_in_ssr": len(notes),
    }
    return {"mode": "http+api", "profile": profile, "notes": notes,
            "paging": {"pages": pages} if (pages and listed_via_api) else {},
            "warnings": warnings, "request_log": log,
            "requests_made": throttle.count if 'throttle' in locals() else 0}


def fetch_profile_paged(user_id: str, cookie: str, *, limit: int = 0,
                        threshold: tuple[float, float] = (STEADY_MIN, STEADY_MAX),
                        verbose: bool = False, max_pages: int = 40,
                        throttle: Throttle | None = None,
                        max_requests: int = DEFAULT_REQUEST_BUDGET) -> dict:
    """纯 HTTP **翻页**读主页全部作品（走签名接口 ``user_posted``）。

    主页 SSR 一次只给 30 条；要拿全量必须调分页接口，而该接口需要 ``x-s``/``x-s-common``
    签名头。这里用 ``xhshow`` 生成签名（第三方纯算库），仍然**不用浏览器**。

    ⚠️ 两个实测坑（都踩过、都已解决）：
    1. 请求 URL 必须用 ``Xhshow().build_url()`` 生成——签名覆盖查询串，
       自己 ``urlencode`` 拼出来的参数顺序与库内部不一致时会 406。
    2. ``xsec_token``/``xsec_source`` 传空串即可（浏览器真实请求里就是空的）。
    """
    from curl_cffi import requests as creq

    jar = parse_cookie(cookie)
    if not jar:
        raise RuntimeError("cookie 为空，无法调用签名接口")
    signer = xhs_browser.signer()
    endpoint = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
    options = xhs_browser.http_options("api")
    throttle = throttle or Throttle(*threshold)
    notes: list[dict] = []
    seen: set[str] = set()
    cursor = ""
    log: list[str] = []
    risk: str | None = None
    pages = 0
    budget_exhausted = False
    while pages < max_pages:
        if pages >= max(0, max_requests):
            budget_exhausted = True
            log.append(f"[i] 已达到本次请求预算 {max_requests}，停止翻页")
            break
        params = {"num": 30, "cursor": cursor, "user_id": user_id,
                  "image_formats": "jpg,webp,avif", "xsec_token": "", "xsec_source": ""}
        url = signer.build_url(endpoint, params)      # 必须用库的 URL：签名覆盖查询串
        headers = signer.sign_headers_get(uri=endpoint, cookies=jar, params=params)
        throttle.wait()
        response = xhs_access.request(creq.get, url,
                            **(options | {"headers": headers | options["headers"]}), cookies=jar, timeout=25,
                            verify=False, allow_redirects=True)
        pages += 1
        if response.status_code != 200:
            raise RuntimeError(f"分页接口 HTTP {response.status_code}（第 {pages} 页）")
        body = response.json()
        if _risk_hit(response.text):
            risk = str(_risk_hit(response.text))
            log.append(f"[!] 第 {pages} 页命中风控信号：{risk} —— 已停止，不重试")
            break
        if not body.get("success"):
            raise RuntimeError(f"分页接口返回失败：code={body.get('code')} msg={body.get('msg')}")
        data = body.get("data") or {}
        for item in data.get("notes") or []:
            note = _normalize_note(item)
            if note is None or note["note_id"] in seen:
                continue
            seen.add(note["note_id"])
            notes.append(note)
        log.append(f"[i] 第 {pages} 页：本页 {len(data.get('notes') or [])} 条，累计 {len(notes)} 条")
        if verbose:
            print(log[-1])
        if limit and len(notes) >= limit:
            notes = notes[:limit]
            log.append(f"[i] 已达上限 {limit} 条，停止翻页")
            break
        if not data.get("has_more"):
            log.append(f"[i] 平台报告 has_more=false：公开作品已取完（{len(notes)} 条）")
            break
        cursor = str(data.get("cursor") or "")
        if not cursor:
            log.append("[i] 无下一页游标，停止翻页")
            break
    return {"mode": "http+paged", "notes": notes, "pages": pages,
            "complete": not risk and not budget_exhausted and pages < max_pages,
            "request_log": log, "risk_control": risk,
            "requests_made": throttle.count, "budget_exhausted": budget_exhausted}


def _normalize_note(item: dict) -> dict | None:
    """把两种形态的作品条目归一化。

    - **分页接口**（``user_posted``）：字段在顶层且是蛇形 ——
      ``note_id`` / ``display_title`` / ``interact_info.liked_count`` / ``cover.url_default``
    - **主页 SSR**：包在 ``noteCard`` 里且是驼峰 ——
      ``noteCard.noteId`` / ``displayTitle`` / ``interactInfo.likedCount`` / ``cover.infoList``

    两种都要认，否则会出现"拿到了条目却全是空字段"（实测踩过）。
    """
    if not isinstance(item, dict):
        return None
    card = item.get("note_card") or item.get("noteCard") or item
    if not isinstance(card, dict):
        return None
    note_id = str(item.get("note_id") or item.get("id") or card.get("noteId") or "")
    title = card.get("display_title") or card.get("displayTitle") or card.get("title") or ""
    info = card.get("interact_info") or card.get("interactInfo") or {}
    liked = info.get("liked_count") if info.get("liked_count") is not None else info.get("likedCount")
    cover = _cover_url(card)
    # ⚠️ 没有 note_id 是**合法**情况：游客态下平台就是不给作品 ID（实测 32 条全为空）。
    # 早先这里把「无 ID」当无效条目丢掉，导致游客态整批作品变成 0 条——只有真正的空壳才该丢。
    if not note_id and not title and not cover and liked is None:
        return None
    return {
        "note_id": note_id,
        "title": title,
        "type": card.get("type") or "",
        "liked_count": liked,
        "liked_count_num": _count_number(liked),
        "sticky": bool(info.get("sticky")),
        "time_ms": card.get("time") or item.get("time"),
        "cover": cover,
        "xsec_token": (item.get("xsec_token") or item.get("xsecToken")
                       or card.get("xsecToken") or ""),
    }


def _browser_channel() -> str | None:
    """有头模式的浏览器内核：本机 Chrome / Edge。

    无头 Playwright Chromium 会被小红书判 300012（IP 风险限制），同一网络下有头正常，
    所以默认走有头 + 本机真实内核。
    """
    if os.name != "nt":
        return None
    pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    chrome = (pf / "Google" / "Chrome" / "Application" / "chrome.exe",
              pf86 / "Google" / "Chrome" / "Application" / "chrome.exe",
              local / "Google" / "Chrome" / "Application" / "chrome.exe")
    if any(p.is_file() for p in chrome):
        return "chrome"
    edge = (pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    if any(p.is_file() for p in edge):
        return "msedge"
    return None


def _count_number(raw) -> int | None:
    """XHS shows '1万+' / '1.2亿' / '115'. Return a lower-bound int, or None."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").rstrip("+")
    mult = 1
    if s.endswith("万"):
        mult, s = 10_000, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def _cover_url(card: dict) -> str:
    """SSR noteCard.cover → 封面图 URL（优先 WB_DFT 默认图，回退第一张）。

    实测结构（2026-10-16 抓包）::

        cover: {"width": 1320, "url": "", "infoList": [
            {"imageScene": "WB_PRV", "url": "http://sns-webpic-qc.xhscdn.com/…"},
            {"imageScene": "WB_DFT", "url": "http://sns-webpic-qc.xhscdn.com/…"}]}

    注意 ``cover.url`` 常为空、真实地址在 ``infoList``，且给的是 http（统一升 https）。
    分页接口用的是蛇形同义字段（``info_list`` / ``image_scene`` / ``url_default``），一并认。
    """
    cover = card.get("cover") or card.get("cover_info") or {}
    if not isinstance(cover, dict):
        return ""
    scene_urls = {}
    for item in (cover.get("infoList") or cover.get("info_list") or []):
        if isinstance(item, dict) and item.get("url"):
            scene_urls[item.get("imageScene") or item.get("image_scene") or ""] = item["url"]
    url = (scene_urls.get("WB_DFT") or cover.get("url") or cover.get("url_default")
           or cover.get("urlDefault") or cover.get("url_pre") or cover.get("urlPre")
           or next(iter(scene_urls.values()), ""))
    url = str(url or "")
    return "https://" + url[7:] if url.startswith("http://") else url


def parse_profile(state: dict) -> dict:
    user = state.get("user") or {}
    page = user.get("userPageData") or {}
    basic = page.get("basicInfo") or {}

    def interaction(kind: str):
        for item in page.get("interactions") or []:
            if item.get("type") == kind:
                return item.get("count")
        return None

    notes: list[dict] = []
    for group in user.get("notes") or []:
        if not isinstance(group, list):
            continue
        for entry in group:
            card = entry.get("noteCard") or {}
            info = card.get("interactInfo") or {}
            notes.append({
                "note_id": entry.get("id") or card.get("noteId") or "",
                "title": card.get("displayTitle") or "",
                "type": card.get("type") or "",
                "liked_count": info.get("likedCount"),
                "liked_count_num": _count_number(info.get("likedCount")),
                "sticky": bool(info.get("sticky")),
                "time_ms": card.get("time"),
                "cover": _cover_url(card),
                "xsec_token": entry.get("xsecToken") or card.get("xsecToken") or "",
            })

    return {
        "logged_in_per_ssr": user.get("loggedIn"),
        # 游客态判定：登录态失效时 SSR 会给「骨架值」——粉丝与「获赞与收藏」
        # 双双回落成 "1万+"（实测 2026-10-16：真实获赞+收藏 53.9 万也显示 1万+），
        # 因此游客态下的展示串不可信，必须让调用方知道（详见 userInfo.guest）。
        "guest": bool((user.get("userInfo") or {}).get("guest")),
        # 精确值：与展示串无关，服务端一直给整数（游客态也给，且会随时间增长）
        "liked_exact": page.get("liked"),
        "collected_exact": page.get("collected"),
        "posted_exact": page.get("posted"),
        "user_id": basic.get("redId") and page.get("basicInfo", {}).get("userId") or "",
        "nickname": basic.get("nickname") or "",
        "red_id": basic.get("redId") or "",
        "desc": basic.get("desc") or "",
        "ip_location": basic.get("ipLocation") or "",
        "tags": [t.get("name") for t in (page.get("tags") or []) if t.get("name")],
        "follows_raw": interaction("follows"),
        "fans_raw": interaction("fans"),
        "interaction_raw": interaction("interaction"),
        "fans_num": _count_number(interaction("fans")),
        "interaction_num": _count_number(interaction("interaction")),
        "note_total_hint": page.get("extraInfo") or None,
        "notes_in_ssr": len(notes),
        "notes": notes,
    }


def dedupe(notes: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for n in notes:
        key = n.get("note_id") or n.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


# ----------------------------------------------------------------------------
# fetching
# ----------------------------------------------------------------------------


def _risk_hit(text: str) -> str | None:
    for marker in RISK_MARKERS:
        if marker in text:
            return marker
    return None


def _risk_in_url(url: str) -> str | None:
    """A redirect to the login wall is itself a login/session signal."""
    low = url or ""
    if "/website-login" in low:
        return "redirect → website-login（登录/风控墙）"
    if "/login" in low:
        return "redirect → /login（需要登录或会话失效）"
    return None


def fetch(user_id: str, *, anonymous: bool, throttle: Throttle, limit: int,
          details: int, verbose: bool, headless: bool = False) -> tuple[dict, list[str], str | None]:
    """Return (result, log_lines, risk_reason).

    ``headless`` 默认 **False（有头）**：无头内核会被小红书判 300012，
    生产调用不要改成无头（``--headless`` 只留给离线自检/调试）。
    """
    from playwright.sync_api import sync_playwright

    log: list[str] = []
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    result: dict = {"user_id_requested": user_id, "mode": None, "profile": None,
                    "notes": [], "complete": False, "limit": limit,
                    "pagination": "unsupported (needs signed x-s/x-t headers)"}

    with sync_playwright() as p:
        ctx = None
        try:
            if anonymous:
                browser = p.chromium.launch(
                    headless=headless, args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(**xhs_browser.context_options())
                ctx_kind = "anonymous"
            else:
                profile = PROFILE_ROOT / PROFILE_NAME
                if not profile.is_dir():
                    log.append(f"[!] 持久化 profile 不存在：{profile} —— 回退匿名")
                    browser = p.chromium.launch(headless=headless)
                    ctx = browser.new_context(**xhs_browser.context_options())
                    ctx_kind = "anonymous (profile missing)"
                else:
                    # 有头（本机 Chrome 内核）：无头 Playwright Chromium 会被小红书判 300012，
                    # 同一网络下有头正常。生产调用一律走这里（--headless 仅留给离线自检）。
                    launch_kwargs: dict = dict(
                        headless=headless, **xhs_browser.context_options(),
                        args=["--disable-blink-features=AutomationControlled"])
                    channel = _browser_channel()
                    if not headless and channel:
                        launch_kwargs["channel"] = channel      # 退回本机 Chrome/Edge
                    ctx = p.chromium.launch_persistent_context(str(profile), **launch_kwargs)
                    ctx_kind = "logged-in (persistent profile)"

            xhs_access.install_browser_guard(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

            # Warm up on the origin so cookies/challenge state settle, then read
            # the profile through the context request API (page.goto gets blocked).
            throttle.wait()
            try:
                page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded",
                          timeout=45000)
                page.wait_for_timeout(2500)
            except Exception as e:  # noqa: BLE001
                log.append(f"[!] 预热导航失败（继续）：{e}")

            throttle.wait()
            resp = xhs_access.request(ctx.request.get, url, headers={"Referer": "https://www.xiaohongshu.com/"},
                                   timeout=25000)
            body = resp.text()
            log.append(f"[i] {ctx_kind} → status={resp.status} bytes={len(body)}")

            if resp.status != 200:
                return result, log, f"HTTP {resp.status}"

            # A followed redirect to the login wall returns 200 with the login
            # page, so inspect the final URL too.
            url_hit = _risk_in_url(resp.url)
            if url_hit:
                return result, log, url_hit

            hit = _risk_hit(body)
            if hit:
                return result, log, f"风控信号 {hit!r}"

            state = _initial_state(body)
            if not state:
                return result, log, None  # empty shell: nothing parsable

            profile = parse_profile(state)
            notes = dedupe(profile.pop("notes"))
            result["mode"] = ctx_kind
            result["profile"] = profile
            result["notes"] = notes[:limit] if limit else notes
            result["complete"] = bool(notes) and len(notes) >= limit
            log.append(f"[i] SSR 解析：{profile.get('nickname') or '?'} / "
                       f"{len(notes)} 篇（上限 {limit}）")

            # Optional per-note detail. Each detail read is a request, so it is
            # paced and circuit-broken like everything else.
            if details > 0 and notes:
                target = [n for n in notes if n.get("note_id")][:details]
                log.append(f"[i] 追加读取 {len(target)} 篇笔记详情（节流中，预计 "
                           f"{len(target) * (throttle.min_delay + throttle.max_delay) / 2:.0f}s+）")
                for n in target:
                    throttle.wait()
                    durl = (f"https://www.xiaohongshu.com/explore/{n['note_id']}"
                            f"?xsec_token={n['xsec_token']}&xsec_source=pc_user")
                    try:
                        dr = xhs_access.request(ctx.request.get, durl, headers={"Referer": url}, timeout=25000)
                        dbody = dr.text()
                        dhit = _risk_hit(dbody)
                        if dhit:
                            return result, log, f"风控信号 {dhit!r}（读详情时）"
                        dst = _initial_state(dbody) or {}
                        dm = ((dst.get("note") or {}).get("noteDetailMap") or {})
                        detail = next(iter(dm.values()), None)
                        if detail and detail.get("note"):
                            nt = detail["note"]
                            n["detail"] = {
                                "title": nt.get("title"),
                                "desc": (nt.get("desc") or "")[:500],
                                "interact": nt.get("interactInfo"),
                                "time": nt.get("time"),
                            }
                        else:
                            n["detail"] = None
                    except Exception as e:  # noqa: BLE001
                        log.append(f"[!] 详情失败 {n['note_id'][:12]}：{e}")
                        n["detail"] = None
            return result, log, None
        finally:
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取小红书创作者主页（带登录态优先）")
    ap.add_argument("--user-id", required=True, help="24 位 user_id（主页 URL 里那段）")
    ap.add_argument("--limit", type=int, default=20, help="最多取多少篇（默认 20）")
    ap.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET,
                    help=f"本次最多请求数（默认 {DEFAULT_REQUEST_BUDGET}）")
    ap.add_argument("--details", type=int, default=0,
                    help="额外读取前 N 篇的笔记详情（默认 0，每篇一次请求）")
    ap.add_argument("--min-delay", type=float, default=STEADY_MIN,
                    help=f"请求间隔下限秒（默认 {STEADY_MIN:g}，Steady 档）")
    ap.add_argument("--max-delay", type=float, default=STEADY_MAX,
                    help=f"请求间隔上限秒（默认 {STEADY_MAX:g}，Steady 档）")
    ap.add_argument("--anonymous", action="store_true", help="强制匿名")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式（默认有头：无头内核会被小红书判 300012，仅供离线自检）")
    ap.add_argument("--no-fallback", action="store_true", help="登录态失败时不回退匿名")
    ap.add_argument("--cookie-file", help="浏览器里复制出来的 cookie 文件；给了就走**纯 HTTP**（不用浏览器）")
    ap.add_argument("--json", dest="json_out", help="把结果写到该文件")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    xhs_access.set_budget(max(1, a.request_budget))

    if not re.fullmatch(r"[0-9a-f]{24}", a.user_id or ""):
        print(f"user_id 格式不对（需 24 位十六进制）：{a.user_id}")
        return 4
    if a.min_delay < 0 or a.max_delay < a.min_delay:
        print("延迟参数不合法")
        return 4
    if a.max_delay < FAST_CEILING and not a.quiet:
        print(f"⚠️  间隔上限 {a.max_delay:g}s 低于 {FAST_CEILING:g}s —— "
              "skill-xhs-analyzer/references/research-loop.md 把该区间归为需要用户显式 opt-in 的 "
              "Fast 档：验证码/300012/账号降级数小时的风险显著上升。", file=sys.stderr)

    throttle = Throttle(a.min_delay, a.max_delay)
    if not a.quiet:
        print(f"[i] 节流：{throttle.describe()}"
              + ("（Steady 档）" if (a.min_delay, a.max_delay) == (STEADY_MIN, STEADY_MAX) else ""))
    log: list[str] = []

    cookie = ""
    if a.cookie_file:
        try:
            cookie = Path(a.cookie_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"cookie 文件读不到：{error}", file=sys.stderr)
            return 4
        if not cookie:
            print("cookie 文件是空的", file=sys.stderr)
            return 4

    if cookie:
        # 有 cookie 就走统一身份的纯 HTTP；平台风险响应仍会触发熔断。
        # 指标走 otherinfo 接口、作品走 user_posted 分页接口 —— 因为**带 cookie 时
        # 主页 SSR 是登录态空壳**（实测 10KB、没有 __INITIAL_STATE__），读不到任何数据。
        try:
            result = fetch_profile_api(a.user_id, cookie, limit=max(int(a.limit or 20), 20),
                                       threshold=(a.min_delay, a.max_delay),
                                       verbose=not a.quiet,
                                       request_budget=max(1, a.request_budget))
        except Exception as error:  # noqa: BLE001
            if isinstance(error, xhs_access.AccessStopped):
                raise
            print(f"ERROR: 纯 HTTP 取数失败：{type(error).__name__}: {error}", file=sys.stderr)
            return 3
        result["notes"] = (result.get("notes") or [])[:a.limit] if a.limit else result.get("notes") or []
        result["limit"] = a.limit
        result["complete"] = bool(result.get("notes"))
        result["cookie_used"] = True
        risk = None
        log += result.get("request_log") or []
        # 接口层的告警（如列表被限流后退回游客列表）要冒泡给上层，不能吞
        extra_warnings = result.get("warnings") or []
    else:
        result, run_log, risk = fetch(a.user_id, anonymous=a.anonymous, throttle=throttle,
                                      limit=a.limit, details=a.details, verbose=not a.quiet,
                                      headless=a.headless)
        log += run_log

        # Fallback: logged-in produced nothing → try anonymous, as requested.
        if (not a.anonymous and not a.no_fallback and risk is None
                and not (result.get("notes"))):
            log.append("[i] 登录态未取到作品 → 回退匿名方式")
            result2, log2, risk2 = fetch(a.user_id, anonymous=True, throttle=throttle,
                                         limit=a.limit, details=0, verbose=not a.quiet)
            log += log2
            if result2.get("notes"):
                result2["fallback_used"] = True
                result, risk = result2, risk2
            else:
                risk = risk2 or risk

    result["request_log"] = log
    result["throttle"] = throttle.describe()
    result["requests_made"] = result.get("requests_made", throttle.count)
    if risk:
        result["risk_control"] = risk

    if not a.quiet:
        for line in log:
            print(line)
        prof = result.get("profile") or {}
        if prof:
            print()
            print(f"  昵称     {prof.get('nickname')}  ({prof.get('red_id')})")
            print(f"  粉丝     {prof.get('fans_raw')}   获赞与收藏 {prof.get('interaction_raw')}")
            print(f"  模式     {result.get('mode')}")
            print(f"  作品     {len(result.get('notes') or [])} 篇"
                  + (f"（分页 {result['paging']['pages']} 页；本项目最多取最新 50 条）"
                     if result.get("paging") else
                     f"（SSR 内 {prof.get('notes_in_ssr')} 篇；分页未启用）"))
        if risk:
            print(f"\n⛔ 风控熔断：{risk}\n   已停止，未做任何重试。请等待后再试。")

    if a.json_out:
        Path(a.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not a.quiet:
            print(f"\nJSON → {a.json_out}")

    if risk:
        return 2
    if not result.get("notes"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(xhs_access.run_cli(main))
