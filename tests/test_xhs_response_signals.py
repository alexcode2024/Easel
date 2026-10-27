"""Offline regression coverage for XHS HTML verification signals."""
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/shared/scripts'))
import platform_access as access
import platform_adapters


@pytest.fixture
def adapter(tmp_path):
    return replace(access.get_adapter('xiaohongshu'), persistent_path=tmp_path / 'state.json')


def response(text='', status=200, url='https://www.xiaohongshu.com/explore/test', **headers):
    return SimpleNamespace(text=text, status_code=status, url=url, headers=headers)


@pytest.mark.parametrize('text', [
    '<title>正常笔记 - 小红书</title><script>window.__INITIAL_STATE__={"redOfficialVerifyType":0}</script><p>作品正文</p>',
    '<script src="/captcha.js"></script><style>.verify {color:red}</style><p>正常笔记</p>',
    '<!-- captcha NeedVerify --><div data-verify="captcha">作品正文</div>',
    '<script>const NeedVerify=false; const captcha={};</script><p>正常笔记</p>',
    '<p>redOfficialVerifyType: 0; verifyNote; captchaWidget</p>',
    json.dumps({'code': 0, 'data': {'redOfficialVerifyType': 0, 'caption': '验证码教程'}}),
])
def test_ordinary_fields_and_assets_do_not_trip_breaker(adapter, text):
    resource = access.AccessResource('xiaohongshu')
    adapter.inspect_response(response(text), resource)
    assert not adapter.state_path(resource).exists()


@pytest.mark.parametrize('text', [
    '<title>安全验证 - 小红书</title><p>请完成验证后继续访问</p>',
    '<p>请完成<span>安全验证</span></p>',
    '<noscript>请输入验证码</noscript>',
    '<p>请输入验证码</p>', '<p>IP存在风险，请切换可靠网络环境</p>',
    '<p>访问频繁</p>', '<p>请求过于频繁</p>', '<title>Captcha</title>',
    '<p>NeedVerify</p>', '<p>verify_page</p>',
    '<p>Verify your identity</p>', '<p>Too many requests</p>',
    '<p>HTTP 461</p>',
])
def test_real_page_signals_still_trip_breaker(adapter, text):
    resource = access.AccessResource('xiaohongshu')
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(response(text), resource)
    assert adapter.risk_status(resource)['active']


@pytest.mark.parametrize('status', [429, 461])
def test_http_risk_status_is_checked_before_html(adapter, status):
    with pytest.raises(access.AccessStopped, match=f'HTTP {status}'):
        adapter.inspect_response(response('<p>normal</p>', status), access.AccessResource('xiaohongshu'))


@pytest.mark.parametrize('body', [
    {'code': 300012, 'data': {}}, {'status_code': 461},
    {'code': -1, 'msg': 'NeedVerify'}, {'code': -1, 'message': 'IP存在风险'},
])
def test_structured_error_codes_and_messages_are_preserved(adapter, body):
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(response(json.dumps(body)), access.AccessResource('xiaohongshu'))


@pytest.mark.parametrize('url', [
    'https://www.xiaohongshu.com/website-login/captcha',
    'https://www.xiaohongshu.com/verify_page',
    'https://www.xiaohongshu.com/verify',
    'https://www.xiaohongshu.com/error?error_code=300012',
])
@pytest.mark.parametrize('redirect', [False, True])
def test_challenge_destinations_trip_even_with_empty_body(adapter, url, redirect):
    resp = response(location=url) if redirect else response(url=url)
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(resp, access.AccessResource('xiaohongshu'))


def test_callable_browser_response_text_is_supported(adapter):
    adapter.inspect_response(response(lambda: '<p>正常笔记</p>'), access.AccessResource('xiaohongshu'))
    with pytest.raises(access.AccessStopped):
        adapter.inspect_response(response(lambda: '<title>安全验证</title>'), access.AccessResource('xiaohongshu'))
