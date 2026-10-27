"""Batch refresh stops only the affected platform, without launching skipped tasks."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
sys.path.insert(0, str(ROOT / 'skills/shared/scripts'))
import app as web


@pytest.mark.parametrize('already_paused', [False, True])
def test_batch_skips_paused_platform_and_continues_other_platforms(monkeypatch, tmp_path, already_paused):
    monkeypatch.setattr(web, 'BENCH_DIR', tmp_path)
    monkeypatch.setattr(web, 'BENCH_ALL_TASK', None)
    monkeypatch.setattr(web, 'BENCH_RUNNING', {})
    monkeypatch.setattr(web, 'BENCH_LAST_START', {})
    accounts = [
        {'id': 'xhs1', 'platform': 'xiaohongshu'},
        {'id': 'dy01', 'platform': 'douyin'},
        {'id': 'xhs2', 'platform': 'xiaohongshu'},
        {'id': 'dy02', 'platform': 'douyin'},
        {'id': 'xhs3', 'platform': 'xiaohongshu'},
    ]
    monkeypatch.setattr(web, '_bench_read_accounts', lambda: accounts)
    monkeypatch.setattr(web, '_bench_platform_meta', lambda _: {'auto': True})
    paused = {'xiaohongshu'} if already_paused else set()
    monkeypatch.setattr(web, '_platform_access_risk_status',
                        lambda platform: {'active': platform in paused})
    calls = []

    async def fetch(bid):
        calls.append(bid)
        try:
            if bid == 'xhs1':
                paused.add('xiaohongshu')
                web._bench_write_status(bid, 'risk', 'HTTP 461')
            else:
                # All remaining affected accounts are marked before any other platform runs.
                assert web._bench_read_status('xhs2')['state'] == 'skipped'
                assert web._bench_read_status('xhs3')['state'] == 'skipped'
                web._bench_write_status(bid, 'done', '完成')
        finally:
            web.BENCH_RUNNING.pop(bid, None)

    async def no_delay(_):
        pass

    monkeypatch.setattr(web, '_bench_run_fetch', fetch)
    monkeypatch.setattr(web.asyncio, 'sleep', no_delay)

    async def scenario():
        assert (await web.api_bench_fetch_all())['total'] == 5
        await web.BENCH_ALL_TASK
        status = await web.api_bench_fetch_all_status()
        assert status['running'] is False
        assert status['done'] == status['total'] == 5
        assert status['states']['xhs1'] == ('skipped' if already_paused else 'risk')
        assert status['states']['xhs2'] == status['states']['xhs3'] == 'skipped'
        assert status['states']['dy01'] == status['states']['dy02'] == 'done'

    asyncio.run(scenario())
    assert calls == (['dy01', 'dy02'] if already_paused else ['xhs1', 'dy01', 'dy02'])
    for bid in ('xhs2', 'xhs3'):
        assert web._bench_read_status(bid)['message'] == '因平台暂停而跳过'
        assert bid not in web.BENCH_LAST_START
    assert not web.BENCH_RUNNING


def test_ordinary_fetch_failure_does_not_stop_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(web, 'BENCH_DIR', tmp_path)
    monkeypatch.setattr(web, 'BENCH_ALL_TASK', None)
    monkeypatch.setattr(web, 'BENCH_RUNNING', {})
    monkeypatch.setattr(web, 'BENCH_LAST_START', {})
    monkeypatch.setattr(web, '_bench_read_accounts', lambda: [
        {'id': 'one1', 'platform': 'xiaohongshu'}, {'id': 'two2', 'platform': 'xiaohongshu'}])
    monkeypatch.setattr(web, '_bench_platform_meta', lambda _: {'auto': True})
    monkeypatch.setattr(web, '_platform_access_risk_status', lambda _: {'active': False})
    calls = []

    async def fetch(bid):
        calls.append(bid)
        web._bench_write_status(bid, 'error' if bid == 'one1' else 'done')
        web.BENCH_RUNNING.pop(bid, None)

    async def no_delay(_):
        pass

    monkeypatch.setattr(web, '_bench_run_fetch', fetch)
    monkeypatch.setattr(web.asyncio, 'sleep', no_delay)

    async def scenario():
        await web.api_bench_fetch_all()
        await web.BENCH_ALL_TASK

    asyncio.run(scenario())
    assert calls == ['one1', 'two2']
