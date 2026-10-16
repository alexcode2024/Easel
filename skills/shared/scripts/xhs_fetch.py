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
   requests. Defaults to the Steady band (20 s +/- 50%, i.e. 10-30 s) documented
   in skill-xhs-analyzer/references/research-loop.md. Faster pacing is possible
   via ``--min-delay`` / ``--max-delay`` but prints a risk warning; the safety
   reference classes anything under 5 s as needing explicit user consent.

Usage
-----
    python xhs_fetch.py --user-id 55753cf4f5a2633511d5a652 [--limit 50]
                        [--details N] [--json OUT]
                        [--min-delay 10] [--max-delay 30]
                        [--anonymous] [--no-fallback]

Exit codes: 0 ok (partial counts as ok, see "complete"), 2 risk-control stop,
            3 nothing retrievable, 4 bad input.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROFILE_NAME = "XiaohongshuProfile"
PROFILE_ROOT = Path.home() / ".easel-browser-profiles"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Pacing bands from skill-xhs-analyzer/references/research-loop.md:
#   Steady (default)  20 s +/- 50%  -> 10-30 s
#   Fast (opt-in)     >= 5 s, burst-capped, requires the user's explicit consent
# Defaults stay in Steady. Going faster prints a warning rather than failing,
# because the operator may knowingly accept the risk.
STEADY_MIN = 10.0
STEADY_MAX = 30.0
FAST_CEILING = 5.0

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
        self.last_at: float | None = None
        self.count = 0

    def wait(self) -> None:
        if self.last_at is not None:
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
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1).replace("undefined", "null"))
    except json.JSONDecodeError:
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
                "xsec_token": entry.get("xsecToken") or card.get("xsecToken") or "",
            })

    return {
        "logged_in_per_ssr": user.get("loggedIn"),
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
          details: int, verbose: bool) -> tuple[dict, list[str], str | None]:
    """Return (result, log_lines, risk_reason)."""
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
                    headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                          viewport={"width": 1440, "height": 900})
                ctx_kind = "anonymous"
            else:
                profile = PROFILE_ROOT / PROFILE_NAME
                if not profile.is_dir():
                    log.append(f"[!] 持久化 profile 不存在：{profile} —— 回退匿名")
                    browser = p.chromium.launch(headless=True)
                    ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                              viewport={"width": 1440, "height": 900})
                    ctx_kind = "anonymous (profile missing)"
                else:
                    ctx = p.chromium.launch_persistent_context(
                        str(profile), headless=True, user_agent=UA, locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        viewport={"width": 1440, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"])
                    ctx_kind = "logged-in (persistent profile)"

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
            resp = ctx.request.get(url, headers={"Referer": "https://www.xiaohongshu.com/"},
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
                        dr = ctx.request.get(durl, headers={"Referer": url}, timeout=25000)
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
    ap.add_argument("--limit", type=int, default=50, help="最多取多少篇（默认 50）")
    ap.add_argument("--details", type=int, default=0,
                    help="额外读取前 N 篇的笔记详情（默认 0，每篇一次请求）")
    ap.add_argument("--min-delay", type=float, default=STEADY_MIN,
                    help=f"请求间隔下限秒（默认 {STEADY_MIN:g}，Steady 档）")
    ap.add_argument("--max-delay", type=float, default=STEADY_MAX,
                    help=f"请求间隔上限秒（默认 {STEADY_MAX:g}，Steady 档）")
    ap.add_argument("--anonymous", action="store_true", help="强制匿名")
    ap.add_argument("--no-fallback", action="store_true", help="登录态失败时不回退匿名")
    ap.add_argument("--json", dest="json_out", help="把结果写到该文件")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

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

    result, run_log, risk = fetch(a.user_id, anonymous=a.anonymous, throttle=throttle,
                                  limit=a.limit, details=a.details, verbose=not a.quiet)
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
    result["requests_made"] = throttle.count
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
            print(f"  作品     SSR 内 {prof.get('notes_in_ssr')} 篇，"
                  f"取用 {len(result.get('notes') or [])} 篇"
                  f"（账号总数请以主页显示为准；翻页需签名头，本工具不支持）")
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
    sys.exit(main())
