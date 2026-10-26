"""Offline regression tests for single-account Xiaohongshu access."""
import asyncio
import inspect
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "skills/shared/scripts"))
import app as web
import xhs_access as safety
import xhs_fetch as fetch
import xhs_sync as sync
import platform_adapters


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "BENCH_DIR", tmp_path / "bench")
    monkeypatch.setattr(web, "BENCH_ACCOUNTS_FILE", tmp_path / "bench/accounts.json")
    monkeypatch.setattr(web, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(web, "PLATFORM_ACCESS_QUEUE", web.platform_access.AsyncResourceQueue(0.001))
    adapter = web.platform_access.get_adapter("xiaohongshu")
    monkeypatch.setattr(adapter, "shared_lock_path", tmp_path / "sessions/platform.lock")
    monkeypatch.setattr(adapter, "shared_pace_path", tmp_path / "pace.json")
    monkeypatch.setattr(safety, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(safety, "LOCK_FILE", tmp_path / "slot.lock")
    monkeypatch.setattr(safety, "PACE_FILE", tmp_path / "pace.json")
    monkeypatch.setattr(safety, "_budget", 20)


def test_different_benchmarks_and_identity_share_one_slot(monkeypatch):
    events = []
    web._bench_write_accounts([{"id": identity, "platform": "xiaohongshu"}
                               for identity in ("benchA", "benchB")])

    def snapshot(identity):
        events.append(identity)
        return {"status": "ok", "platform": "xiaohongshu"}

    async def whoami(*args):
        events.append("whoami")
        return {"loggedIn": True}

    monkeypatch.setattr(web, "_bench_fetch_one", snapshot)
    monkeypatch.setattr(web, "_check_account_whoami_unlocked", whoami)

    async def scenario():
        async with web._xhs_access("existing"):
            tasks = [asyncio.create_task(web._bench_run_fetch("benchA")),
                     asyncio.create_task(web._bench_run_fetch("benchB")),
                     asyncio.create_task(web._check_account_whoami("xiaohongshu"))]
            await asyncio.sleep(0.02)
            assert not events
            status = web._xhs_access_snapshot()
            assert status["concurrency"] == 1
            assert [entry["position"] for entry in status["queue"]] == [1, 2, 3]
        await asyncio.gather(*tasks)
        assert not web.PLATFORM_ACCESS_QUEUE._queues.get("xiaohongshu:default", [])

    asyncio.run(scenario())
    assert events == ["benchA", "benchB", "whoami"]


def test_risk_discards_waiting_work_and_survives_restart():
    async def scenario():
        async with web._xhs_access("active"):
            queued = asyncio.create_task(web._xhs_access_enter("waiting"))
            await asyncio.sleep(0.01)
            web._xhs_mark_result(3, {"error": "HTTP 461"})
            with pytest.raises(web.XHSRiskError):
                await queued
        with pytest.raises(web.XHSRiskError):
            await web._xhs_access_enter("new")

    asyncio.run(scenario())
    state = json.loads(web._xhs_access_file().read_text())
    assert state["risk_until"] > state["risk_triggered_at"]
    assert not web.PLATFORM_ACCESS_QUEUE._queues.get("xiaohongshu:default", [])


def test_cancellation_keeps_slot_until_thread_finishes():
    started, finished = threading.Event(), threading.Event()

    def work():
        started.set()
        assert finished.wait(3)

    async def scenario():
        async def owner():
            async with web._xhs_access("thread"):
                await web._access_thread(work)

        task = asyncio.create_task(owner())
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        follower = asyncio.create_task(web._xhs_access_enter("next"))
        await asyncio.sleep(0.02)
        assert not follower.done()
        finished.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = await follower
        web._xhs_access_leave(entry)

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leave_stale_queue():
    async def scenario():
        async with web._xhs_access("active"):
            waiter = asyncio.create_task(web._xhs_access_enter("cancel"))
            await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with web._xhs_access("next"):
            pass
        assert not web.PLATFORM_ACCESS_QUEUE._queues.get("xiaohongshu:default", [])

    asyncio.run(scenario())


def test_repeated_detail_requests_recheck_cache_after_queueing(monkeypatch):
    web._bench_write_accounts([{"id": "bench", "platform": "xiaohongshu"}])
    web._bench_write_json(web._bench_account_dir("bench") / "works.json", {
        "works": [{"id": "note", "extra": {"xsec_token": "test-placeholder"}}]})
    calls = []

    def detail(*args, **kwargs):
        calls.append(1)
        return 0, {"likes": 1, "favorites": 2, "comments": 3}

    monkeypatch.setattr(web, "_bench_script", detail)

    async def scenario():
        async with web._xhs_access("active"):
            tasks = [asyncio.create_task(web.api_bench_work_detail("bench", "note"))
                     for number in range(2)]
            await asyncio.sleep(0.01)
        results = await asyncio.gather(*tasks)
        assert results[0] == results[1]

    asyncio.run(scenario())
    assert calls == [1]


@pytest.mark.parametrize("message", ["HTTP 429", "HTTP 461", "300012", "NeedVerify", "访问频繁"])
def test_risk_markers(message):
    assert web._xhs_result_risk(3, {"error": message})


def test_dependency_failure_is_not_risk():
    assert not web._xhs_result_risk(1, {"error": "No module named curl_cffi"})


def test_refresh_defaults_are_list_only():
    assert inspect.signature(web.api_bench_fetch).parameters["sync"].default is False
    assert inspect.signature(web._bench_run_fetch).parameters["sync"].default is False
    assert web.BENCH_DETAIL_MAX == 20
    assert web.BENCH_SYNC_MAX_DOWNLOADS == 5
    assert (fetch.STEADY_MIN, fetch.STEADY_MAX) == (8, 15)


@pytest.mark.parametrize("delay", [8, 15])
def test_script_request_pacing_and_budget(monkeypatch, delay):
    waits, calls = [], []

    def uniform(minimum, maximum):
        assert (minimum, maximum) == (8, 15)
        return delay

    monkeypatch.setattr(safety.platform_access.time, "sleep", waits.append)
    monkeypatch.setattr(safety.platform_access.time, "time", lambda: 100)
    monkeypatch.setattr(safety.platform_access.random, "uniform", uniform)
    monkeypatch.setattr(safety, "_budget", 1)

    def request():
        calls.append(1)
        return SimpleNamespace(status_code=200, text='{"success":true}')

    with safety.session():
        safety.request(request)
        with pytest.raises(safety.RequestBudgetExhausted):
            safety.request(request)
    assert len(calls) == 1
    assert waits == [delay]
    assert not safety.read_state(safety.STATE_FILE).get("risk_until")


def test_script_risk_prevents_next_request(monkeypatch):
    monkeypatch.setattr(safety.platform_access.time, "sleep", lambda delay: None)
    with safety.session():
        with pytest.raises(safety.AccessStopped):
            safety.request(lambda: SimpleNamespace(status_code=461, text="blocked"))
    with pytest.raises(safety.AccessStopped):
        with safety.session():
            pytest.fail("must not access network")


def test_risk_does_not_trigger_on_note_content():
    safety.inspect_response(SimpleNamespace(status_code=200,
        text=json.dumps({"success": True, "data": {"title": "风控分析"}})))


def test_sync_budget_caps_existing_work_detail_reads(monkeypatch, tmp_path):
    works = tmp_path / "works.json"
    works.write_text(json.dumps({"works": [{"id": f"note-{number}"} for number in range(10)]}))
    calls = []
    monkeypatch.setattr(sync.note, "parse_note", lambda identity, *args, **kwargs:
                        calls.append(identity) or {"interact": {"likes": 1}})
    monkeypatch.setattr(sync.time, "sleep", lambda delay: None)
    result = sync.sync(works, tmp_path / "notes", max_downloads=0, max_requests=2)
    assert len(calls) == 2
    assert result["skipped_due_request_budget"] == 8


def test_sync_stops_on_first_risk(monkeypatch, tmp_path):
    works = tmp_path / "works.json"
    works.write_text(json.dumps({"works": [{"id": "one"}, {"id": "two"}]}))
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("HTTP 461")

    monkeypatch.setattr(sync.note, "parse_note", blocked)
    result = sync.sync(works, tmp_path / "notes", max_downloads=0)
    assert result["risk"] and len(calls) == 1


def test_media_risk_does_not_retry_another_candidate(monkeypatch, tmp_path):
    from curl_cffi import requests

    calls, closed = [], []

    def blocked(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=461, close=lambda: closed.append(True))

    monkeypatch.setattr(requests, "get", blocked)
    with safety.session(), pytest.raises(safety.AccessStopped):
        sync.note._download(["first", "second"], tmp_path / "image.jpg", referer="")
    assert calls == ["first"]
    assert closed == [True]
    assert safety.read_state(safety.STATE_FILE)["risk_until"] > 0


def test_sync_media_risk_stops_following_works(monkeypatch, tmp_path):
    works = tmp_path / "works.json"
    works.write_text(json.dumps({"works": [{"id": "one"}, {"id": "two"}]}))
    calls = []
    monkeypatch.setattr(sync.note, "parse_note", lambda identity, *args, **kwargs:
                        calls.append(identity) or {"interact": {"likes": 4}})

    def blocked(*args, **kwargs):
        raise safety.AccessStopped("HTTP 429")

    monkeypatch.setattr(sync.note, "download_media", blocked)
    result = sync.sync(works, tmp_path / "notes", max_downloads=2)
    assert result["risk"] and calls == ["one"]
    assert json.loads(works.read_text())["works"][0]["likes"] == 4


def test_profile_risk_never_falls_back_to_guest(monkeypatch):
    monkeypatch.setattr(fetch.Throttle, "wait", lambda self: None)
    monkeypatch.setattr(fetch, "fetch_otherinfo", lambda *args: {})

    def blocked(*args, **kwargs):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(fetch, "fetch_profile_paged", blocked)
    monkeypatch.setattr(fetch, "fetch_profile_http", lambda *args: pytest.fail("must not retry as guest"))
    with pytest.raises(safety.AccessStopped):
        fetch.fetch_profile_api("user", "cookie")


def test_cli_processes_share_session_lock(tmp_path):
    code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import xhs_access as access
directory = Path(sys.argv[2])
access.LOCK_FILE = directory / 'slot.lock'
access.STATE_FILE = directory / 'state.json'
with access.session():
    with (directory / 'events').open('a') as output:
        output.write(sys.argv[3] + '-start\\n')
    time.sleep(0.1)
    with (directory / 'events').open('a') as output:
        output.write(sys.argv[3] + '-end\\n')
"""
    command = [sys.executable, "-c", code, str(ROOT / "skills/shared/scripts"), str(tmp_path)]
    processes = [subprocess.Popen(command + [identity]) for identity in ("first", "second")]
    for process in processes:
        assert process.wait(timeout=5) == 0
    lines = (tmp_path / "events").read_text().splitlines()
    assert lines in (["first-start", "first-end", "second-start", "second-end"],
                     ["second-start", "second-end", "first-start", "first-end"])
