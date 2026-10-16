"""对标账号模块：平台适配器纯函数 + Web 接口与落盘行为。

不联网、不起浏览器：抓取部分用假的 ``_bench_script`` 返回值驱动，
真实抓取路径由 skills/shared/scripts/benchmark_fetch.py 的 selftest 与手工实测覆盖。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "skills" / "shared" / "scripts"))

import app as web  # noqa: E402
import benchmark_fetch as bf  # noqa: E402
import xhs_fetch as xhs  # noqa: E402


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [
    ("1.3万", (13000, False)),
    ("1万+", (10000, True)),
    ("32.0万", (320000, False)),
    ("1.2亿", (120000000, False)),
    ("1,234", (1234, False)),
    (143, (143, False)),
    ("", (None, False)),
    (None, (None, False)),
    ("暂无", (None, False)),
])
def test_parse_count(raw, want):
    assert bf.parse_count(raw) == want


def test_parse_count_marks_lower_bound():
    """"1万+" 只是量级下界，前端据此原样展示而不是伪精确数字。"""
    num, lower = bf.parse_count("1万+")
    assert (num, lower) == (10000, True)


@pytest.mark.parametrize("raw,want", [
    ("https://www.xiaohongshu.com/user/profile/55753cf4f5a2633511d5a652", "55753cf4f5a2633511d5a652"),
    ("https://www.xiaohongshu.com/user/profile/55753CF4F5A2633511D5A652?x=1", "55753cf4f5a2633511d5a652"),
    ("55753cf4f5a2633511d5a652", "55753cf4f5a2633511d5a652"),
])
def test_parse_target_xhs_ok(raw, want):
    assert bf.parse_target_xhs(raw) == want


@pytest.mark.parametrize("raw", ["", "xiaohongshu.com/user/profile/abc", "12345"])
def test_parse_target_xhs_rejects(raw):
    with pytest.raises(ValueError):
        bf.parse_target_xhs(raw)


def test_parse_target_douyin():
    assert bf.parse_target_douyin("https://www.douyin.com/user/MS4wLjABAAAAxyz?tab=post") == "MS4wLjABAAAAxyz"
    assert bf.parse_target_douyin("MS4wLjABAAAAxyz") == "MS4wLjABAAAAxyz"
    # 只给「抖音号」无法离线定位账号：必须明确报错，不能猜
    with pytest.raises(ValueError):
        bf.parse_target_douyin("dy123456")


def test_median_and_burst():
    works = [bf.Work(id=str(i), likes=v) for i, v in enumerate([100, 110, 90, 400])]
    assert bf.mark_burst(works) == 105
    assert [w.is_burst for w in works] == [False, False, False, True]

    # 样本不足 3 条时不标记（中位数不可靠）
    thin = [bf.Work(id="a", likes=10), bf.Work(id="b", likes=1000)]
    bf.mark_burst(thin)
    assert [w.is_burst for w in thin] == [False, False]

    assert bf.median([]) is None
    assert bf.median([None, None]) is None


def test_cover_stem_is_stable_and_collision_free():
    """作品 ID 为空是常态：封面文件名不能依赖标题（会互相覆盖）。"""
    a = ("https://sns-webpic-qc.xhscdn.com/202610160113/deadbeef/1040g2sg31mqkknat5ie040pi8tuf99iiunq37qg"
         "!nc_n_nwebp_prv_1")
    b = ("https://sns-webpic-qc.xhscdn.com/202610160113/deadbeef/1040g2sg31mqkknat5ie040pi8tuf99iiunq37qh"
         "!nc_n_nwebp_dft_1")
    assert bf.cover_stem(a, "", "同样标题") == "1040g2sg31mqkknat5ie040pi8tuf99iiunq37qg"
    assert bf.cover_stem(a, "", "同样标题") != bf.cover_stem(b, "", "同样标题")
    # 取不到 fileId 时按内容哈希，仍不冲突
    x = bf.cover_stem("https://cdn.example.com/", "", "标题A", 111)
    y = bf.cover_stem("https://cdn.example.com/", "", "标题B", 222)
    assert x and y and x != y


def test_epoch_ms_normalizes():
    assert bf._epoch_ms(1760000000) == 1760000000000
    assert bf._epoch_ms(1760000000000) == 1760000000000
    assert bf._epoch_ms(None) is None
    assert bf._epoch_ms(0) is None


def test_adapter_registry_declares_honest_capabilities():
    plats = {p["platform"]: p for p in (a.describe() for a in bf.ADAPTERS.values())}
    # 三个平台都已实测通过（小红书/抖音 2026-10、TikTok 2026-10），不再是未实测路径
    for key in ("xiaohongshu", "douyin", "tiktok"):
        assert plats[key]["auto"] is True
        assert plats[key]["experimental"] is False
    # 小红书列表里只有点赞：收藏/评论要按需逐条打开作品页读（登录态下主页会给作品 ID）
    assert plats["xiaohongshu"]["fields"]["work_likes"] is True
    assert plats["xiaohongshu"]["fields"]["work_favorites"] is True
    assert plats["xiaohongshu"]["fields"]["work_comments"] is True
    assert plats["xiaohongshu"]["fields"]["work_detail_manual"] is True
    assert plats["douyin"]["fields"]["work_favorites"] is True
    assert plats["tiktok"]["fields"]["work_favorites"] is True
    assert plats["tiktok"]["fields"]["work_plays"] is True
    # 抖音/TikTok 的收藏/评论随列表返回，不该被逐条深挖
    assert not plats["douyin"]["fields"].get("work_detail_manual")
    assert not plats["tiktok"]["fields"].get("work_detail_manual")
    # 抖音播放量不公开 → 不该声明有
    assert plats["douyin"]["fields"].get("work_plays", False) is False
    # 其余平台留了接口但明确标记为不支持自动抓取
    for key in ("kuaishou", "zhihu", "bilibili", "weixin-channels", "wechat-oa"):
        assert plats[key]["auto"] is False
        assert plats[key]["note"]


def test_douyin_zero_play_count_is_unknown_not_zero():
    """抖音接口对他人作品恒返回 play_count=0：显示 0 等于编造「没人看」，必须是未知。"""
    assert bf._public_plays(0) is None
    assert bf._public_plays(None) is None
    assert bf._public_plays("0") is None
    assert bf._public_plays(12345) == 12345


# --------------------------------------------------------------------------- #
# 小红书：精确字段 vs 游客态骨架值（2026-10-16 实测踩坑）
# --------------------------------------------------------------------------- #
def _xhs_payload(**over):
    """按 xhs_fetch.py 的真实输出结构造一份账号数据。"""
    prof = {
        "nickname": "大厂升升🎬", "red_id": "BunnyIP", "desc": "搞钱访谈",
        "fans_raw": "1万+", "fans_num": 10000,
        "interaction_raw": "1万+", "interaction_num": 10000,
        "liked_exact": 320299, "collected_exact": 218662, "posted_exact": 292,
        "guest": False, "logged_in_per_ssr": True, "notes_in_ssr": 0,
    }
    prof.update(over)
    return {"platform": "xiaohongshu", "mode": "logged_in", "profile": prof,
            "notes": [{"note_id": "", "title": "标题", "liked_count": "1.3万",
                       "liked_count_num": 13000, "time_ms": 1758719093000,
                       "cover": "", "xsec_token": "T", "sticky": False}],
            "request_log": [], "throttle": "10-30s", "requests_made": 2}


def _fetch_xhs(monkeypatch, payload):
    """跑一次小红书适配器（把 xhs_fetch 子进程换成假返回值）。"""
    ad = bf.get_adapter("xiaohongshu")

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(bf.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(bf.Path, "is_file", lambda self: True)
    monkeypatch.setattr(bf.Path, "read_text", lambda self, **k: json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(bf.Path, "unlink", lambda self, **k: None)
    return ad.fetch("55753cf4f5a2633511d5a652", limit=12, profile_root=Path("/tmp"),
                    proxy=None, cover_dir=None, verbose=False)


def test_xhs_uses_exact_liked_and_collected(monkeypatch):
    """SSR 有精确整数字段：累计获赞/收藏必须用它，而不是「获赞与收藏」展示串。"""
    snap = _fetch_xhs(monkeypatch, _xhs_payload())
    assert snap.likes.num == 320299
    assert snap.favorites.num == 218662        # 以前这里恒为 None（误判平台不公开）
    assert snap.posts == 292                   # 作品数以前也拿不到
    assert snap.followers.num == 10000
    assert snap.logged_in is True
    assert not any("游客态" in w for w in snap.warnings)


def test_xhs_guest_mode_blanks_placeholder_values(monkeypatch):
    """游客态下粉丝/获赞展示串都是 "1万+" 骨架值（真实 53.9 万也显示 1万+）：
    粉丝必须留空并告警，精确字段照常可用。"""
    snap = _fetch_xhs(monkeypatch, _xhs_payload(
        guest=True, logged_in_per_ssr=False,
        fans_raw="1万+", interaction_raw="1万+"))

    assert snap.logged_in is False
    assert snap.followers is None                       # 不再显示不可信的「1万+」
    assert snap.likes.num == 320299                     # 精确值不受游客态影响
    assert snap.favorites.num == 218662
    assert any("游客态" in w for w in snap.warnings)


def test_xhs_guest_detected_from_logged_in_flag(monkeypatch):
    """即使 userInfo.guest 缺失，SSR 的 loggedIn=false 也要判为游客态。"""
    snap = _fetch_xhs(monkeypatch, _xhs_payload(guest=False, logged_in_per_ssr=False))
    assert snap.logged_in is False
    assert snap.followers is None


def test_xhs_falls_back_to_display_string_without_exact_fields(monkeypatch):
    """老数据/异常结构里没有精确字段时，退回展示串（不因为缺字段就丢数据）。"""
    snap = _fetch_xhs(monkeypatch, _xhs_payload(
        liked_exact=None, collected_exact=None, posted_exact=None))
    assert snap.likes.num == 10000      # interaction_raw 兜底
    assert snap.favorites is None
    assert snap.posts is None


def test_platforms_sorted_by_product_priority():
    """产品优先级：小红书 → 抖音 → TikTok → 其余（新增平台不该打乱这个顺序）。"""
    keys = [a.key for a in bf.all_adapters()]
    assert keys[:3] == ["xiaohongshu", "douyin", "tiktok"]
    assert [bf.get_adapter(k).order for k in keys[:3]] == [0, 1, 2]
    assert bf.PLATFORM_ORDER == ("xiaohongshu", "douyin", "tiktok")
    assert set(keys[3:]) == {"kuaishou", "zhihu", "bilibili", "weixin-channels", "wechat-oa"}


def test_register_rejects_duplicate_platform():
    with pytest.raises(ValueError):
        bf.register(bf.XiaohongshuAdapter())


def test_unregistered_platform_rejected():
    with pytest.raises(ValueError):
        bf.get_adapter("myspace")


# --------------------------------------------------------------------------- #
# TikTok（无海外网络，解析用合成 fixture 验证；结构取自页面脚本 id 与字段名）
# --------------------------------------------------------------------------- #
TIKTOK_HTML = """<!doctype html><html><head><title>x</title></head><body>
<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
{"__DEFAULT_SCOPE__": {"webapp.user-detail": {
  "userInfo": {
    "user": {"id": "123", "uniqueId": "somecreator", "nickname": "Some Creator",
             "signature": "AI 工具测评", "avatarLarger": {"urlList": ["https://p16.example/a.jpeg"]}},
    "stats": {"followerCount": 1250000, "followingCount": 88, "heartCount": 9800000,
              "videoCount": 214, "diggCount": 41}
  },
  "itemList": [
    {"id": "7412345678901234567", "desc": "3 AI tools you missed", "createTime": 1730000000,
     "stats": {"diggCount": 120000, "shareCount": 900, "commentCount": 3400,
               "playCount": 2400000, "collectCount": 8800},
     "video": {"cover": {"urlList": ["https://p16.example/cover1.jpeg"]}}},
    {"id": "7412345678901234568", "desc": "", "createTime": 1730100000,
     "stats": {"diggCount": 300, "commentCount": 12, "playCount": 9000, "collectCount": 5},
     "video": {"originCover": {"urlList": ["https://p16.example/cover2.jpeg"]}}}
  ]}}}
</script></body></html>"""


def test_parse_target_tiktok():
    assert bf.parse_target_tiktok("https://www.tiktok.com/@somecreator") == "somecreator"
    assert bf.parse_target_tiktok("https://www.tiktok.com/@somecreator?lang=en") == "somecreator"
    assert bf.parse_target_tiktok("@somecreator") == "somecreator"
    assert bf.parse_target_tiktok("somecreator") == "somecreator"
    for bad in ("", "https://www.tiktok.com/", "有中文的名"):
        with pytest.raises(ValueError):
            bf.parse_target_tiktok(bad)


def test_parse_tiktok_ssr_synthetic():
    user_info, items = bf.parse_tiktok_ssr(TIKTOK_HTML)
    assert user_info["user"]["uniqueId"] == "somecreator"
    assert user_info["stats"]["heartCount"] == 9800000
    assert len(items) == 2
    assert items[0]["id"] == "7412345678901234567"


@pytest.mark.parametrize("html", [
    "",
    "<html><body>没有 SSR 脚本</body></html>",
    '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{坏 JSON</script>',
    '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{"__DEFAULT_SCOPE__":{}}</script>',
])
def test_parse_tiktok_ssr_never_guesses(html):
    """取不到就返回空——调用方据此如实降级，不猜数据。"""
    assert bf.parse_tiktok_ssr(html) == ({}, [])


def test_tiktok_snapshot_maps_fields():
    user_info, items = bf.parse_tiktok_ssr(TIKTOK_HTML)
    snap = bf.snapshot_from_tiktok("somecreator", user_info, items, "TikTok 主页 SSR")

    assert snap.platform == "tiktok"
    assert snap.nickname == "Some Creator"
    assert snap.account_id == "somecreator"
    assert snap.home_url == "https://www.tiktok.com/@somecreator"
    assert snap.followers == bf.Metric(raw="1250000", num=1250000, lower_bound=False)
    assert snap.likes.num == 9800000                 # heartCount = 累计获赞
    assert snap.posts == 214
    # 账号级收藏/评论不是公开数据 → 必须是 None，不能拿作品级求和冒充
    assert snap.favorites is None and snap.comments is None

    first = snap.works[0]
    assert first.id == "7412345678901234567"
    assert first.url == "https://www.tiktok.com/@somecreator/video/7412345678901234567"
    assert first.likes == 120000 and first.favorites == 8800
    assert first.comments == 3400 and first.plays == 2400000
    assert first.published_at_ms == 1730000000000    # 秒 → 毫秒
    assert first.cover_url == "https://p16.example/cover1.jpeg"
    # 空标题兜底 + originCover 回退
    assert snap.works[1].title == "(无标题)"
    assert snap.works[1].cover_url == "https://p16.example/cover2.jpeg"


def test_tiktok_snapshot_survives_empty_payload():
    snap = bf.snapshot_from_tiktok("ghost", {}, [], "TikTok 主页")
    assert snap.works == []
    assert snap.followers is None and snap.likes is None
    assert snap.account_id == "ghost"
    assert snap.home_url == "https://www.tiktok.com/@ghost"


def test_tiktok_burst_marking_uses_platform_likes():
    user_info, items = bf.parse_tiktok_ssr(TIKTOK_HTML)
    snap = bf.snapshot_from_tiktok("somecreator", user_info, items, "x")
    bf.mark_burst(snap.works)
    # 只有 2 条样本 → 样本不足不标记（与竞品方法论一致）
    assert [w.is_burst for w in snap.works] == [False, False]


# --- 以下三条对应 2026-10 实测踩到的真实坑（都曾让封面/账号信息静默为空） ---
def test_tiktok_cover_accepts_plain_string():
    """接口给的是纯字符串 URL，SSR 给的是 {urlList}；只认一种就会静默丢封面。"""
    assert bf._first_url("https://p16.example/a.jpeg") == "https://p16.example/a.jpeg"
    assert bf._first_url({"urlList": ["https://p16.example/b.jpeg"]}) == "https://p16.example/b.jpeg"
    assert bf._first_url({"url": "https://p16.example/c.jpeg"}) == "https://p16.example/c.jpeg"
    assert bf._first_url(None) == "" and bf._first_url({}) == ""

    snap = bf.snapshot_from_tiktok("someone", {}, [{
        "id": "7412", "desc": "x", "createTime": 1730000000,
        "stats": {"diggCount": 1},
        "video": {"cover": "http://p16.example/plain.jpeg"},      # 纯字符串
    }], "x")
    assert snap.works[0].cover_url == "https://p16.example/plain.jpeg"


def test_tiktok_ssr_reads_itemlist_nested_in_userinfo():
    """实测 itemList 嵌在 userInfo 下（不在 detail 一级）。"""
    html = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
            '{"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {'
            '"user": {"uniqueId": "nested", "nickname": "Nested"},'
            '"stats": {"followerCount": 10},'
            '"itemList": [{"id": "999", "desc": "nested item", "stats": {"diggCount": 7}}]'
            '}}}}</script>')
    user_info, items = bf.parse_tiktok_ssr(html)
    assert user_info["user"]["uniqueId"] == "nested"
    assert [it["id"] for it in items] == ["999"]


def test_tiktok_account_falls_back_to_item_author_stats():
    """user/detail 不一定触发，但每条作品自带 author/authorStats，账号信息不该为空。"""
    items = [{
        "id": "7412", "desc": "hello", "createTime": 1730000000,
        "stats": {"diggCount": 5, "collectCount": 1, "commentCount": 2},
        "author": {"uniqueId": "fallback", "nickname": "Fallback Creator",
                   "signature": "bio here", "avatarLarger": "http://p16.example/av.jpeg"},
        "authorStats": {"followerCount": 4321, "heartCount": 98765, "videoCount": 12},
        "video": {"cover": "http://p16.example/c.jpeg"},
    }]
    snap = bf.snapshot_from_tiktok("fallback", {}, items, "TikTok 接口响应（页面内）")
    assert snap.nickname == "Fallback Creator"
    assert snap.bio == "bio here"
    assert snap.avatar == "https://p16.example/av.jpeg"
    assert snap.account_id == "fallback"
    assert snap.followers.num == 4321
    assert snap.likes.num == 98765          # heartCount
    assert snap.posts == 12


# --------------------------------------------------------------------------- #
# Web 接口（本地存储，不联网）
# --------------------------------------------------------------------------- #
PLATFORMS = [
    {"platform": "xiaohongshu", "name": "小红书", "auto": True, "experimental": False,
     "id_label": "小红书 user_id", "id_hint": "", "fields": {"work_likes": True}, "note": "约 30 篇/次"},
    {"platform": "tiktok", "name": "TikTok", "auto": False, "experimental": False,
     "id_label": "TikTok 账号", "id_hint": "", "fields": {}, "note": "接口已预留"},
]


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """把存储落到 tmp_path，并让平台能力矩阵走假数据。"""
    bench_dir = tmp_path / "_benchmarks"
    monkeypatch.setattr(web, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(web, "BENCH_ACCOUNTS_FILE", bench_dir / "accounts.json")
    monkeypatch.setattr(web, "_BENCH_PLATFORMS_CACHE", [dict(p) for p in PLATFORMS])
    monkeypatch.setattr(web, "BENCH_RUNNING", {})
    monkeypatch.setattr(web, "BENCH_LAST_START", {})
    monkeypatch.setattr(web, "_bench_script", lambda *a, **k: (0, {"target": "55753cf4f5a2633511d5a652"}))
    return web


def _create(bench, platform="xiaohongshu", target="55753cf4f5a2633511d5a652"):
    return asyncio.run(bench.api_bench_create(
        bench.BenchmarkItem(platform=platform, target=target, remark="同赛道头部")))


def test_create_list_delete_roundtrip(bench):
    acc = _create(bench)
    assert acc["platform"] == "xiaohongshu"
    assert acc["account_id"] == "55753cf4f5a2633511d5a652"
    assert acc["status"] == "new"

    listed = asyncio.run(bench.api_bench_list())
    assert [a["id"] for a in listed] == [acc["id"]]
    assert listed[0]["running"] is False

    # 重复添加要挡掉（同一平台同一账号）
    with pytest.raises(HTTPException) as e:
        _create(bench)
    assert e.value.status_code == 409

    asyncio.run(bench.api_bench_delete(acc["id"]))
    assert asyncio.run(bench.api_bench_list()) == []
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_delete(acc["id"]))
    assert e.value.status_code == 404


def test_manual_platform_allowed_but_not_auto_fetchable(bench):
    acc = _create(bench, platform="tiktok", target="https://www.tiktok.com/@someone")
    assert acc["status"] == "manual"
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_fetch(acc["id"]))
    assert e.value.status_code == 400
    assert "暂不支持自动抓取" in str(e.value.detail)


def test_create_rejects_bad_target(bench, monkeypatch):
    monkeypatch.setattr(web, "_bench_script",
                        lambda *a, **k: (4, {"error": "小红书需要 24 位 user_id"}))
    with pytest.raises(HTTPException) as e:
        _create(bench, target="not-a-link")
    assert e.value.status_code == 400


def test_unknown_platform_rejected(bench):
    with pytest.raises(HTTPException) as e:
        _create(bench, platform="myspace")
    assert e.value.status_code == 400


FETCH_PAYLOAD = {
    "platform": "xiaohongshu", "platform_name": "小红书", "account_id": "55753cf4f5a2633511d5a652",
    "nickname": "大厂升升", "avatar": "", "bio": "搞钱访谈", "home_url": "https://x/user/profile/557",
    "followers": {"raw": "1万+", "num": 10000, "lower_bound": True},
    "likes": {"raw": "1万+（获赞与收藏）", "num": 10000, "lower_bound": True},
    "favorites": None, "comments": None, "posts": None,
    "coverage": "主页 SSR 批次 3 篇（一次请求；平台不提供翻页）",
    "source": "xhs_fetch.py（SSR 首页）", "logged_in": True, "risk": None,
    "warnings": ["小红书号：BunnyIP"], "median_likes": 249, "fetched_at": 1760000000,
    "works": [
        {"id": "", "title": "副业思路", "url": "", "cover_url": "https://cdn/a.webp",
         "cover": "aaa.jpg", "published_at_ms": 1758719093000, "likes": 13000,
         "favorites": None, "comments": None, "is_top": True, "is_burst": True,
         "detail_available": False, "extra": {"xsec_token": "T"}},
        {"id": "", "title": "第二条", "url": "", "cover_url": "https://cdn/b.webp",
         "cover": "", "published_at_ms": 1792062160000, "likes": 246,
         "is_burst": False, "detail_available": False, "extra": {}},
    ],
}


def test_fetch_persists_snapshot_and_marks_cover_path(bench, monkeypatch):
    acc = _create(bench)
    calls = []

    def fake_script(cmd, timeout=30):
        calls.append(cmd)
        if cmd[0] == "platforms":
            return 0, {"platforms": PLATFORMS}
        return 0, dict(FETCH_PAYLOAD)

    monkeypatch.setattr(web, "_bench_script", fake_script)

    asyncio.run(bench._bench_run_fetch(acc["id"]))

    stored = json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8"))
    assert stored[0]["nickname"] == "大厂升升"
    assert stored[0]["followers"]["num"] == 10000
    assert stored[0]["followers"]["lower_bound"] is True      # 量级标记一路保留
    assert stored[0]["median_likes"] == 249
    assert stored[0]["work_count"] == 2
    assert stored[0]["status"] == "ok"
    assert stored[0]["last_error"] == ""

    works_doc = json.loads((bench.BENCH_DIR / acc["id"] / "works.json").read_text(encoding="utf-8"))
    assert works_doc["works"][0]["cover_path"] == f"_benchmarks/{acc['id']}/covers/aaa.jpg"
    assert works_doc["works"][1]["cover_path"] == ""          # 没下到封面就不编路径

    # 历史快照：每次抓取追加一行，供以后看涨粉/涨赞
    hist = (bench.BENCH_DIR / acc["id"] / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(hist) == 1
    assert json.loads(hist[0])["followers"] == 10000

    # 详情接口读到落盘的作品
    detail = asyncio.run(bench.api_bench_detail(acc["id"]))
    assert len(detail["works"]) == 2
    assert detail["median_likes"] == 249
    assert detail["account"]["nickname"] == "大厂升升"


def test_fetch_risk_stop_is_recorded_not_retried(bench, monkeypatch):
    acc = _create(bench)
    monkeypatch.setattr(web, "_bench_script",
                        lambda *a, **k: (2, {"platform": "xiaohongshu", "risk": "300012"}))

    asyncio.run(bench._bench_run_fetch(acc["id"]))

    stored = json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8"))
    assert stored[0]["status"] == "risk"
    assert "300012" in stored[0]["last_error"]
    status = json.loads((bench.BENCH_DIR / acc["id"] / "fetch.json").read_text(encoding="utf-8"))
    assert status["state"] == "risk"
    # 没有抓到的账号不留半截作品数据
    assert not (bench.BENCH_DIR / acc["id"] / "works.json").exists()


def test_fetch_error_recorded(bench, monkeypatch):
    acc = _create(bench)
    monkeypatch.setattr(web, "_bench_script",
                        lambda *a, **k: (3, {"error": "未取到主页数据（未登录 / 平台维护）"}))
    asyncio.run(bench._bench_run_fetch(acc["id"]))
    stored = json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8"))
    assert stored[0]["status"] == "error"
    assert "未取到主页数据" in stored[0]["last_error"]


def test_same_account_fetch_is_rate_limited(bench):
    acc = _create(bench)
    asyncio.run(bench.api_bench_fetch(acc["id"]))          # 第一次允许（只登记时间，不真的抓）
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_fetch(acc["id"]))       # 冷却期内直接拒绝
    assert e.value.status_code == 429
    assert "风控" in str(e.value.detail)


def test_work_detail_requires_platform_identity(bench):
    """平台不给作品 ID 时明确拒绝，而不是让用户等一次注定失败的深挖。"""
    acc = _create(bench)
    web._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json", {
        "works": [{"id": "", "title": "无 ID 作品", "extra": {"xsec_token": "T"}}]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_work_detail(acc["id"], ""))
    assert e.value.status_code == 400
    assert "作品 ID" in str(e.value.detail)

    # 有 ID 但缺 token（小红书 SSR 只给一个页面级 token）同样拒绝
    web._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json", {
        "works": [{"id": "abc123", "title": "有 ID 无 token", "extra": {}}]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_work_detail(acc["id"], "abc123"))
    assert e.value.status_code == 400
    assert "定位信息" in str(e.value.detail)


# --------------------------------------------------------------------------- #
# 作品级收藏/评论：跨刷新保留 + 失败不标记完成（2026-10-16 实测踩到的两个坑）
# --------------------------------------------------------------------------- #
DETAIL_WORKS = [
    {"id": "w1", "title": "第一条", "likes": 100, "favorites": None, "comments": None,
     "extra": {"xsec_token": "T1"}},
    {"id": "w2", "title": "第二条", "likes": 200, "favorites": None, "comments": None,
     "extra": {"xsec_token": "T2"}},
]


def test_refresh_keeps_deep_dived_metrics(bench, monkeypatch):
    """刷新会整批替换作品列表：已深挖出来的收藏/评论必须按 id 对齐保留下来。

    不然用户逐条读出来的数据会在下一次「获取最新作品」时被抹掉（实测踩到）。
    """
    acc = _create(bench)
    works_file = bench.BENCH_DIR / acc["id"] / "works.json"
    web._bench_write_json(works_file, {"works": [
        {"id": "w1", "title": "第一条", "favorites": 6314, "comments": 137,
         "detail_fetched": True, "extra": {"xsec_token": "T1"}},
        {"id": "w9", "title": "已掉出本批", "favorites": 5, "comments": 1, "detail_fetched": True},
    ]})

    fresh = [dict(w) for w in DETAIL_WORKS]           # 本次抓取：收藏/评论仍是 None
    monkeypatch.setattr(web, "_bench_script",
                        lambda *a, **k: (0, {"platform": "xiaohongshu", "platform_name": "小红书",
                                             "account_id": acc["account_id"], "nickname": "大厂升升",
                                             "followers": {"raw": "9.2万", "num": 92004, "lower_bound": False},
                                             "likes": None, "favorites": None, "comments": None,
                                             "posts": 292, "works": fresh, "coverage": "x",
                                             "source": "x", "warnings": [], "median_likes": 150}))
    asyncio.run(bench._bench_run_fetch(acc["id"]))

    kept = json.loads(works_file.read_text(encoding="utf-8"))["works"]
    by_id = {w["id"]: w for w in kept}
    assert by_id["w1"]["favorites"] == 6314 and by_id["w1"]["comments"] == 137
    assert by_id["w1"]["detail_fetched"] is True
    assert by_id["w2"]["favorites"] is None            # 没深挖过的仍是空，不编造
    assert "w9" not in by_id                            # 掉出本批的作品不会硬塞回来
    assert json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8"))[0][
        "detail_work_count"] == 1


def test_empty_detail_result_is_not_marked_fetched(bench, monkeypatch):
    """深挖没读到数据时不能标记「已完成」——否则它再也不会被批量重试，界面也看不出问题。"""
    acc = _create(bench)
    works_file = bench.BENCH_DIR / acc["id"] / "works.json"
    web._bench_write_json(works_file, {"works": [dict(DETAIL_WORKS[0])]})
    monkeypatch.setattr(web, "_bench_script", lambda *a, **k: (0, {
        "id": "w1", "detail_fetched": True, "likes": None, "favorites": None, "comments": None,
        "extra": {"detail_note": "页面上没读到互动数字"}}))

    work = asyncio.run(bench.api_bench_work_detail(acc["id"], "w1"))
    assert work.get("detail_error")
    saved = json.loads(works_file.read_text(encoding="utf-8"))["works"][0]
    assert not saved.get("detail_fetched"), "没数据就不能算完成"


def test_normalize_note_handles_both_shapes():
    """作品条目有两种形态，必须都认——否则"拿到条目却全是空字段"（实测踩过）。

    - 主页 SSR：包在 noteCard 里、驼峰（noteId/displayTitle/interactInfo/cover.infoList）
    - 签名分页接口：字段在顶层、蛇形（note_id/display_title/interact_info/cover.url_default）
    """
    ssr = {"id": "6a1b2c3d0000000000000001", "xsecToken": "TOK",
           "noteCard": {"type": "video", "displayTitle": "SSR 标题", "time": 1758719093000,
                        "interactInfo": {"likedCount": "1.3万", "sticky": True},
                        "cover": {"infoList": [
                            {"imageScene": "WB_PRV", "url": "http://x/prv.jpg"},
                            {"imageScene": "WB_DFT", "url": "http://x/dft.jpg"}]}}}
    api = {"note_id": "6a1b2c3d0000000000000002", "xsec_token": "TOK2",
           "type": "normal", "display_title": "分页标题", "time": 1758719094000,
           "interact_info": {"liked_count": "2.2万", "sticky": False},
           "cover": {"url_default": "http://y/dft.jpg",
                     "info_list": [{"image_scene": "WB_DFT", "url": "http://y/dft.jpg"}]}}

    a = xhs._normalize_note(ssr)
    assert a["note_id"] == "6a1b2c3d0000000000000001"
    assert a["title"] == "SSR 标题" and a["liked_count_num"] == 13000
    assert a["sticky"] is True and a["cover"] == "https://x/dft.jpg"     # 优先 WB_DFT，并升 https
    assert a["xsec_token"] == "TOK"

    b = xhs._normalize_note(api)
    assert b["note_id"] == "6a1b2c3d0000000000000002"
    assert b["title"] == "分页标题" and b["liked_count_num"] == 22000
    assert b["cover"] == "https://y/dft.jpg" and b["time_ms"] == 1758719094000
    assert b["xsec_token"] == "TOK2"
    # 无 ID 但有内容的条目要保留（游客态就是这种），只有真空壳才丢
    keep = xhs._normalize_note({"noteCard": {"displayTitle": "没有 ID 但有标题"}})
    assert keep is not None and keep["note_id"] == "" and keep["title"] == "没有 ID 但有标题"
    assert xhs._normalize_note({"noteCard": {}}) is None


def test_normalize_note_keeps_guest_entries_without_id():
    """游客态的作品**没有 ID** 是平台行为，不能当成无效条目丢掉。

    早先把「无 ID」当无效条目丢弃，导致游客态整批作品变成 0 条（登录态有 ID 所以没暴露）。
    """
    guest = {"id": "", "xsecToken": "",
             "noteCard": {"type": "video", "displayTitle": "游客态也能看到标题", "time": 1758719093000,
                          "interactInfo": {"likedCount": "2.2万"},
                          "cover": {"infoList": [{"imageScene": "WB_DFT", "url": "http://x/c.jpg"}]}}}
    note = xhs._normalize_note(guest)
    assert note is not None, "游客态条目必须保留"
    assert note["note_id"] == "" and note["title"] == "游客态也能看到标题"
    assert note["liked_count_num"] == 22000 and note["cover"] == "https://x/c.jpg"
    # 真正的空壳（无 ID、无标题、无封面、无点赞）才丢
    assert xhs._normalize_note({"noteCard": {}}) is None
    assert xhs._normalize_note({}) is None


def test_burst_baseline_uses_newest_30():
    """爆款中位数基准固定取「最新 30 条」，标记仍覆盖全部作品（用户口径）。"""
    works = []
    # 最新 30 条：点赞都是 100；更早的 5 条：点赞都是 100000（老爆款，不该抬高基准）
    for i in range(30):
        works.append(bf.Work(id=f"new{i}", published_at_ms=2_000_000_000_000 - i,
                             likes=100))
    for i in range(5):
        works.append(bf.Work(id=f"old{i}", published_at_ms=1_000_000_000_000 - i,
                             likes=100_000))
    works.append(bf.Work(id="viral", published_at_ms=2_000_000_000_001, likes=500))

    med = bf.mark_burst(works)
    assert med == 100, "基准应只看最新 30 条（中位数 100），不被更早的高赞作品抬高"
    by_id = {w.id: w for w in works}
    assert by_id["viral"].is_burst is True          # 500 ≥ 100×3
    assert by_id["new0"].is_burst is False
    assert by_id["old0"].is_burst is True           # 标记仍覆盖全部作品


def test_guest_downgrade_keeps_previous_works(bench, monkeypatch):
    """cookie 失效被判游客态时：不能拿"没有作品 ID"的批次覆盖掉能深挖的那批。"""
    acc = _create(bench)
    works_file = bench.BENCH_DIR / acc["id"] / "works.json"
    web._bench_write_json(works_file, {"works": [
        {"id": "good1", "title": "有 ID 的作品", "favorites": 10, "comments": 2,
         "detail_fetched": True, "extra": {"xsec_token": "T"}},
        {"id": "good2", "title": "有 ID 的作品2", "extra": {"xsec_token": "T2"}},
    ]})
    monkeypatch.setattr(web, "_bench_script", lambda *a, **k: (0, {
        "platform": "xiaohongshu", "platform_name": "小红书",
        "account_id": acc["account_id"], "nickname": "许哈哈", "logged_in": False,
        "followers": None, "likes": {"raw": "328921", "num": 328921}, "favorites": None,
        "posts": 222, "coverage": "主页 SSR 批次 32 条（未登录：只有这一批，且无作品 ID）",
        "source": "x", "median_likes": None,
        "warnings": ["游客态"],
        "works": [{"id": "", "title": "游客态作品（无 ID）", "likes": 100}]}))

    asyncio.run(bench._bench_run_fetch(acc["id"]))

    works = json.loads(works_file.read_text(encoding="utf-8"))["works"]
    assert [w["id"] for w in works] == ["good1", "good2"], "应保留上次有 ID 的那批"
    accs = json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8"))
    assert accs[0]["degraded"] is True
    assert any("保留上次" in w for w in accs[0]["warnings"])
    assert accs[0]["likes"]["num"] == 328921, "账号级精确指标仍要更新"


def test_normal_guest_first_fetch_still_stores_works(bench, monkeypatch):
    """首次抓取就是游客态（没有任何旧数据）时，照常存下来，不能因为保护逻辑丢数据。"""
    acc = _create(bench)
    monkeypatch.setattr(web, "_bench_script", lambda *a, **k: (0, {
        "platform": "xiaohongshu", "platform_name": "小红书",
        "account_id": acc["account_id"], "nickname": "许哈哈", "logged_in": False,
        "followers": None, "likes": None, "favorites": None, "posts": 222,
        "coverage": "x", "source": "x", "median_likes": None, "warnings": [],
        "works": [{"id": "", "title": "游客态作品", "likes": 100}]}))

    asyncio.run(bench._bench_run_fetch(acc["id"]))

    works = json.loads((bench.BENCH_DIR / acc["id"] / "works.json").read_text(
        encoding="utf-8"))["works"]
    assert len(works) == 1 and works[0]["title"] == "游客态作品"


def test_detail_batch_rejects_platform_without_deep_dive(bench):
    """列表里本来就有收藏/评论的平台（如抖音）不需要深挖，接口应明确拒绝。"""
    bench._BENCH_PLATFORMS_CACHE.append({
        "platform": "douyin", "name": "抖音", "auto": True, "experimental": False, "order": 1,
        "id_label": "sec_uid", "id_hint": "", "note": "",
        "fields": {"work_favorites": True, "work_comments": True}})
    acc = _create(bench, platform="douyin", target="MS4wLjABAAAAxyz")
    # 抖音的收藏/评论随列表返回，不提供单独深挖
    bench._BENCH_PLATFORMS_CACHE[-1]["fields"]["work_detail_manual"] = False
    with pytest.raises(HTTPException) as e:
        asyncio.run(bench.api_bench_works_detail_batch(acc["id"]))
    assert e.value.status_code == 400
    assert "不需要逐条深挖" in str(e.value.detail)


# --------------------------------------------------------------------------- #
# 批量深挖的执行：逐条落盘 + 命中风控即停（用假的抓取脚本驱动，不联网）
# --------------------------------------------------------------------------- #
def _batch_setup(bench, monkeypatch, replies):
    """准备 3 条待深挖的作品 + 一个按顺序返回结果的假脚本。"""
    acc = _create(bench)
    works_file = bench.BENCH_DIR / acc["id"] / "works.json"
    web._bench_write_json(works_file, {"works": [
        {"id": f"w{i}", "title": f"第{i}条", "favorites": None, "comments": None,
         "extra": {"xsec_token": f"T{i}"}} for i in range(1, 4)]})
    seq = list(replies)
    calls: list[list[str]] = []

    def fake_script(cmd, timeout=30):
        calls.append(cmd)
        if cmd[0] == "platforms":
            return 0, {"platforms": [dict(p) for p in PLATFORMS]}
        return seq.pop(0)

    monkeypatch.setattr(web, "_bench_script", fake_script)
    monkeypatch.setattr(web, "_bench_detail_gap", lambda: 0)     # 测试里不等间隔
    return acc, works_file, calls


def test_detail_batch_fills_works_and_persists_progress(bench, monkeypatch):
    payload = lambda fav, com: (0, {"id": "x", "likes": 10, "favorites": fav, "comments": com})
    acc, works_file, calls = _batch_setup(bench, monkeypatch, [
        payload(11, 1), payload(22, 2), payload(33, 3)])

    asyncio.run(bench._bench_run_detail_batch(acc["id"], 3))

    works = json.loads(works_file.read_text(encoding="utf-8"))["works"]
    assert [w["favorites"] for w in works] == [11, 22, 33]
    assert [w["comments"] for w in works] == [1, 2, 3]
    assert all(w["detail_fetched"] for w in works)
    # 串行：3 条作品 → 3 次 detail 调用（没有并发轰炸）
    assert [c[0] for c in calls] == ["detail", "detail", "detail"]
    st = json.loads((bench.BENCH_DIR / acc["id"] / "fetch.json").read_text(encoding="utf-8"))
    assert st["state"] == "done" and "3" in st["message"]


def test_detail_batch_stops_on_risk_control(bench, monkeypatch):
    """命中风控必须立刻停手：已完成部分保留，剩下的不再打（重试会升级成封禁）。"""
    acc, works_file, calls = _batch_setup(bench, monkeypatch, [
        (0, {"id": "w1", "favorites": 11, "comments": 1}),
        (2, {"error": "命中风控信号 '300012'：已停止，未重试"}),
    ])

    asyncio.run(bench._bench_run_detail_batch(acc["id"], 3))

    works = json.loads(works_file.read_text(encoding="utf-8"))["works"]
    assert works[0]["favorites"] == 11                # 第一条已落盘
    assert works[1]["favorites"] is None              # 后续不再尝试
    assert [c[0] for c in calls] == ["detail", "detail"]
    st = json.loads((bench.BENCH_DIR / acc["id"] / "fetch.json").read_text(encoding="utf-8"))
    assert st["state"] == "risk" and "风控" in st["message"]


def test_detail_batch_skips_already_filled(bench, monkeypatch):
    """已补齐的作品不再重复打接口（只补待补的）。"""
    acc, works_file, calls = _batch_setup(bench, monkeypatch, [
        (0, {"id": "w2", "favorites": 22}), (0, {"id": "w3", "favorites": 33})])
    data = json.loads(works_file.read_text(encoding="utf-8"))
    data["works"][0].update({"favorites": 5, "comments": 1, "detail_fetched": True})
    web._bench_write_json(works_file, data)

    asyncio.run(bench._bench_run_detail_batch(acc["id"], 3))

    asked = [c[c.index("--work-id") + 1] for c in calls]
    assert asked == ["w2", "w3"], "w1 已补齐，不该再打一次"
