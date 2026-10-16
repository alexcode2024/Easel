import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "shared" / "scripts"))
import xhs_publish


def html_state(user):
    return '<script>window.__INITIAL_STATE__=' + json.dumps({
        "user": user, "feed": {"nickname": "unrelated author", "images": "https://feed.test/avatar"},
    }) + ";</script>"


def test_initial_state_reads_own_identity_only():
    account = xhs_publish._web_account_html(html_state({
        "loggedIn": True,
        "userInfo": {"userId": "self-id", "nickname": "My account", "images": "https://self.test/avatar"},
    }))
    assert xhs_publish._account_identity(account) == {
        "name": "My account", "avatar": "https://self.test/avatar",
    }


@pytest.mark.parametrize("user", [
    None, [], "bad", {}, {"loggedIn": False, "userInfo": {"userId": "id"}},
    {"loggedIn": True, "userInfo": {"userId": ""}},
    {"loggedIn": True, "userInfo": {"userId": ["id"]}},
    {"loggedIn": 1, "userInfo": {"userId": "id"}},
])
def test_anon_or_malformed_self_state_never_returns_feed_author(user):
    assert xhs_publish._web_account_html(html_state(user)) is None


def test_undefined_replacement_does_not_modify_quoted_nickname():
    html = html_state({"loggedIn": True, "userInfo": {
        "userId": "id", "nickname": "undefined person", "images": None,
    }}).replace('"images": null', '"images": undefined')
    assert xhs_publish._web_account_html(html)["userName"] == "undefined person"


@pytest.mark.parametrize("html", [
    "<script>window.__INITIAL_STATE__=alert('do not eval')</script>",
    "<script>window.__INITIAL_STATE__=[]</script>",
    "<script>window.__INITIAL_STATE__=null</script>",
    "<div>no state</div>", "x" * 2_000_001,
], ids=["executable-state", "array-state", "null-state", "missing-state", "oversized-state"])
def test_invalid_initial_state_is_ignored_without_execution(html):
    assert xhs_publish._web_account_html(html) is None


def test_complete_web_identity_does_not_wait_for_creator(monkeypatch):
    account = {"userId": "self", "userName": "My account", "userAvatar": "https://self.test/avatar"}
    monkeypatch.setattr(xhs_publish, "_web_account", lambda context: account)

    def unnecessary_request(context):
        raise AssertionError("Complete identity should return immediately")

    monkeypatch.setattr(xhs_publish, "_creator_account", unnecessary_request)
    assert xhs_publish._authenticated_account(object()) == account


def test_incomplete_web_identity_is_enriched_from_same_account(monkeypatch):
    monkeypatch.setattr(xhs_publish, "_web_account", lambda context: {
        "userId": "self", "userName": "My account", "userAvatar": "",
    })
    monkeypatch.setattr(xhs_publish, "_creator_account", lambda context: {
        "userId": "self", "userName": "Creator name", "userAvatar": "https://self.test/avatar",
    })
    assert xhs_publish._account_identity(xhs_publish._authenticated_account(object())) == {
        "name": "My account", "avatar": "https://self.test/avatar",
    }


def test_identity_is_never_merged_across_accounts(monkeypatch):
    account = {"userId": "self", "userName": "", "userAvatar": ""}
    monkeypatch.setattr(xhs_publish, "_web_account", lambda context: account)
    monkeypatch.setattr(xhs_publish, "_creator_account", lambda context: {
        "userId": "other", "userName": "Other account", "userAvatar": "https://other.test/avatar",
    })
    assert xhs_publish._authenticated_account(object()) == account


def test_creator_identity_is_used_when_web_identity_is_unavailable(monkeypatch):
    account = {"userId": "self", "userName": "My account", "userAvatar": "https://self.test/avatar"}
    monkeypatch.setattr(xhs_publish, "_web_account", lambda context: None)
    monkeypatch.setattr(xhs_publish, "_creator_account", lambda context: account)
    assert xhs_publish._authenticated_account(object()) == account


@pytest.mark.parametrize("wrapper", ["plain", "value", "_value"])
def test_profile_basic_info_reads_current_self_and_prefers_large_avatar(wrapper):
    profile = {"basicInfo": {"nickname": "My actual nickname",
                             "imageb": "//self.test/large.png",
                             "images": "https://self.test/small.png"}}
    state = {"user": {"loggedIn": True, "userInfo": {"userId": "self-id"},
                      "userPageData": profile if wrapper == "plain" else {wrapper: profile}},
             "feed": {"nickname": "Unrelated author", "images": "https://other.test/avatar"}}
    assert xhs_publish._self_profile_identity(state, "self-id") == {
        "name": "My actual nickname", "avatar": "https://self.test/large.png",
    }


@pytest.mark.parametrize("own_id,profile_id,logged_in", [
    ("other", "self", True), ("self", "other", True), ("self", "self", False),
])
def test_profile_basic_info_rejects_other_users_and_anonymous_state(own_id, profile_id, logged_in):
    state = {"user": {"loggedIn": logged_in, "userInfo": {"userId": own_id},
                      "userPageData": {"userId": profile_id, "basicInfo": {
                          "nickname": "Not me", "images": "https://other.test/avatar"}}}}
    assert xhs_publish._self_profile_identity(state, "self") == {"name": "", "avatar": ""}


def test_vue_wrapped_authenticated_state_and_alternate_fields_are_supported():
    state = {"user": {"loggedIn": {"value": True}, "userInfo": {"_value": {
        "userId": "self", "nickname": "My nickname", "avatar": "//self.test/avatar"}}}}
    assert xhs_publish._account_identity(xhs_publish._state_account(state)) == {
        "name": "My nickname", "avatar": "https://self.test/avatar",
    }


@pytest.mark.parametrize("avatar", ["https://[broken", "https://user:secret@self.test/avatar",
                                   "https://self.test/\nimage", "javascript:alert(1)"])
def test_invalid_avatar_does_not_discard_valid_name_or_next_avatar(avatar):
    assert xhs_publish._account_identity({
        "nickname": "Me", "imageb": avatar, "images": "https://self.test/valid",
    }) == {"name": "Me", "avatar": "https://self.test/valid"}


def test_profile_request_uses_verified_id_and_validates_response_url():
    state = {"user": {"loggedIn": True, "userInfo": {"userId": "self"},
                      "userPageData": {"basicInfo": {"nickname": "Me", "imageb": "https://self.test/avatar"}}}}
    account = {"userId": "self", "userName": "", "userAvatar": ""}
    response = SimpleNamespace(status=200, url="https://www.xiaohongshu.com/user/profile/self",
                               text=lambda: '<script>window.__INITIAL_STATE__=' + json.dumps(state) + '</script>')
    context = SimpleNamespace(request=SimpleNamespace(get=Mock(return_value=response)))
    result = xhs_publish._self_profile_account(context, account)
    assert xhs_publish._account_identity(result) == {"name": "Me", "avatar": "https://self.test/avatar"}
    context.request.get.assert_called_once_with(
        response.url, headers={"Referer": xhs_publish.EXPLORE_URL}, timeout=4000)
    response.url = "https://www.xiaohongshu.com/user/profile/other"
    assert xhs_publish._self_profile_account(context, account) == account


def test_profile_canonical_fields_override_stale_login_state_fields():
    assert xhs_publish._account_identity({
        "userName": "Current name", "nickname": "Old name",
        "userAvatar": "https://self.test/current", "images": "https://self.test/old",
    }) == {"name": "Current name", "avatar": "https://self.test/current"}


def test_creator_account_request_includes_required_first_party_referer():
    account = {"userId": "self", "userName": "Me", "userAvatar": "https://self.test/avatar"}
    response = SimpleNamespace(status=200, json=lambda: {"success": True, "data": account})
    request = Mock(return_value=response)
    context = SimpleNamespace(request=SimpleNamespace(get=request))
    assert xhs_publish._creator_account(context) == account
    request.assert_called_once_with(
        xhs_publish.CREATOR_ACCOUNT_URL, headers={"Referer": xhs_publish.CREATOR_HOME_URL}, timeout=5000)


@pytest.mark.parametrize("complete", [False, True])
def test_whoami_enriches_incomplete_identity_without_losing_verified_login(monkeypatch, capsys, complete):
    account = {"userId": "self", "userName": "Me",
               "userAvatar": "https://self.test/avatar" if complete else ""}
    page = object()
    context = SimpleNamespace(pages=[page], close=Mock())
    playwright = Mock()
    playwright.__enter__ = Mock(return_value=object())
    playwright.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: playwright)
    monkeypatch.setattr(xhs_publish, "_ProfileLock", lambda profile: SimpleNamespace(acquire=Mock(), release=Mock()))
    monkeypatch.setattr(xhs_publish, "_launch", lambda *args, **kwargs: context)
    monkeypatch.setattr(xhs_publish, "_authenticated_account", lambda context: account)
    profile = Mock(return_value=account)
    dom = Mock(return_value={"name": "Do not replace name", "avatar": "https://self.test/avatar"})
    monkeypatch.setattr(xhs_publish, "_self_profile_account", profile)
    monkeypatch.setattr(xhs_publish, "_page_account_identity", dom)
    args = SimpleNamespace(profile_base=None, proxy=None, no_proxy=True)
    assert xhs_publish.cmd_whoami(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "loggedIn": True, "name": "Me", "avatar": "https://self.test/avatar",
    }
    assert profile.call_count == (0 if complete else 1)
    assert dom.call_count == (0 if complete else 1)
    context.close.assert_called_once()


# --------------------------------------------------------------------------- #
# 登录短路：创作平台会话有效 ≠ 主站会话有效（2026-10-16 实测踩坑）
# --------------------------------------------------------------------------- #
class _ReachedQR(Exception):
    """哨兵：证明流程走到了「取主站登录二维码」这一步。"""


LOGIN_URL = "https://www.xiaohongshu.com/login?redirectPath=%2Fexplore"
EXPLORE_URL = "https://www.xiaohongshu.com/explore"


class _FakeLoginPage:
    """假页面：goto 首页时按「主站是否已登录」决定停在 /login 还是信息流。

    need_web 现在由**导航结果**判定（首页停在 /login = 游客态），假页面必须如实反映，
    否则测试就没在测真实契约。
    """

    def __init__(self, web_logged_in: bool):
        self.web_logged_in = web_logged_in
        self.url = "about:blank"

    def goto(self, url, **kwargs):
        if url == EXPLORE_URL:
            self.url = EXPLORE_URL if self.web_logged_in else LOGIN_URL
        else:
            self.url = url
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def content(self):
        return ""


class _FakeLoginContext:
    def __init__(self, web_logged_in: bool):
        self._page = _FakeLoginPage(web_logged_in)
        self.pages: list = []

    def new_page(self):
        return self._page

    def close(self):
        return None


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoopLock:
    def __init__(self, *a, **k):
        pass

    def acquire(self, timeout_s):
        return None

    def release(self):
        return None


def _login_args(timeout=1):
    return SimpleNamespace(qr_out=None, timeout=timeout, status_file=None, profile_base=None,
                           headed=False, proxy=None, no_proxy=True)


def _patch_login(monkeypatch, *, web_logged_in, on_qr, scanned=False,
                 page_web=False, creator=None):
    """打桩整套登录环境。

    web_logged_in: 首页导航是否落在信息流（False = 游客态 → need_web=True）
    page_web:      轮询里「从页面读出的主站会话」（False/True/None，或 callable）
    creator:       创作平台会话（默认一直有效；可传 callable 模拟先无后有）
    """
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(xhs_publish, "_launch",
                        lambda *a, **k: _FakeLoginContext(web_logged_in))
    monkeypatch.setattr(xhs_publish, "_ProfileLock", _NoopLock)
    monkeypatch.setattr(xhs_publish, "_creator_account",
                        creator if callable(creator) else (lambda ctx: {"userId": "self"}))
    monkeypatch.setattr(xhs_publish, "_page_web_session",
                        page_web if callable(page_web) else (lambda page: page_web))
    # 兜底探针：只在页面读不出身份时才会被问到
    monkeypatch.setattr(xhs_publish, "_web_session_state", lambda ctx: False)
    monkeypatch.setattr(xhs_publish, "_login_error", lambda page: "")
    monkeypatch.setattr(xhs_publish, "_is_logged_in", lambda page: False)
    monkeypatch.setattr(xhs_publish, "_qr_state", lambda page: ("scanned" if scanned else ""))
    monkeypatch.setattr(xhs_publish, "_capture_login_qr", on_qr)


def test_login_continues_to_qr_when_main_site_is_guest(monkeypatch):
    """创作平台已登录但主站是游客态：必须继续取主站二维码。

    回归的是这个坑：以前只看创作平台会话就直接 return「已登录」，
    主站 web_session 永远修不好 —— 而对标账号读的正是主站公开页，
    游客态下粉丝只会拿到「1万+」占位值。
    """
    reached: dict = {}

    def on_qr(page, qr_out, need_web=False):
        reached["qr"] = True
        reached["need_web"] = need_web
        raise _ReachedQR()          # 走到这里就够了，后面的轮询不再跑

    _patch_login(monkeypatch, web_logged_in=False, on_qr=on_qr)
    xhs_publish.cmd_login(_login_args())     # 外层会兜住异常并写失败状态，不影响断言

    assert reached.get("qr") is True, "主站游客态时必须继续取二维码"
    assert reached.get("need_web") is True, "主站会话未确认时必须只取主站码"


def test_login_short_circuits_when_both_sessions_alive(monkeypatch):
    """两个会话都有效时不弹二维码（不打扰已登录的用户）。"""
    def on_qr(page, qr_out, need_web=False):  # pragma: no cover - 不该被调用
        raise AssertionError("两个会话都有效时不该去取二维码")

    _patch_login(monkeypatch, web_logged_in=True, page_web=True, on_qr=on_qr)
    assert xhs_publish.cmd_login(_login_args()) == 0


def test_login_success_requires_web_session_when_web_is_guest(monkeypatch):
    """创作平台一直是好的：不能在主站会话还没建立时就宣布「登录成功」并关掉浏览器。

    回归的是这个坑：扫码后的成功判定去问创作平台（它一直是好的）→ 秒判成功 → 关浏览器
    → 主站登录走不完，只留下一个游客 web_session，粉丝数永远是占位值。
    """
    _patch_login(monkeypatch, web_logged_in=False, page_web=False,
                 on_qr=lambda *a, **k: True, scanned=True)
    rc = xhs_publish.cmd_login(_login_args(timeout=8))

    assert rc == 1, "主站会话没建立时不得报成功"


def test_login_succeeds_once_web_session_appears(monkeypatch):
    """主站会话一建立就应立刻报成功（不必干等到超时）。"""
    calls = {"n": 0}

    def page_web(page):
        calls["n"] += 1
        return calls["n"] > 1        # 第一轮还是游客，之后已登录

    _patch_login(monkeypatch, web_logged_in=False, page_web=page_web,
                 on_qr=lambda *a, **k: True, scanned=True)
    assert xhs_publish.cmd_login(_login_args(timeout=15)) == 0


def test_creator_only_login_still_works_when_web_is_fine(monkeypatch):
    """主站会话正常、只缺创作平台时：仍走扫码，并以创作平台会话判定成功。"""
    reached: dict = {}
    calls = {"n": 0}

    def creator(ctx):
        calls["n"] += 1
        return None if calls["n"] <= 1 else {"userId": "self"}   # 首检无、扫码后有

    def on_qr(page, qr_out, need_web=False):
        reached["qr"] = True
        return True

    _patch_login(monkeypatch, web_logged_in=True, page_web=True, on_qr=on_qr,
                 creator=creator, scanned=True)          # 扫码后页面进入「已扫描」态，触发会话复核
    rc = xhs_publish.cmd_login(_login_args(timeout=15))

    assert reached.get("qr") is True, "缺创作平台会话时应弹码"
    assert rc == 0, "扫码后应报成功"


# --------------------------------------------------------------------------- #
# cookie 校验（纯 HTTP，不起浏览器）——「自有账号」页的会话健康依赖它
# --------------------------------------------------------------------------- #
def _write_cookie(tmp_path, value: str) -> str:
    fp = tmp_path / "xhs.cookie"
    fp.write_text(value, encoding="utf-8")
    return str(fp)


def test_cookie_whoami_without_web_session_reports_both_sessions(tmp_path, capsys):
    """cookie 里没有 web_session：判定未登录，并说明创作平台会话在不在（不联网）。"""
    rc = xhs_publish._whoami_via_cookie(_write_cookie(tmp_path, "a1=abc; webId=xyz"))

    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and data["loggedIn"] is False
    assert data["sessions"]["web"]["ok"] is False
    assert "web_session" in data["sessions"]["web"]["detail"]
    assert data["sessions"]["creator"]["cookie_present"] is False


def test_cookie_whoami_detects_creator_session(tmp_path, capsys):
    """带上创作平台 cookie 时要能认出来（发布/后台用），全程不需要浏览器。"""
    path = _write_cookie(
        tmp_path, "web_session=; galaxy_creator_session_id=abc; customer-sso-sid=def")
    xhs_publish._whoami_via_cookie(path)

    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["sessions"]["creator"]["cookie_present"] is True


def test_cookie_whoami_missing_file_is_reported(tmp_path, capsys):
    """cookie 文件读不到时如实报错，而不是抛异常（whoami 必须永远输出 JSON）。"""
    rc = xhs_publish._whoami_via_cookie(str(tmp_path / "nope.cookie"))

    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and data["loggedIn"] is False and "cookie 文件读不到" in data["error"]


def test_self_user_id_from_cookie():
    """从 cookie 里的 x-user-id 推断当前用户（用于读自己的主页确认会话）。"""
    assert xhs_publish._whoami_self_user_id(
        "a1=x; x-user-id-creator.xiaohongshu.com=55753cf4f5a2633511d5a652"
    ) == "55753cf4f5a2633511d5a652"
    assert xhs_publish._whoami_self_user_id("a1=x; web_session=y") == ""
