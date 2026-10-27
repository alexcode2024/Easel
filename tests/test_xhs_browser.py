"""Browser identity consistency; only loopback traffic, never platform requests."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/shared/scripts'))
import xhs_browser as browser
import xhs_fetch as fetch
import benchmark_fetch as benchmark


@pytest.fixture
def echo_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize('kind', ['document', 'api', 'media'])
def test_real_outgoing_headers_match_pinned_profile(echo_url, kind):
    from curl_cffi import requests
    from curl_cffi.requests.impersonate import BrowserTypeLiteral

    assert browser.IMPERSONATE in get_args(BrowserTypeLiteral)
    response = requests.get(echo_url, **browser.http_options(kind), timeout=5)
    sent = response.json()
    assert sent['user-agent'] == browser.USER_AGENT
    assert f'Chrome/{browser.CHROME_MAJOR}.' in sent['user-agent']
    assert f'"Google Chrome";v="{browser.CHROME_MAJOR}"' in sent['sec-ch-ua']
    assert sent['sec-ch-ua-platform'] == '"Windows"'
    assert sent['sec-ch-ua-mobile'] == '?0'
    assert sent['accept-language'] == browser.ACCEPT_LANGUAGE
    assert 'cookie' not in sent
    assert 'sec-fetch-user' not in sent
    if kind == 'api':
        assert sent['sec-fetch-mode'] == 'cors'
        assert sent['sec-fetch-dest'] == 'empty'
        assert 'upgrade-insecure-requests' not in sent
    response.close()


def test_document_fetch_uses_profile_and_preserves_cookie(monkeypatch):
    from curl_cffi import requests
    request = Mock(return_value=object())
    monkeypatch.setattr(requests, 'get', request)
    fetch._http_get('https://www.xiaohongshu.com/explore/test', 'web_session=test')
    kwargs = request.call_args.kwargs
    for key, value in browser.http_options('document').items():
        assert kwargs[key] == value
    assert kwargs['cookies'] == {'web_session': 'test'}
    with pytest.raises(ValueError, match='配置须统一'):
        fetch._http_get('https://www.xiaohongshu.com/', impersonate='chrome131')
    assert request.call_count == 1


@pytest.mark.parametrize('paged', [False, True])
def test_api_fetch_keeps_signature_and_consistent_headers(monkeypatch, paged):
    from curl_cffi import requests
    from xhshow import Xhshow

    request = Mock(return_value=SimpleNamespace(
        status_code=200, text='{"success":true}',
        json=lambda: {'success': True, 'data': {'notes': [], 'has_more': False}}))
    monkeypatch.setattr(requests, 'get', request)
    monkeypatch.setattr(Xhshow, 'sign_headers_get', lambda *args, **kwargs: {
        'x-s': 'test-signature', 'user-agent': 'conflicting-signer-value'})
    if paged:
        fetch.fetch_profile_paged('test', 'web_session=test',
                                  throttle=SimpleNamespace(wait=lambda: None, count=1))
    else:
        fetch._signed_api_get('https://edith.xiaohongshu.com/api/test', {}, 'web_session=test')
    kwargs = request.call_args.kwargs
    assert kwargs['impersonate'] == browser.IMPERSONATE
    assert kwargs['default_headers'] is False
    assert kwargs['headers'] == {'x-s': 'test-signature'} | browser.http_options('api')['headers']
    assert kwargs['cookies'] == {'web_session': 'test'}


def test_signer_embedded_identity_matches_http_profile():
    signer = browser.signer()
    assert signer.config.PUBLIC_USERAGENT == browser.USER_AGENT
    assert signer.config.DATA_PALTFORM == 'Windows'


def test_browser_context_keeps_native_version_and_shares_locale():
    options = browser.context_options()
    assert 'user_agent' not in options
    assert 'sec-ch-ua' not in {key.lower() for key in options['extra_http_headers']}
    assert options['locale'] == 'zh-CN'
    assert options['timezone_id'] == 'Asia/Shanghai'
    assert options['extra_http_headers']['Accept-Language'] == browser.ACCEPT_LANGUAGE
    options['viewport']['width'] = 1
    assert browser.context_options()['viewport']['width'] == 1440


def test_cover_reader_uses_profile_and_closes_response(monkeypatch, tmp_path):
    from curl_cffi import requests
    response = SimpleNamespace(status_code=200, headers={'Content-Type': 'image/webp'},
                               iter_content=lambda size: iter([b'fake-image']), close=Mock())
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, 'get', request)
    name = benchmark.download_cover('https://cdn.test/image', tmp_path, 'test',
                                    referer='https://www.xiaohongshu.com/', read_response=browser.read_cover)
    assert name == 'test.webp'
    assert (tmp_path / name).read_bytes() == b'fake-image'
    for key, value in browser.http_options('media').items():
        assert request.call_args.kwargs[key] == value
    assert 'cookies' not in request.call_args.kwargs
    response.close.assert_called_once()
