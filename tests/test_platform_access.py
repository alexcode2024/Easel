"""Offline tests for the platform-neutral access contracts."""
import asyncio
import json
import re
import sys
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/shared/scripts"))
sys.path.insert(0, str(ROOT / "web"))
import platform_access as access
import platform_adapters


def make_adapter(tmp_path, platform="fake", concurrency=1, **policy_options):
    policy = access.AccessPolicy(platform, platform, concurrency=concurrency, **policy_options)
    return access.PlatformPolicyAdapter(
        policy=policy,
        domains=(f"{platform}.example",),
        api_paths=("/api/",),
        risk_pattern=re.compile(r"captcha|rate.?limit|HTTP\s*429", re.I),
        persistent_path=tmp_path / f"{platform}.json",
    )


def test_resource_key_namespaces_platform_and_account():
    assert access.AccessResource("douyin", "creator").key == "douyin:creator"
    assert access.AccessResource("douyin", "creator").digest != access.AccessResource("tiktok", "creator").digest
    assert access.AccessResource("douyin", "creator").digest != access.AccessResource("douyin", "other").digest


@pytest.mark.parametrize("argv", [["--platform", "douyin"], ["--platform=douyin"]])
def test_cli_option_reads_both_platform_argument_forms(argv):
    assert access.cli_option(argv, "--platform") == "douyin"


def test_invalid_platform_policy_ranges_are_rejected():
    with pytest.raises(ValueError):
        access.AccessPolicy("invalid", "Invalid", concurrency=0)
    with pytest.raises(ValueError):
        access.AccessPolicy("invalid", "Invalid", request_delay_min=10, request_delay_max=8)


def test_queue_is_fifo_per_resource_and_isolated_across_platforms(tmp_path):
    queue = access.AsyncResourceQueue(0.001)
    adapter_a = make_adapter(tmp_path, "alpha")
    adapter_b = make_adapter(tmp_path, "beta")
    resource_a = access.AccessResource("alpha")
    resource_b = access.AccessResource("beta")
    order = []

    async def scenario():
        first = await queue.enter(adapter_a, resource_a, "first", adapter_a.check_breaker,
                                  use_process_lock=False)
        follower = asyncio.create_task(queue.enter(
            adapter_a, resource_a, "second", adapter_a.check_breaker, use_process_lock=False))
        other = await queue.enter(adapter_b, resource_b, "other", adapter_b.check_breaker,
                                  use_process_lock=False)
        await asyncio.sleep(0.01)
        assert not follower.done()
        queue.leave(first)
        second = await follower
        order.append(second["label"])
        queue.leave(second)
        queue.leave(other)

    asyncio.run(scenario())
    assert order == ["second"]
    assert not queue._queues


def test_queue_honors_adapter_concurrency(tmp_path):
    queue = access.AsyncResourceQueue(0.001)
    adapter = make_adapter(tmp_path, "parallel", concurrency=2)
    resource = access.AccessResource("parallel")

    async def scenario():
        first = await queue.enter(adapter, resource, "first", adapter.check_breaker,
                                  use_process_lock=False)
        second = await queue.enter(adapter, resource, "second", adapter.check_breaker,
                                   use_process_lock=False)
        third = asyncio.create_task(queue.enter(
            adapter, resource, "third", adapter.check_breaker, use_process_lock=False))
        await asyncio.sleep(0.01)
        assert not third.done()
        queue.leave(first)
        third_entry = await third
        queue.leave(second)
        queue.leave(third_entry)

    asyncio.run(scenario())


def test_breaker_state_is_adapter_and_resource_scoped(tmp_path):
    adapter = make_adapter(tmp_path, "alpha", risk_cooldown=30)
    first = access.AccessResource("alpha", "account-one")
    second = access.AccessResource("alpha", "account-two")
    reason = adapter.mark_result(first, access.AccessResult(
        exit_code=1, payload={"error": "captcha required"}))
    assert "captcha" in reason
    with pytest.raises(access.AccessStopped):
        adapter.check_breaker(first)
    adapter.check_breaker(second)
    assert adapter.state_path(first) != adapter.state_path(second)
    assert json.loads(adapter.state_path(first).read_text())["risk_until"] > int(time.time())


def test_adapter_owns_platform_response_classification(tmp_path):
    adapter = make_adapter(tmp_path, "alpha", risk_cooldown=30)
    resource = access.AccessResource("alpha")
    assert adapter.matches_url("https://api.alpha.example/api/items")
    assert not adapter.matches_url("https://unrelated.example/api/items")
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(SimpleNamespace(status_code=429, text="{}"), resource)
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(SimpleNamespace(
            status_code=200, text='{"message":"rate limit exceeded"}'), resource)
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(SimpleNamespace(
            status_code=200, text="captcha required"), resource)
    adapter.inspect_response(SimpleNamespace(
        status_code=200, text='{"data":{"caption":"rate limit explained"}}'), resource)


def test_browser_guard_skips_navigation_and_gates_api_requests(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, "browser")
    resource = access.AccessResource("browser")
    events = {}

    class Context:
        def route(self, matcher, callback):
            events["matcher"] = matcher
            events["route"] = callback

        def on(self, name, callback):
            events[name] = callback

        def close(self):
            events["closed"] = True

    route_calls = []

    class Route:
        def __init__(self, resource_type):
            self.request = SimpleNamespace(resource_type=resource_type)

        def continue_(self):
            route_calls.append("continued")

        def fetch(self, **_kwargs):
            route_calls.append("fetched")
            return SimpleNamespace(status_code=200, text="{}")

        def fulfill(self, response):
            route_calls.append("fulfilled")

        def abort(self):
            route_calls.append("aborted")

    context = Context()
    token = access._ACTIVE_SESSION.set(
        access._ActiveSession(adapter, resource, None, budget=2))
    try:
        access.install_browser_guard(context, platform="browser")
        events["route"](Route("document"))
        events["route"](Route("xhr"))
    finally:
        access._ACTIVE_SESSION.reset(token)
    assert route_calls == ["continued", "fetched", "fulfilled"]


def test_child_session_reuses_parent_resource_lock(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, "alpha")
    resource = access.AccessResource("alpha")
    parent_lock = access.FileResourceLock(adapter.lock_path(resource))
    assert parent_lock.acquire(timeout=0)
    monkeypatch.setenv(access.RESOURCE_ENV, resource.key)
    try:
        with access.session(adapter):
            assert access.active_resource() == resource
    finally:
        parent_lock.release()


def test_child_process_reuses_web_parent_lock(tmp_path):
    adapter = make_adapter(tmp_path, "child")
    adapter.shared_lock_path = tmp_path / "shared.lock"
    resource = access.AccessResource("child")
    parent_lock = access.FileResourceLock(adapter.lock_path(resource))
    assert parent_lock.acquire(timeout=0)
    code = """
import re, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import platform_access as access
policy = access.AccessPolicy('child', 'child', risk_cooldown=30)
adapter = access.PlatformPolicyAdapter(policy, (), (), re.compile('risk'), Path(sys.argv[3]), Path(sys.argv[2]))
with access.session(adapter):
    print('child-entered')
"""
    env = access.environment_for(resource)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "skills/shared/scripts"),
             str(adapter.shared_lock_path), str(adapter.persistent_path)],
            env=env, capture_output=True, text=True, timeout=3, check=True)
    finally:
        parent_lock.release()
    assert "child-entered" in result.stdout


def test_web_child_environment_carries_resource_key(monkeypatch):
    import app as web

    resource = access.AccessResource("douyin")
    token = web._ACTIVE_PLATFORM_ACCESS.set(resource)
    original = {"PATH": "/bin"}
    calls = []
    monkeypatch.setattr(web, "_run_analytics_command",
                        lambda cmd, env, timeout: calls.append((env, timeout)) or "result")
    try:
        assert web._run_platform_command(["fake"], "douyin", env=original, timeout=10) == "result"
    finally:
        web._ACTIVE_PLATFORM_ACCESS.reset(token)
    assert calls[0][0][access.RESOURCE_ENV] == resource.key
    assert access.RESOURCE_ENV not in original


def test_request_pacing_serializes_access_to_shared_pace_state(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, "paced", request_delay_min=1, request_delay_max=2)
    resource = access.AccessResource("paced")
    monkeypatch.delenv(access.RESOURCE_ENV, raising=False)
    monkeypatch.setattr(access.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(access.random, "uniform", lambda _minimum, _maximum: 1)
    with access.session(adapter):
        access.request(lambda: SimpleNamespace(status_code=200, text="{}"))
    pace_path = adapter.pace_path(resource)
    assert json.loads(pace_path.read_text())["finished_at"]
    assert pace_path.with_name(f"{pace_path.name}.lock").is_file()


def test_registered_platforms_configure_adapter_specific_policies(tmp_path):
    xhs = access.get_adapter("xiaohongshu")
    douyin = access.get_adapter("douyin")
    tiktok = access.get_adapter("tiktok")
    assert {xhs.policy.concurrency, douyin.policy.concurrency, tiktok.policy.concurrency} == {1}
    assert xhs.policy.request_delay_min == douyin.policy.request_delay_min == tiktok.policy.request_delay_min == 8
    assert xhs.policy.request_delay_max == douyin.policy.request_delay_max == tiktok.policy.request_delay_max == 15
    assert xhs.policy.command_timeout == 300
    assert douyin.policy.command_timeout == tiktok.policy.command_timeout == 90
    assert xhs.state_path(access.AccessResource("xiaohongshu")).name == "xhs_access.json"
    assert douyin.matches_url("https://www.douyin.com/aweme/v1/web/aweme/post/")
    assert tiktok.matches_url("https://www.tiktok.com/api/post/item_list/")
