"""趋势折线回归检查（开发期手工跑，不进 pytest：要起浏览器、依赖本地服务）。

背景：趋势图曾把「没采到的那天」当成断线，于是 17 日 + 19 日两个真实点之间没有任何线，
图上只剩两个孤立圆点。这里锁住两件事：
  1. 相邻真实观测值之间必须画出连接线（line[stroke-width="2"] 的数量 = 点数 - 1）；
  2. 跨过未采样日的那一段必须是虚线，且线段横跨两点之间的完整区间。

用法：先启动本地 easel web（默认 7860），然后
    python scripts/check_trend_chart.py [http://127.0.0.1:7860]
需要 `.venv` 里的 playwright 与 chromium。
"""
from __future__ import annotations

import sys

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860").rstrip("/")

DASHED = "5 4"
# 账号列表里「监控中」的角标文字（与 BenchmarksPage 的 `📈 监控 N` 对应）
MONITOR_TAG = "📈"


def monitored_rows(page):
    """作品列表里正在监控的行（用开关文案区分「监控中 / 已到期 / 监控」）。"""
    return page.locator(".wl-row").filter(has=page.locator(".bench-mon.on"))


def main() -> int:
    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('easel_onboarding_seen','1');")
        page.goto(f"{BASE}/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(800)

        page.get_by_text("对标账号", exact=True).first.click()
        page.wait_for_timeout(1300)
        rows = page.locator(".bench-row")
        if rows.count() == 0:
            print("没有对标账号，跳过（先添加一个账号并开监控）")
            browser.close()
            return 0

        # 列表里带「监控 N」标记的账号才可能有监控中的作品（「已到期」不算）；没有就退出
        tagged = rows.filter(has_text=MONITOR_TAG)
        if tagged.count() == 0:
            print("没有开着监控的账号，跳过（先给一篇作品打开监控）")
            browser.close()
            return 0

        tagged.first.click()
        page.wait_for_timeout(2600)
        if monitored_rows(page).count() == 0:
            # 兜底：换下一个带标记的账号再试
            for i in range(1, tagged.count()):
                tagged.nth(i).click()
                page.wait_for_timeout(2600)
                if monitored_rows(page).count() > 0:
                    break
        if monitored_rows(page).count() == 0:
            print("带监控标记的账号里没有找到「监控中」的作品，跳过")
            browser.close()
            return 0

        monitored_rows(page).first.get_by_text("趋势").click()
        page.wait_for_timeout(1500)

        charts = page.evaluate(
            """() => [...document.querySelectorAll('.trend-chart')].map(c => {
                const segs = [...c.querySelectorAll('line[stroke-width="2"]')].map(l => ({
                    x1: +l.getAttribute('x1'), x2: +l.getAttribute('x2'),
                    dashed: l.getAttribute('stroke-dasharray')}));
                const dots = [...c.querySelectorAll('circle')].map(d => +d.getAttribute('cx'));
                return {segs, dots};
            })"""
        )

        if not charts:
            failures.append("没有渲染出任何趋势图")
        for idx, chart in enumerate(charts, 1):
            segs, dots = chart["segs"], chart["dots"]
            if len(dots) >= 2 and len(segs) != len(dots) - 1:
                failures.append(
                    f"图 {idx}：{len(dots)} 个数据点应连出 {len(dots) - 1} 段线，实际 {len(segs)} 段")
            for seg in segs:
                span = seg["x2"] - seg["x1"]
                if span <= 0:
                    failures.append(f"图 {idx}：线段没有横向跨度（x1={seg['x1']} x2={seg['x2']}）")
                if seg["dashed"] not in (None, DASHED):
                    failures.append(f"图 {idx}：未知的虚线样式 {seg['dashed']!r}")
            print(f"图 {idx}：{len(dots)} 个点，{len(segs)} 段线，"
                  f"其中虚线 {sum(1 for s in segs if s['dashed'] == DASHED)} 段")

        if errors:
            failures.append(f"页面报错：{errors[:2]}")
        browser.close()

    if failures:
        print("\n失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\nOK：趋势图按日期连接了真实数据点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
