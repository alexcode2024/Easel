"""Concrete platform-specific policies behind the shared access contract."""
from __future__ import annotations

import re
from pathlib import Path

from platform_access import AccessPolicy, PlatformPolicyAdapter, register_adapter

ROOT = Path(__file__).resolve().parents[3]


def _register(platform: str, display_name: str, domains: tuple[str, ...],
              paths: tuple[str, ...], risk: str, *, cooldown: int = 3600,
              min_delay: float = 8.0, max_delay: float = 15.0,
              budget: int = 20, state_filename: str | None = None,
              risk_codes: tuple[str, ...] = ("429", "461"),
              risk_exit_codes: tuple[int, ...] = (2, 429, 461),
              command_timeout: int = 90) -> None:
    state_path = ROOT / "outputs" / "_benchmarks" / (state_filename or f"{platform}.json")
    shared_lock = ROOT / "outputs" / "_sessions" / f"{platform}-account.lock"
    shared_pace = (ROOT / "outputs" / "_locks" / "xhs-requests.json"
                   if platform == "xiaohongshu" else None)
    adapter = PlatformPolicyAdapter(
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
        response_text_pattern=re.compile(
            r"captcha|verify|验证码|访问频繁|rate.?limit|too.?many.?requests|HTTP\s*(?:429|461)",
            re.IGNORECASE),
    )
    register_adapter(adapter)


_register(
    "xiaohongshu", "小红书", ("xiaohongshu.com",),
    ("/api/", "/user/profile/", "/explore/"),
    r"300012|NeedVerify|verify_page|captcha|验证码|访问频繁|IP存在风险|请切换可靠网络环境|风控|风险|HTTP\s*(?:429|461)\b",
    state_filename="xhs_access.json", risk_codes=("429", "461", "300012"),
    risk_exit_codes=(2, 429, 461), command_timeout=300,
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
