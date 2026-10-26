"""Xiaohongshu adapter for the shared platform access runtime."""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import platform_access
import platform_adapters

ROOT = Path(__file__).resolve().parents[3]
ADAPTER = platform_access.get_adapter("xiaohongshu")
RESOURCE = platform_access.AccessResource("xiaohongshu")
STATE_FILE = ADAPTER.persistent_path
LOCK_FILE = ADAPTER.lock_path(RESOURCE)
PACE_FILE = ADAPTER.pace_path(RESOURCE)
REQUEST_DELAY_MIN = ADAPTER.policy.request_delay_min
REQUEST_DELAY_MAX = ADAPTER.policy.request_delay_max
RISK_PATTERN = ADAPTER.risk_pattern
_budget = ADAPTER.policy.request_budget
AccessStopped = platform_access.AccessStopped
RequestBudgetExhausted = platform_access.RequestBudgetExhausted


def read_state(path: Path) -> dict:
    return platform_access._read_json(path)


def write_state(path: Path, data: dict) -> None:
    platform_access._write_json(path, data)


def _compat_adapter():
    return replace(ADAPTER, persistent_path=STATE_FILE,
                   shared_lock_path=LOCK_FILE, shared_pace_path=PACE_FILE)


def check_breaker() -> None:
    _compat_adapter().check_breaker(RESOURCE)


def trip(reason: str) -> None:
    _compat_adapter().trip(RESOURCE, reason)


def inspect_response(response) -> None:
    _compat_adapter().inspect_response(response, RESOURCE)


def inspect_status(response) -> None:
    _compat_adapter().inspect_status(response, RESOURCE)


def is_risk_error(error) -> bool:
    return bool(platform_access.classify_error(_compat_adapter(), str(error)))


def active() -> bool:
    return platform_access.is_active("xiaohongshu")


@contextmanager
def session():
    with platform_access.session(_compat_adapter(), budget=_budget):
        yield


def request(call, *args, **kwargs):
    return platform_access.request(call, *args, **kwargs)


def set_budget(budget: int) -> None:
    global _budget
    _budget = max(0, int(budget))
    platform_access.set_request_budget(_budget)


def run_cli(main, *, enabled: bool = True) -> int:
    return platform_access.run_cli(main, platform=_compat_adapter(), enabled=enabled, budget=_budget)


def install_browser_guard(context) -> None:
    platform_access.install_browser_guard(context, platform="xiaohongshu")
