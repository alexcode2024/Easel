"""Shared single-account access policy for Xiaohongshu script entry points."""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE_FILE = ROOT / "outputs" / "_benchmarks" / "xhs_access.json"
LOCK_FILE = ROOT / "outputs" / "_locks" / "xhs-session.lock"
PACE_FILE = ROOT / "outputs" / "_locks" / "xhs-requests.json"
REQUEST_DELAY_MIN = 8.0
REQUEST_DELAY_MAX = 15.0
RISK_PATTERN = re.compile(r"300012|NeedVerify|verify_page|captcha|验证码|访问频繁|IP存在风险|请切换可靠网络环境|风控|风险|HTTP\s*(?:429|461)\b", re.I)
_depth = 0
_requests = 0
_budget = 20


class AccessStopped(RuntimeError):
    pass


class RequestBudgetExhausted(AccessStopped):
    pass


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def check_breaker() -> None:
    if float(read_state(STATE_FILE).get("risk_until") or 0) > time.time():
        raise AccessStopped("小红书风控暂停中，本轮停止且不自动重试")


def trip(reason: str) -> None:
    state = read_state(STATE_FILE)
    if float(state.get("risk_until") or 0) <= time.time():
        now = int(time.time())
        state.update(risk_until=now + 3600, risk_reason=reason,
                     risk_triggered_at=now)
        write_state(STATE_FILE, state)


def inspect_response(response) -> None:
    status = getattr(response, "status_code", getattr(response, "status", None))
    if status in (429, 461):
        trip(f"HTTP {status}")
        raise AccessStopped(f"HTTP {status}")
    text = getattr(response, "text", "")
    if callable(text):
        text = text()
    structured = False
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            structured = True
            if str(body.get("code")) in ("429", "461", "300012"):
                trip(f"平台错误码 {body['code']}")
                raise AccessStopped(f"平台错误码 {body['code']}")
            text = " ".join(str(body.get(key) or "") for key in ("code", "msg", "message", "error"))
    except (ValueError, TypeError):
        pass
    marker = (RISK_PATTERN if structured else re.compile(
        r"300012|NeedVerify|verify_page|IP存在风险|请切换可靠网络环境", re.I)).search(str(text))
    if marker:
        reason = f"平台校验信号 {marker.group()}"
        trip(reason)
        raise AccessStopped(reason)


@contextmanager
def session():
    global _depth, _requests
    if _depth:
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
        else:
            import fcntl
        while True:
            check_breaker()
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.2)
            except OSError:
                if os.name != "nt":
                    raise
                time.sleep(0.2)
        try:
            check_breaker()
            _depth, _requests = 1, 0
            yield
        finally:
            _depth = 0
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def request(call, *args, **kwargs):
    global _requests
    if not _depth:
        return call(*args, **kwargs)
    check_breaker()
    if _requests >= _budget:
        raise RequestBudgetExhausted("本轮请求预算已用完，停止访问")
    previous = float(read_state(PACE_FILE).get("finished_at") or time.time())
    time.sleep(max(0, previous + random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX) - time.time()))
    check_breaker()
    _requests += 1
    try:
        response = call(*args, **kwargs)
        inspect_response(response)
        return response
    finally:
        write_state(PACE_FILE, {"finished_at": time.time()})


def run_cli(main, *, enabled: bool = True) -> int:
    offline = len(sys.argv) > 1 and sys.argv[1] in ("selftest", "check", "plan")
    if not enabled or offline or "--help" in sys.argv or "-h" in sys.argv:
        return main()
    try:
        with session():
            return main()
    except RequestBudgetExhausted as error:
        print(json.dumps({"error": str(error), "budget_exhausted": True}, ensure_ascii=False))
        return 3
    except AccessStopped as error:
        trip("平台访问限制，本轮停止")
        print(json.dumps({"error": str(error), "risk": str(error)}, ensure_ascii=False))
        return 2


def install_browser_guard(context) -> None:
    if not _depth:
        return

    def gate(route):
        try:
            response = request(route.fetch, max_redirects=0)
            route.fulfill(response=response)
        except AccessStopped:
            route.abort()
            context.close()

    context.route(re.compile(r"https://(?:[^/]+\.)?xiaohongshu\.com/(?:api/|user/profile/|explore/)"), gate)

    def inspect(response):
        if "xiaohongshu.com" not in response.url:
            return
        try:
            check_breaker()
            if response.status in (429, 461):
                trip(f"HTTP {response.status}")
                raise AccessStopped("平台限流，本轮停止")
            if "/api/" in response.url:
                inspect_response(response)
        except AccessStopped:
            context.close()

    context.on("response", inspect)
