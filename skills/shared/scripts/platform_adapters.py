"""Concrete platform-specific policies behind the shared access contract."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from platform_access import AccessPolicy, AccessStopped, PlatformPolicyAdapter, register_adapter

ROOT = Path(__file__).resolve().parents[3]


class _XhsPageText(HTMLParser):
    """Only inspect page text, never bundle names, attributes or embedded SSR data."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "template"):
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class XiaohongshuPolicyAdapter(PlatformPolicyAdapter):
    """Keep XHS verification-page interpretation inside its platform adapter."""

    def inspect_response(self, response, resource):
        self.inspect_status(response, resource)
        # Check final/redirect destinations as challenges may have an empty body.
        headers = getattr(response, "headers", {}) or {}
        for url in (getattr(response, "url", ""), headers.get("location", "")):
            parsed = urlsplit(str(url))
            challenge = re.search(r"/(?:captcha|verify|verify_page|website-login)(?:/|$)",
                                  parsed.path, re.IGNORECASE)
            codes = parse_qs(parsed.query).get("error_code", [])
            if challenge or any(code in self.risk_codes for code in codes):
                reason = "小红书验证页面跳转" if challenge else "小红书风险错误码跳转"
                self.trip(resource, reason)
                raise AccessStopped(reason)
        text = getattr(response, "text", "")
        text = text() if callable(text) else text
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            # Preserve error-code and error-message checks; don't scan JSON data fields.
            return super().inspect_response(response, resource)
        page = _XhsPageText()
        page.feed(text or "")
        page.close()
        return super().inspect_response(SimpleNamespace(text=" ".join(page.parts)), resource)


def _register(platform: str, display_name: str, domains: tuple[str, ...],
              paths: tuple[str, ...], risk: str, *, cooldown: int = 3600,
              min_delay: float = 8.0, max_delay: float = 15.0,
              budget: int = 20, state_filename: str | None = None,
              risk_codes: tuple[str, ...] = ("429", "461"),
              risk_exit_codes: tuple[int, ...] = (2, 429, 461),
              command_timeout: int = 90,
              adapter_type: type[PlatformPolicyAdapter] = PlatformPolicyAdapter,
              response_text_pattern: str = (
                  r"captcha|verify|验证码|访问频繁|rate.?limit|too.?many.?requests|HTTP\s*(?:429|461)"
              )) -> None:
    state_path = ROOT / "outputs" / "_benchmarks" / (state_filename or f"{platform}.json")
    shared_lock = ROOT / "outputs" / "_sessions" / f"{platform}-account.lock"
    shared_pace = (ROOT / "outputs" / "_locks" / "xhs-requests.json"
                   if platform == "xiaohongshu" else None)
    adapter = adapter_type(
        policy=AccessPolicy(platform, display_name, concurrency=1,
                            request_delay_min=min_delay, request_delay_max=max_delay,
                            risk_cooldown=cooldown, request_budget=budget,
                            command_timeout=command_timeout),
        domains=domains,
        api_paths=paths,
        risk_pattern=re.compile(risk, re.IGNORECASE),
        persistent_path=state_path,
        shared_lock_path=shared_lock,
        shared_pace_path=shared_pace,
        risk_codes=risk_codes,
        risk_exit_codes=risk_exit_codes,
        response_text_pattern=re.compile(response_text_pattern, re.IGNORECASE),
    )
    register_adapter(adapter)


_register(
    "xiaohongshu", "小红书", ("xiaohongshu.com",),
    ("/api/", "/user/profile/", "/explore/"),
    r"300012|NeedVerify|verify_page|captcha|验证码|访问频繁|IP存在风险|请切换可靠网络环境|风控|风险|HTTP\s*(?:429|461)\b",
    state_filename="xhs_access.json", risk_codes=("429", "461", "300012"),
    risk_exit_codes=(2, 429, 461), command_timeout=300,
    adapter_type=XiaohongshuPolicyAdapter,
    response_text_pattern=(
        r"\b(?:NeedVerify|verify_page)\b|验证码|安全验证|访问频繁|请求过于频繁|"
        r"IP存在风险|请切换可靠网络环境|请完成验证|验证后继续|"
        r"\b(?:captcha|security verification|verify your (?:identity|request)|"
        r"rate[ -]?limit(?:ed)?|too many requests)\b|HTTP\s*(?:429|461)\b"
    ),
)
_register(
    "douyin", "抖音", ("douyin.com", "iesdouyin.com", "snssdk.com", "amemv.com"),
    ("/aweme/v1/", "/api/", "/creator-micro/", "/user/", "/video/", "/note/"),
    r"captcha|verify|验证码|验证|访问频繁|请求过于频繁|风控|risk.?control|too.?many.?requests|HTTP\s*(?:429|461)|status.?code.{0,8}(?:429|461)",
)
_register(
    "tiktok", "TikTok", ("tiktok.com", "tiktokv.com", "byteoversea.com"),
    ("/api/", "/@", "/video/"),
    r"captcha|verify|验证码|验证|too many requests|rate.?limit|risk.?control|HTTP\s*(?:429|461)|status.?code.{0,8}(?:429|461)",
)
