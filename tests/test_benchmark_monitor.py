"""作品表现监控（15 天趋势）：存储、按日采样、到期与趋势接口。

不联网、不起浏览器：平台取数用假的 ``_bench_script`` 驱动。时间一律显式传入
（``_monitor_record(now=...)`` / ``_monitor_due(w, now)``），所以白天黑夜跑结果都一样。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))

import app as web  # noqa: E402

XHS_ID = "55753cf4f5a2633511d5a652"

PLATFORMS = [
    {"platform": "xiaohongshu", "name": "小红书", "auto": True, "experimental": False,
     "id_label": "小红书 user_id", "id_hint": "",
     # 作品指标要逐条读详情（work_detail_manual），收藏/评论值不在列表里；没有播放量
     "fields": {"followers": True, "work_likes": True, "work_favorites": True,
                "work_comments": True, "work_detail_manual": True}, "note": ""},
    {"platform": "douyin", "name": "抖音", "auto": True, "experimental": False,
     "id_label": "抖音 sec_uid", "id_hint": "",
     # 作品指标随列表返回，不逐条打；播放量不是公开数据（不声明 work_plays）
     "fields": {"followers": True, "work_likes": True, "work_favorites": True,
                "work_comments": True}, "note": ""},
    {"platform": "tiktok", "name": "TikTok", "auto": True, "experimental": False,
     "id_label": "TikTok @handle", "id_hint": "",
     # 列表型 + 公开播放量（多一条「播放量」曲线）
     "fields": {"followers": True, "work_likes": True, "work_favorites": True,
                "work_comments": True, "work_plays": True}, "note": ""},
    {"platform": "kuaishou", "name": "快手", "auto": False, "experimental": False,
     "id_label": "快手号", "id_hint": "", "fields": {}, "note": "只能手动登记"},
]


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """存储落到 tmp_path；平台能力矩阵与抓取脚本都走假数据。"""
    bench_dir = tmp_path / "_benchmarks"
    monkeypatch.setattr(web, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(web, "BENCH_ACCOUNTS_FILE", bench_dir / "accounts.json")
    monkeypatch.setattr(web, "_BENCH_PLATFORMS_CACHE", [dict(p) for p in PLATFORMS])
    monkeypatch.setattr(web, "BENCH_RUNNING", {})
    monkeypatch.setattr(web, "BENCH_LAST_START", {})
    monkeypatch.setattr(web, "BENCH_MONITOR_TASK", None)
    monkeypatch.setattr(web, "BENCH_MONITOR_LOOP_TASK", None)
    monkeypatch.setattr(web, "_bench_script",
                        lambda *a, **k: (0, {"target": XHS_ID}))
    return web


def _create(bench, platform="xiaohongshu", target=XHS_ID):
    return asyncio.run(bench.api_bench_create(
        bench.BenchmarkItem(platform=platform, target=target, remark="同赛道头部")))


XHS_WORKS = [
    {"id": "w1", "title": "第一条", "likes": 1000, "favorites": None, "comments": None,
     "extra": {"xsec_token": "T1"}},
    {"id": "w2", "title": "第二条", "likes": 200, "extra": {"xsec_token": "T2"}},
]


def _seed_xhs(bench):
    acc = _create(bench)
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json",
                            {"works": [dict(w) for w in XHS_WORKS]})
    return acc


def _detail_script(bench, monkeypatch, *, likes=1200, favorites=34, comments=5, error="",
                   code=0):
    """让 detail 子命令返回一组固定数字（或一个错误）。"""
    calls = []

    def fake(cmd, timeout=30):
        calls.append(list(cmd))
        if cmd[0] == "platforms":
            return 0, {"platforms": PLATFORMS}
        if cmd[0] == "parse":
            return 0, {"target": XHS_ID}
        if error:
            return code, {"error": error}
        return 0, {"likes": likes, "favorites": favorites, "comments": comments,
                   "id": cmd[cmd.index("--work-id") + 1]}

    monkeypatch.setattr(bench, "_bench_script", fake)
    return calls


def _watch(bench, bid, wid):
    return (bench._monitor_read(bid).get("works") or {}).get(wid) or {}


def _enable(bench, bid, wid):
    return asyncio.run(bench.api_bench_work_monitor(bid, wid, bench.BenchMonitorToggle(enabled=True)))


# --------------------------------------------------------------------------- #
# 存储与开关
# --------------------------------------------------------------------------- #
def test_enable_records_first_point_and_shows_on_card(bench, monkeypatch):
    """开启监控立刻记下第一个点——否则用户要等到明天才看得到任何数据。"""
    acc = _seed_xhs(bench)
    calls = _detail_script(bench, monkeypatch)

    r = _enable(bench, acc["id"], "w1")
    assert r["monitor"]["enabled"] is True
    assert r["monitor"]["days"] == 1
    assert r["sampled"] is True
    assert any(c[0] == "detail" for c in calls)

    point = _watch(bench, acc["id"], "w1")["points"][0]
    assert point["day"] == bench._monitor_day()
    assert (point["likes"], point["favorites"], point["comments"]) == (1200, 34, 5)
    assert point["ok"] is True

    # 作品卡数据里要带上监控摘要，前端才不用逐条再请求
    detail = asyncio.run(bench.api_bench_detail(acc["id"]))
    card = next(w for w in detail["works"] if w["id"] == "w1")
    assert card["monitor"]["enabled"] is True and card["monitor"]["days"] == 1
    assert card["monitor"]["last"]["likes"] == 1200
    other = next(w for w in detail["works"] if w["id"] == "w2")
    assert other["monitor"]["enabled"] is False and other["monitor"]["days"] == 0


def test_disable_keeps_recorded_points(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    r = asyncio.run(bench.api_bench_work_monitor(
        acc["id"], "w1", bench.BenchMonitorToggle(enabled=False)))
    assert r["monitor"]["enabled"] is False
    assert r["monitor"]["days"] == 1, "关掉监控不能把已经记下的点删了"
    assert "保留" in r["message"]


def test_enable_requires_platform_that_can_fetch(bench):
    acc = _create(bench, platform="kuaishou", target="https://www.kuaishou.com/profile/x")
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json",
                            {"works": [{"id": "w1", "title": "x"}]})
    with pytest.raises(HTTPException) as e:
        _enable(bench, acc["id"], "w1")
    assert e.value.status_code == 400
    assert "无法监控" in str(e.value.detail)


def test_toggle_unknown_work_is_404(bench):
    acc = _seed_xhs(bench)
    with pytest.raises(HTTPException) as e:
        _enable(bench, acc["id"], "nope")
    assert e.value.status_code == 404


def test_work_without_id_cannot_be_monitored(bench):
    """游客态的作品没有 ID：无法定位，也就无法按日采样（要如实拒绝，不能建个空监控）。"""
    acc = _create(bench)
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json",
                            {"works": [{"id": "", "title": "游客态条目"}]})
    with pytest.raises(HTTPException) as e:
        _enable(bench, acc["id"], "")
    assert e.value.status_code == 400
    assert "作品 ID" in str(e.value.detail)
    assert not bench._monitor_read(acc["id"])["works"], "拒绝时不该留下半截监控记录"


# --------------------------------------------------------------------------- #
# 每天一条：同一天覆盖、跨天新增
# --------------------------------------------------------------------------- #
def test_same_day_overwrites_instead_of_duplicating(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, likes=1200)
    _enable(bench, acc["id"], "w1")

    _detail_script(bench, monkeypatch, likes=1350, favorites=40, comments=6)
    asyncio.run(bench._bench_monitor_run(force=True))

    points = _watch(bench, acc["id"], "w1")["points"]
    assert len(points) == 1, "一天只能有一个点，否则「最近 15 天」会变成 15 个时刻"
    assert points[0]["likes"] == 1350, "重采要覆盖成最新的数"


def test_next_day_adds_a_point(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, likes=1200)
    _enable(bench, acc["id"], "w1")

    data = bench._monitor_read(acc["id"])
    watch = data["works"]["w1"]
    bench._monitor_record(watch, likes=1500, favorites=50, comments=7, now=time.time() + 86400)
    bench._monitor_write(acc["id"], data)

    points = _watch(bench, acc["id"], "w1")["points"]
    assert [p["likes"] for p in points] == [1200, 1500]
    assert bench._monitor_summary(_watch(bench, acc["id"], "w1"))["days"] == 2


# --------------------------------------------------------------------------- #
# 15 天上限
# --------------------------------------------------------------------------- #
def test_expires_after_15_days_and_stops_sampling(bench, monkeypatch):
    """连续记录 15 天：第 16 天自动停止（不是继续打平台），已记录的点保留。"""
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    now = time.time()
    data = bench._monitor_read(acc["id"])
    watch = data["works"]["w1"]
    watch["started_at"] = int(now - 15 * 86400)     # 窗口 15 天前开始 → 今天是第 16 天
    watch["points"] = []
    for offset in range(15, 0, -1):                 # 窗口内 15 天各一个点
        bench._monitor_record(watch, likes=1200 + offset, favorites=34, comments=5,
                              now=now - offset * 86400)
    bench._monitor_write(acc["id"], data)

    assert len(watch["points"]) == 15
    assert bench._monitor_summary(watch, now)["days"] == 15
    assert bench._monitor_expired(watch, now - 86400) is False, "第 15 天还算在窗口内"
    assert bench._monitor_expired(watch, now) is True, "第 16 天到期"

    # 到期后：读取接口把它自动关掉并标记 expired
    detail = asyncio.run(bench.api_bench_detail(acc["id"]))
    card = next(w for w in detail["works"] if w["id"] == "w1")
    assert card["monitor"]["enabled"] is False
    assert card["monitor"]["expired"] is True
    assert card["monitor"]["days"] == 15
    assert len(_watch(bench, acc["id"], "w1")["points"]) == 15, "到期的点要保留，能回看"

    # 也不再被调度器挑中
    assert bench._monitor_targets(force=False, now=now) == []


def test_expired_watch_can_restart_a_fresh_window(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    data = bench._monitor_read(acc["id"])
    watch = data["works"]["w1"]
    watch["started_at"] = int(time.time()) - 30 * 86400      # 上一轮早就到期
    watch["expired"] = True
    watch["enabled"] = False
    bench._monitor_write(acc["id"], data)

    r = _enable(bench, acc["id"], "w1")
    assert r["monitor"]["enabled"] is True
    assert r["monitor"]["expired"] is False
    fresh = _watch(bench, acc["id"], "w1")
    assert bench._monitor_day(fresh["started_at"]) == bench._monitor_day()
    assert bench._monitor_summary(fresh)["days"] == 1, "新一轮从今天重新开始记"


# --------------------------------------------------------------------------- #
# 「每天固定时间」与补采
# --------------------------------------------------------------------------- #
def test_due_only_after_the_scheduled_time(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    data = bench._monitor_read(acc["id"])
    watch = data["works"]["w1"]
    watch["points"] = []                     # 假装今天还没采过
    bench._monitor_write_schedule(9, 0)
    today_at = bench._monitor_today_at(time.time())
    assert today_at > 0

    assert bench._monitor_due(watch, today_at - 60) is False, "没到点不采"
    assert bench._monitor_due(watch, today_at + 60) is True

    # 今天已经采过就不再采（否则每分钟都会打一次平台）
    bench._monitor_record(watch, likes=1, favorites=1, comments=1)
    assert bench._monitor_due(watch, today_at + 120) is False


def test_catch_up_after_downtime_same_day(bench, monkeypatch):
    """进程当时没在跑：启动后补当天那一次，但同一天不会补出第二个点。"""
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")
    bench._monitor_write_schedule(9, 0)
    now = time.time()

    # 把今天已记的点挪到昨天，模拟「今天还没采过」
    data = bench._monitor_read(acc["id"])
    watch = data["works"]["w1"]
    for p in watch["points"]:
        p["day"] = bench._monitor_day(now - 86400)
    bench._monitor_write(acc["id"], data)

    after_due = max(now, bench._monitor_today_at(now) + 60)
    targets = bench._monitor_targets(force=False, now=after_due)
    assert [t[2] for t in targets] == ["w1"], "错过的当天要补采"

    asyncio.run(bench._bench_monitor_run(force=False))
    days = [p["day"] for p in _watch(bench, acc["id"], "w1")["points"]]
    assert days.count(bench._monitor_day()) == 1, "补采也只记一条"


def test_schedule_roundtrip_and_validation(bench):
    got = asyncio.run(bench.api_bench_monitor_schedule())
    assert (got["hour"], got["minute"]) == (9, 0)
    assert got["window_days"] == 15
    assert got["next_run_ts"] > time.time() - 60

    saved = asyncio.run(bench.api_bench_monitor_set_schedule(
        bench.BenchMonitorSchedule(hour=21, minute=30)))
    assert (saved["hour"], saved["minute"]) == (21, 30)
    assert bench._monitor_read_schedule()["hour"] == 21

    for bad in (bench.BenchMonitorSchedule(hour=24, minute=0),
                bench.BenchMonitorSchedule(hour=8, minute=60)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(bench.api_bench_monitor_set_schedule(bad))
        assert e.value.status_code == 400


def test_scheduler_loop_starts_stops_and_samples_when_due(bench, monkeypatch):
    """调度循环本身：启动后挂上任务，到点自己采一轮，关机时能干净取消。"""
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, likes=1200)
    monkeypatch.setattr(bench, "BENCH_MONITOR_STARTUP_DELAY", 0)
    monkeypatch.setattr(bench, "BENCH_MONITOR_TICK", 0.05)
    bench._monitor_write_schedule(0, 0)          # 今天 00:00 那个采样点已经过了 → 该采
    bench._monitor_write(acc["id"], {"works": {"w1": {
        "enabled": True, "expired": False, "started_at": int(time.time()), "points": []}}})

    async def scenario():
        bench._bench_monitor_loop_start()
        loop_task = bench.BENCH_MONITOR_LOOP_TASK
        assert loop_task is not None and not loop_task.done()
        for _ in range(80):                       # 等这一轮真正跑完（最多约 4 秒）
            task = bench.BENCH_MONITOR_TASK
            if task is not None:
                await task
                break
            await asyncio.sleep(0.05)
        await bench._bench_monitor_loop_stop()
        assert bench.BENCH_MONITOR_LOOP_TASK is None
        assert loop_task.done()

    asyncio.run(scenario())
    points = _watch(bench, acc["id"], "w1")["points"]
    assert len(points) == 1 and points[0]["likes"] == 1200
    assert bench._monitor_read_status()["state"] == "done"


def test_scheduler_disabled_by_env(bench, monkeypatch):
    monkeypatch.setenv("EASEL_BENCH_MONITOR", "0")

    async def scenario():
        bench._bench_monitor_loop_start()
        assert bench.BENCH_MONITOR_LOOP_TASK is None, "环境变量关掉时不该起调度循环"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 采样失败 / 风控：不编造、不覆盖已有真实数据
# --------------------------------------------------------------------------- #
def test_failed_sample_is_recorded_as_gap_not_zero(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, error="没读到互动数据（笔记可能已删除）", code=4)
    r = _enable(bench, acc["id"], "w1")

    assert r["sampled"] is False
    assert "没读到数据" in r["message"]
    point = _watch(bench, acc["id"], "w1")["points"][0]
    assert point["ok"] is False
    assert point["likes"] is None and point["favorites"] is None and point["comments"] is None
    assert "已删除" in point["error"]


def test_failed_resample_never_overwrites_a_good_point(bench, monkeypatch):
    """今天已经读到过真实数字，之后一次失败不能把它抹成空值。"""
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, likes=1200)
    _enable(bench, acc["id"], "w1")

    _detail_script(bench, monkeypatch, error="请求超时", code=4)
    asyncio.run(bench._bench_monitor_run(force=True))

    point = _watch(bench, acc["id"], "w1")["points"][0]
    assert point["likes"] == 1200 and point["ok"] is True
    assert "超时" in _watch(bench, acc["id"], "w1")["last_error"]


def test_risk_control_stops_the_round(bench, monkeypatch):
    """命中风控立即停手：这一轮后面的作品不再打，也不会重试。"""
    acc = _seed_xhs(bench)
    _seed_works = [dict(w) for w in XHS_WORKS]
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json", {"works": _seed_works})
    _detail_script(bench, monkeypatch, likes=1200)
    _enable(bench, acc["id"], "w1")
    _enable(bench, acc["id"], "w2")

    calls = _detail_script(bench, monkeypatch, error="300012 IP 风险限制", code=2)
    asyncio.run(bench._bench_monitor_run(force=True))

    st = bench._monitor_read_status()
    assert st["state"] == "risk"
    assert "风控" in st["message"]
    assert len([c for c in calls if c[0] == "detail"]) == 1, "风控后不许继续打第二条"
    # 被拦的那条要留下原因（不记假的采样点），之后还能在卡片/弹窗里看出来
    assert "风控" in _watch(bench, acc["id"], "w1")["last_error"]


# --------------------------------------------------------------------------- #
# 列表型平台（抖音/TikTok）：刷一次主页拿到全部作品指标
# --------------------------------------------------------------------------- #
def test_list_platform_samples_from_account_refresh(bench, monkeypatch):
    acc = _create(bench, platform="douyin", target="MS4wLjABAAAAxyz")
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json", {"works": [
        {"id": "d1", "title": "抖音第一条", "likes": 90, "favorites": 7, "comments": 2}]})
    calls = []

    def fake(cmd, timeout=30):
        calls.append(list(cmd))
        if cmd[0] == "platforms":
            return 0, {"platforms": PLATFORMS}
        if cmd[0] == "parse":
            return 0, {"target": "MS4wLjABAAAAxyz"}
        return 0, {"platform": "douyin", "platform_name": "抖音", "account_id": "MS4wLjABAAAAxyz",
                   "nickname": "同赛道", "works": [
                       {"id": "d1", "title": "抖音第一条", "likes": 400, "favorites": 20,
                        "comments": 9}], "logged_in": True, "warnings": []}

    monkeypatch.setattr(bench, "_bench_script", fake)

    r = _enable(bench, acc["id"], "d1")
    assert r["sampled"] is False, "开启时不该为一个点去刷整个主页"
    assert "每天固定时间" in r["message"]

    asyncio.run(bench._bench_monitor_run(force=True))
    point = _watch(bench, acc["id"], "d1")["points"][0]
    assert (point["likes"], point["favorites"], point["comments"]) == (400, 20, 9)
    assert acc["id"] in bench.BENCH_LAST_START, "整号刷新过要打冷却，别让用户马上再点一次刷新"


def test_list_platform_refresh_failure_marks_gap(bench, monkeypatch):
    acc = _create(bench, platform="douyin", target="MS4wLjABAAAAxyz")
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json",
                            {"works": [{"id": "d1", "title": "x", "likes": 1}]})
    bench._monitor_write(acc["id"], {"works": {"d1": {
        "enabled": True, "expired": False, "started_at": int(time.time()), "points": []}}})

    def fake(cmd, timeout=30):
        if cmd[0] == "platforms":
            return 0, {"platforms": PLATFORMS}
        return 0, {"platform": "douyin", "error": "网络不可达"}

    monkeypatch.setattr(bench, "_bench_script", fake)
    asyncio.run(bench._bench_monitor_run(force=True))

    point = _watch(bench, acc["id"], "d1")["points"][0]
    assert point["ok"] is False and point["likes"] is None
    assert "网络不可达" in point["error"]


def test_tiktok_plays_are_recorded_and_plotted(bench, monkeypatch):
    """TikTok 公开播放量（即阅读量）：采样要记下来，趋势要多一条曲线。"""
    acc = _create(bench, platform="tiktok", target="someone")
    bench._bench_write_json(bench.BENCH_DIR / acc["id"] / "works.json",
                            {"works": [{"id": "t1", "title": "TikTok 第一条", "likes": 5,
                                        "favorites": 1, "comments": 0, "plays": 100}]})

    def fake(cmd, timeout=30):
        if cmd[0] == "platforms":
            return 0, {"platforms": PLATFORMS}
        if cmd[0] == "parse":
            return 0, {"target": "someone"}
        return 0, {"platform": "tiktok", "platform_name": "TikTok", "account_id": "someone",
                   "nickname": "同赛道", "logged_in": True, "warnings": [],
                   "works": [{"id": "t1", "title": "TikTok 第一条", "likes": 400,
                              "favorites": 20, "comments": 9, "plays": 12000}]}

    monkeypatch.setattr(bench, "_bench_script", fake)
    assert _enable(bench, acc["id"], "t1")["sampled"] is False
    asyncio.run(bench._bench_monitor_run(force=True))

    point = _watch(bench, acc["id"], "t1")["points"][0]
    assert (point["likes"], point["favorites"], point["comments"], point["plays"]) \
        == (400, 20, 9, 12000)

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "t1"))
    assert [s["key"] for s in trend["series"]] == ["likes", "favorites", "comments", "plays"]
    assert trend["available"]["plays"] is True
    plays = trend["series"][3]
    assert plays["label"] == "播放量" and plays["available"] is True
    assert plays["stats"]["count"] == 1 and plays["stats"]["last"] == 12000
    # 只有今天一个点：x 轴就一格，值在最左侧那格，不做任何填充
    assert len(plays["rows"]) == 1 and plays["rows"][0]["value"] == 12000
    assert trend["axis"]["slots"] == 1


def test_plays_series_hidden_for_platforms_without_it(bench, monkeypatch):
    """小红书没有播放量、抖音的 play_count 恒为 0：都不能多出这条曲线。"""
    acc = _seed_xhs(bench)
    _seed_points(bench, acc)
    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert trend["available"]["plays"] is False
    assert [s["key"] for s in trend["series"]] == ["likes", "favorites", "comments"]


# --------------------------------------------------------------------------- #
# 趋势接口：3 条序列、按自然日对齐、缺口不插值
# --------------------------------------------------------------------------- #
def _seed_points(bench, acc, wid="w1", *, enabled=True, days=None):
    """按「距今天数 → likes」写点；days 省略时用固定的三天（中间留一个缺口）。"""
    now = time.time()
    data = bench._monitor_read(acc["id"])
    watch = data["works"].get(wid) or {
        "enabled": enabled, "expired": False, "started_at": int(now - 14 * 86400), "points": []}
    values = days if days is not None else {3: 1000, 1: 1400, 0: 1800}
    for offset in sorted(values, reverse=True):
        likes = values[offset]
        bench._monitor_record(watch, likes=likes, favorites=likes // 10, comments=1,
                              now=now - offset * 86400)
    watch["enabled"] = enabled
    data["works"][wid] = watch
    bench._monitor_write(acc["id"], data)
    return watch


def test_trend_returns_three_series_aligned_to_days(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _seed_points(bench, acc)

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert trend["days"] == 15 and trend["window_days"] == 15
    assert [s["key"] for s in trend["series"]] == ["likes", "favorites", "comments"]
    likes = next(s for s in trend["series"] if s["key"] == "likes")
    assert likes["available"] is True
    assert trend["available"] == {"likes": True, "favorites": True, "comments": True,
                                  "plays": False}

    # x 轴从「第一个有记录的日子」开始：这里是前三天那个点，所以只有 4 个刻度，
    # 而不是「今天往左 15 天」的 15 个格子（那样第一个点会被挤到最右边）
    assert trend["axis"]["slots"] == 4
    assert len(likes["rows"]) == 4, "按自然日对齐：区间内有 4 天就 4 个格子"
    assert trend["axis"]["start_day"] == likes["rows"][0]["day"]
    assert trend["axis"]["end_day"] == trend["today"], "还在监控中，右端画到今天"

    # 第一天在最左、中间那天没采到 → 必须是 null（不是 0，也不是重复前一天）
    values = [r["value"] for r in likes["rows"]]
    assert values == [1000, None, 1400, 1800]
    assert [r["sampled"] for r in likes["rows"]] == [True, False, True, True]

    assert likes["stats"]["first"] == 1000 and likes["stats"]["last"] == 1800
    assert likes["stats"]["delta"] == 800
    assert likes["stats"]["pct"] == 80.0
    assert likes["stats"]["count"] == 3
    assert trend["monitor"]["enabled"] is True


def test_axis_starts_at_the_first_sample_not_at_the_window_edge(bench, monkeypatch):
    """刚开始监控时，第一个点必须贴在最左侧（用户实测反馈：原来挤在最右边）。"""
    acc = _seed_xhs(bench)
    _seed_points(bench, acc, days={2: 100, 1: 130, 0: 170})

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    rows = trend["series"][0]["rows"]
    assert len(rows) == 3, "只有 3 天有记录就只画 3 个刻度，不留给右侧的空日子"
    assert rows[0]["value"] == 100, "第一个数据点就在最左侧那一格"
    assert trend["axis"]["start_day"] == rows[0]["day"]
    assert trend["axis"]["end_day"] == trend["today"]


def test_axis_ends_at_the_last_sample_after_stop(bench, monkeypatch):
    """停掉监控后右端画到最后一个点，不留一排空刻度；也不再标「今天」。"""
    acc = _seed_xhs(bench)
    _seed_points(bench, acc, days={5: 100, 4: 130, 3: 170}, enabled=False)

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    rows = trend["series"][0]["rows"]
    assert len(rows) == 3
    assert trend["axis"]["end_day"] == rows[-1]["day"] != trend["today"]
    assert [r["value"] for r in rows] == [100, 130, 170]


def test_axis_never_exceeds_the_15_day_window(bench, monkeypatch):
    """记满 15 天就是 15 个刻度；窗口外的老点不会被翻出来。"""
    acc = _seed_xhs(bench)
    _seed_points(bench, acc, days={d: 1000 + d for d in range(20)})   # 20 天，超出窗口

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert trend["axis"]["slots"] == 15
    assert len(trend["series"][0]["rows"]) == 15
    assert trend["axis"]["end_day"] == trend["today"]


def test_trend_marks_platform_unavailable_metrics(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _seed_points(bench, acc)
    monkeypatch.setattr(bench, "_BENCH_PLATFORMS_CACHE", [{
        "platform": "xiaohongshu", "name": "小红书", "auto": True, "experimental": False,
        "id_label": "", "id_hint": "",
        "fields": {"work_likes": True, "work_favorites": False, "work_comments": False},
        "note": ""}])

    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert trend["available"]["favorites"] is False
    fav = next(s for s in trend["series"] if s["key"] == "favorites")
    assert fav["available"] is False, "平台不公开的指标要标出来，前端据此不画空图"


def test_trend_days_param_is_an_upper_bound(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _seed_points(bench, acc, days={d: 1000 + d for d in range(12)})   # 12 天连续

    full = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert full["axis"]["slots"] == 12, "默认按数据本身的范围画"

    week = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1", days=7))
    assert week["days"] == 7
    assert week["axis"]["slots"] == 7, "days 是上限：从右往左取 7 天"
    assert week["axis"]["end_day"] == week["today"]
    # 只留最近 7 天：最左是 6 天前（likes=1006），最右是今天（likes=1000）
    assert week["series"][0]["rows"][0]["value"] == 1006
    assert week["series"][0]["rows"][-1]["value"] == 1000
    assert week["series"][0]["stats"]["count"] == 7

    capped = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1", days=99))
    assert capped["days"] == 15, "days 参数本身最多到 15"
    assert capped["axis"]["slots"] == 12, "但不会凭空多画空刻度"


def test_trend_without_monitor_is_empty_not_error(bench):
    acc = _seed_xhs(bench)
    trend = asyncio.run(bench.api_bench_work_trend(acc["id"], "w1"))
    assert trend["monitor"]["enabled"] is False
    assert trend["monitor"]["days"] == 0
    assert trend["axis"]["slots"] == 1, "没有监控就没有区间，只保留今天这一格"
    assert all(r["value"] is None for r in trend["series"][0]["rows"])
    assert trend["run"]["running"] is False


def test_trend_unknown_ids_are_404(bench):
    acc = _seed_xhs(bench)
    with pytest.raises(HTTPException):
        asyncio.run(bench.api_bench_work_trend(acc["id"], "nope"))
    with pytest.raises(HTTPException):
        asyncio.run(bench.api_bench_work_trend("nobid1234", "w1"))


# --------------------------------------------------------------------------- #
# 手动采样与状态
# --------------------------------------------------------------------------- #
def test_run_now_reports_idle_without_targets(bench):
    _seed_xhs(bench)
    r = asyncio.run(bench.api_bench_monitor_run())
    assert r["state"] == "idle" and r["total"] == 0

    asyncio.run(bench._bench_monitor_run(force=True))
    assert bench._monitor_read_status()["state"] == "idle"
    assert "没有需要采样" in bench._monitor_read_status()["message"]


def test_run_now_only_covers_enabled_works(bench, monkeypatch):
    acc = _seed_xhs(bench)
    calls = _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    r = asyncio.run(bench.api_bench_monitor_run(force=True, bid=acc["id"]))
    assert r["state"] == "running" and r["total"] == 1
    # 让后台任务跑完，避免测试结束时留下悬挂的 task
    asyncio.run(web.BENCH_MONITOR_TASK) if web.BENCH_MONITOR_TASK else None
    assert len([c for c in calls if c[0] == "detail"]) >= 1


def test_account_list_reports_monitor_counts(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")

    listed = asyncio.run(bench.api_bench_list())
    assert listed[0]["monitor"]["monitored"] == 1
    assert listed[0]["monitor"]["total"] == 1


def test_monitor_state_survives_a_refresh(bench, monkeypatch):
    """刷新作品列表会重写 works.json，监控序列在独立文件里，不能被顺手抹掉。"""
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch, likes=1200)
    _enable(bench, acc["id"], "w1")

    monkeypatch.setattr(bench, "_bench_script", lambda *a, **k: (0, {
        "platform": "xiaohongshu", "account_id": XHS_ID, "nickname": "大厂升升",
        "works": [dict(w) for w in XHS_WORKS], "logged_in": True, "warnings": []}))
    asyncio.run(bench._bench_run_fetch(acc["id"], sync=False))

    assert _watch(bench, acc["id"], "w1")["points"][0]["likes"] == 1200


def test_deleting_account_removes_monitor_file(bench, monkeypatch):
    acc = _seed_xhs(bench)
    _detail_script(bench, monkeypatch)
    _enable(bench, acc["id"], "w1")
    assert (bench.BENCH_DIR / acc["id"] / "monitor.json").is_file()

    asyncio.run(bench.api_bench_delete(acc["id"]))
    assert not (bench.BENCH_DIR / acc["id"]).exists()
    assert json.loads((bench.BENCH_DIR / "accounts.json").read_text(encoding="utf-8")) == []
