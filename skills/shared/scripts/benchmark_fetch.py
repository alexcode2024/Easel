#!/usr/bin/env python3
"""benchmark_fetch.py — 对标账号数据采集（读**他人**公开账号，非本人后台）。

设计要点
--------
1. **平台适配器抽象**：每个平台一个 ``Adapter`` 子类，向 ``ADAPTERS`` 注册；
   新增平台（快手 / 知乎 / B站 …）只需再写一个适配器并加进 ``PLATFORM_ORDER``，
   上层（Web 后端、前端列表/详情）不需要改。能力差异（有没有封面、有没有收藏/评论）
   由适配器用 ``fields`` 声明，缺失一律返回 ``None``，**绝不编造数字**。

2. **已支持的平台（按产品优先级，均已实测）**：
   - ``xiaohongshu`` 小红书：复用 ``xhs_fetch.py`` 读公开主页 SSR
   - ``douyin`` 抖音：拦页面自身的 profile/other 与 aweme/post 响应
   - ``tiktok`` TikTok：拦页面自身的 item_list 响应，SSR 兜底（需能访问 TikTok 的网络）
   其余平台注册为占位适配器：明确声明「不支持自动抓取」，可手动登记账号。

3. **本文件不做绕过平台风控的事**：能拿到的拿，拿不到就如实标注
   （``coverage`` / ``warnings``），命中风控立即停止并报 ``risk``。

4. 归因层口径：``followers/likes/favorites/comments`` 是**账号级累计**，
   ``works[].likes/favorites/comments/plays`` 是**作品级**。平台上不公开的一律 ``None``。

用法
----
    python benchmark_fetch.py platforms
    python benchmark_fetch.py fetch --platform xiaohongshu --target <24位user_id|主页链接>
                                   [--limit 12] [--cover-dir DIR] [--json OUT]
    python benchmark_fetch.py detail --platform xiaohongshu --target <user_id> \
                                   --work-id <note_id> [--xsec-token TOKEN]
    python benchmark_fetch.py selftest

输出：单行 JSON（与 account_stats.py 同风格，供 Web 后端解析）。

退出码：0 成功、2 风控熔断、3 取不到数据、4 输入非法、5 平台不支持自动抓取。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
XHS_FETCH = SCRIPT_DIR / "xhs_fetch.py"
DEFAULT_PROFILE_ROOT = Path.home() / ".easel-browser-profiles"

# 读页面用的浏览器参数（对齐 account_stats.LAUNCH_ARGS；这里**不禁图**，要封面）
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", "--mute-audio",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

EXIT_OK, EXIT_RISK, EXIT_EMPTY, EXIT_INPUT, EXIT_UNSUPPORTED = 0, 2, 3, 4, 5


# --------------------------------------------------------------------------- #
# 纯函数（可单测）
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"([\d.]+)\s*([万亿wkm]?)", re.IGNORECASE)


def parse_count(text) -> tuple[int | None, bool]:
    """把平台展示的量级字符串转成 ``(数值, 是否只是下界)``。

    小红书/抖音主页给的是展示量级而非精确值：
    ``"1.3万" -> (13000, False)``、``"1万+" -> (10000, True)``、
    ``"143" -> (143, False)``、``"" / None -> (None, False)``。

    下界标记用于前端标注「量级」而不是伪精确数字。
    """
    if text is None:
        return None, False
    if isinstance(text, (int, float)):
        return int(text), False
    s = str(text).strip().replace(",", "").replace("，", "")
    if not s:
        return None, False
    m = _NUM_RE.search(s)
    if not m:
        return None, False
    try:
        base = float(m.group(1))
    except ValueError:
        return None, False
    unit = (m.group(2) or "").lower()
    mult = {"": 1, "w": 10_000, "k": 1_000, "m": 1_000_000,
            "万": 10_000, "亿": 100_000_000}.get(unit, 1)
    value = int(round(base * mult))
    lower_bound = "+" in s or "多" in s
    return value, lower_bound


def parse_target_xhs(raw: str) -> str:
    """小红书：接受 24 位 user_id 或主页链接，返回裸 user_id。"""
    s = (raw or "").strip()
    m = re.search(r"/user/profile/([0-9a-fA-F]{24})", s)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[0-9a-fA-F]{24}", s):
        return s.lower()
    raise ValueError("小红书需要 24 位 user_id，或主页链接 …/user/profile/<user_id>")


def parse_target_douyin(raw: str) -> str:
    """抖音：接受 sec_uid 或主页链接，返回裸 sec_uid。

    抖音号（如 ``dyabc123``）无法离线换成 sec_uid，需要主页链接——
    这是平台限制，不是本工具的偷懒：给出明确报错好过猜一个差不多的号。
    """
    s = (raw or "").strip()
    m = re.search(r"douyin\.com/user/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"MS4wLjAB[A-Za-z0-9_\-]+", s):
        return s
    raise ValueError(
        "抖音需要主页链接（douyin.com/user/<sec_uid>）或 sec_uid；"
        "只给「抖音号」无法定位账号，请在抖音里打开主页复制链接")


def median(values: list[int | None]) -> int | None:
    """中位数（忽略 None/负数）；空列表返回 None。"""
    xs = sorted(int(v) for v in values if isinstance(v, int) and v >= 0)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) // 2


# 爆款判据（与 skills/builtin/skill-competitor-analysis/references/viral-patterns.md 一致）
BURST_MULTIPLE = 3
BURST_BASELINE = 30          # 中位数基准取最新多少条（用户口径：最近 30 条）


def mark_burst(works: list["Work"]) -> int | None:
    """给作品打「爆款」标记：点赞 ≥ 该账号点赞中位数 × 3。

    判据来自仓库既有方法论文档；样本 <3 条时不标记（中位数不可靠）。
    返回本账号的点赞中位数（供前端展示基准）。
    """
    # 中位数基准固定取「最新 30 条」（按发布时间倒序）：口径稳定，不随抓到多少条漂移。
    # 标记本身仍然覆盖全部作品 —— 用近期基准去量每一条，才看得出谁是真爆款。
    recent = sorted(works, key=lambda w: w.published_at_ms or 0, reverse=True)[:BURST_BASELINE]
    likes = [w.likes for w in (recent or works) if isinstance(w.likes, int)]
    med = median(likes)
    enough = len(likes) >= 3 and med is not None and med > 0
    for w in works:
        w.is_burst = bool(enough and isinstance(w.likes, int) and w.likes >= med * BURST_MULTIPLE)
        w.median_likes = med
    return med


def _https(url: str) -> str:
    return ("https://" + url[7:]) if url.startswith("http://") else (url or "")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Metric:
    """账号级/作品级的一个数值：原始展示串 + 解析值 + 是否只是下界。"""
    raw: str = ""
    num: int | None = None
    lower_bound: bool = False

    @classmethod
    def of(cls, text) -> "Metric | None":
        num, lb = parse_count(text)
        if num is None and not str(text or "").strip():
            return None
        return cls(raw=str(text or ""), num=num, lower_bound=lb)


@dataclass
class Work:
    id: str = ""
    title: str = ""
    url: str = ""
    cover_url: str = ""            # 平台远程封面（本地化前的地址）
    cover: str = ""                # 本地缓存相对路径（由 --cover-dir 下载后填）
    published_at_ms: int | None = None
    likes: int | None = None
    likes_raw: str = ""
    favorites: int | None = None
    comments: int | None = None
    shares: int | None = None
    plays: int | None = None       # 播放量（TikTok/抖音有，小红书公开主页没有）
    is_top: bool = False           # 置顶
    is_burst: bool = False         # 爆款（>= 该账号点赞中位数 × 3）
    median_likes: int | None = None
    detail_fetched: bool = False   # 是否已深挖过详情（收藏/评论）
    detail_available: bool = False  # 平台是否给了足够信息（id + token）去做深挖
    extra: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    platform: str = ""
    platform_name: str = ""
    account_id: str = ""
    nickname: str = ""
    avatar: str = ""
    bio: str = ""
    home_url: str = ""
    followers: Metric | None = None
    likes: Metric | None = None
    favorites: Metric | None = None
    comments: Metric | None = None
    posts: int | None = None
    works: list[Work] = field(default_factory=list)
    coverage: str = ""             # 实际覆盖说明（诚实标注，如「SSR 首页 12 篇」）
    source: str = ""               # 数据来源说明
    logged_in: bool = True
    risk: str | None = None
    warnings: list[str] = field(default_factory=list)
    median_likes: int | None = None
    fetched_at: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 适配器抽象
# --------------------------------------------------------------------------- #
class Unsupported(Exception):
    """该平台不支持这个操作（上层据此提示用户，而不是报错）。"""


@dataclass
class Adapter:
    """平台适配器基类：新增平台只需继承并实现 parse_target / fetch。

    ``fields`` 声明本平台**能**提供哪些字段，前端据此决定列是否可点、
    以及缺失值显示「—」还是给出「深挖」按钮。

    ``order`` 是产品优先级（越小越靠前），前端平台选择器按它排序：
    小红书(0) → 抖音(1) → TikTok(2) → 其余(100)。
    """
    key: str = ""
    name: str = ""
    auto: bool = False             # 是否支持自动抓取
    experimental: bool = False     # 未实测/易被风控，前端需提示
    order: int = 100               # 产品优先级（排序用）
    id_label: str = "账号ID"
    id_hint: str = ""
    fields: dict = field(default_factory=dict)
    fetch_note: str = ""           # 抓取代价/边界说明，直接展示给用户

    def parse_target(self, raw: str) -> str:
        raise NotImplementedError

    def fetch(self, target: str, *, limit: int, profile_root: Path,
              proxy: str | None, cover_dir: Path | None,
              verbose: bool, cookie_file: str | None = None) -> Snapshot:
        raise Unsupported(f"{self.name}暂不支持自动抓取")

    def fetch_work_detail(self, target: str, work_id: str, *, profile_root: Path,
                          proxy: str | None, extra: dict | None = None,
                          verbose: bool = False, cookie_file: str | None = None) -> Work:
        raise Unsupported(f"{self.name}暂不支持单条作品深挖")

    def describe(self) -> dict:
        return {
            "platform": self.key, "name": self.name, "auto": self.auto,
            "experimental": self.experimental, "order": self.order,
            "id_label": self.id_label, "id_hint": self.id_hint,
            "fields": self.fields, "note": self.fetch_note,
        }


ADAPTERS: dict[str, Adapter] = {}
# 产品优先级：新增平台时把 key 加到这里（不必改 Web 后端与前端）
PLATFORM_ORDER: tuple[str, ...] = ("xiaohongshu", "douyin", "tiktok")


def register(adapter: Adapter) -> Adapter:
    if adapter.key in ADAPTERS:
        raise ValueError(f"平台适配器重复注册：{adapter.key}")
    ADAPTERS[adapter.key] = adapter
    return adapter


def all_adapters() -> list[Adapter]:
    """按产品优先级排序的适配器列表（未列入 PLATFORM_ORDER 的排后面，注册序稳定）。"""
    def sort_key(item: tuple[int, Adapter]):
        idx, ad = item
        try:
            return (0, PLATFORM_ORDER.index(ad.key))
        except ValueError:
            return (1, idx)
    return [ad for _, ad in sorted(enumerate(ADAPTERS.values()), key=sort_key)]


def get_adapter(platform: str) -> Adapter:
    ad = ADAPTERS.get(platform or "")
    if ad is None:
        raise ValueError(f"未知平台：{platform}（已注册：{', '.join(ADAPTERS)}）")
    return ad


# --------------------------------------------------------------------------- #
# 封面本地缓存
# --------------------------------------------------------------------------- #
def _cover_ext(url: str, content_type: str = "") -> str:
    ct = (content_type or "").lower()
    for key, ext in (("jpeg", ".jpg"), ("jpg", ".jpg"), ("png", ".png"),
                     ("webp", ".webp"), ("gif", ".gif"), ("avif", ".avif")):
        if key in ct:
            return ext
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def download_cover(url: str, dest_dir: Path, stem: str, referer: str = "") -> str:
    """下载封面到 ``dest_dir/<stem>.<ext>``，返回相对 dest_dir 的文件名；失败返回 ""。

    图片走平台 CDN，普通 GET 即可（前端展示时也是 no-referrer）。
    下载失败**不是**致命错误：保留远程 URL 让前端直接显示。
    """
    url = _https(url)
    if not url:
        return ""
    for old in dest_dir.glob(f"{stem}.*"):
        return old.name  # 已缓存过，直接复用
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        **({"Referer": referer} if referer else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - 固定 https
            data = resp.read(8 * 1024 * 1024)
            ctype = resp.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    if not data:
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{stem}{_cover_ext(url, ctype)}"
    tmp = dest_dir / f"{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, dest_dir / name)
    return name


def _safe_stem(work_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", work_id or "work")[:80]


def cover_stem(cover_url: str, *fallback_parts) -> str:
    """给封面缓存文件起一个稳定且不冲突的名字。

    小红书封面 URL 末段就是图片 fileId（``…/<fileId>!nc_n_nwebp_prv_1``），
    天然唯一；**不能用标题**（同一账号大量作品 ID 为空、标题又含中文，
    用标题会互相覆盖）。取不到 fileId 时退回内容哈希。
    """
    path = urllib.parse.urlparse(_https(cover_url)).path
    seg = path.rstrip("/").rsplit("/", 1)[-1]
    file_id = seg.split("!", 1)[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", file_id or ""):
        return file_id[:64]
    import hashlib
    digest = hashlib.sha1("|".join(str(p) for p in fallback_parts).encode("utf-8"))
    return digest.hexdigest()[:20]


# --------------------------------------------------------------------------- #
# 小红书适配器（复用 xhs_fetch.py：唯一已实测的他人账号路径）
# --------------------------------------------------------------------------- #
class XiaohongshuAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__(
            key="xiaohongshu", name="小红书", auto=True, experimental=False, order=0,
            id_label="小红书 user_id",
            id_hint="粘贴主页链接（…/user/profile/<24位>）或 24 位 user_id",
            fields={
                "followers": True, "likes": True, "favorites": False, "comments": False,
                "posts": True, "cover": True, "work_likes": True,
                # 作品级收藏/评论：主页列表里**没有**（SSR 只给点赞），但可以逐条打开作品页读
                # （登录态下主页会给作品 ID；游客态没有 ID 就读不了详情，故为「按需深挖」）。
                "work_favorites": True,
                "work_comments": True,
                "work_published_at": True,
                "work_detail_manual": True,   # 需要用户按需触发，不放进「刷新」默认流程
            },
            fetch_note="带登录态抓公开主页 SSR：一次请求可拿一批（登录态实测 30 篇），"
                       "默认 8–15s 随机节流，实际耗时受排队和网络影响；不翻页（翻页需签名，项目已明确不做）。"
                       "作品级收藏/评论不在列表里，需要按需逐条打开作品页读取（每条一次页面加载）。"
                       "**务必用有头浏览器**：无头内核会被小红书判 300012（实测）。",
        )

    def parse_target(self, raw: str) -> str:
        return parse_target_xhs(raw)

    def fetch(self, target, *, limit, profile_root, proxy, cover_dir, verbose,
              cookie_file=None):
        # 上限 50：主页 SSR 一次只给 30 条，多出来的走签名分页接口（本项目约定不做全量）
        limit = max(1, min(int(limit or 50), 50))
        out_file = (Path(tempfile.gettempdir())
                    / f"_bm_xhs_{os.getpid()}_{random.randint(0, 9999)}.json")
        cmd = [sys.executable, str(XHS_FETCH), "--user-id", target,
               "--limit", str(limit), "--quiet", "--json", str(out_file)]
        if cookie_file:
            # 有 cookie 就走纯 HTTP（不用浏览器；实测能拿到真实粉丝 + 作品 ID）
            cmd += ["--cookie-file", str(cookie_file)]
        try:
            proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR.parents[2]),
                                  capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("抓取超时（>300s），已停止；请稍后重试")
        data = {}
        if out_file.is_file():
            try:
                data = json.loads(out_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
            finally:
                out_file.unlink(missing_ok=True)

        risk = data.get("risk_control")
        if proc.returncode == EXIT_RISK or risk:
            snap = Snapshot(platform=self.key, platform_name=self.name,
                            account_id=target, risk=str(risk or "命中风控信号"),
                            logged_in=True, fetched_at=int(time.time()),
                            source="xhs_fetch.py")
            return snap
        if proc.returncode == EXIT_INPUT:
            raise ValueError((proc.stderr or proc.stdout or "输入不合法").strip()[:200])
        prof = data.get("profile") or {}
        if not prof and not data.get("notes"):
            raise RuntimeError(
                "未取到主页数据（未登录 / 平台维护 / 页面结构变化）："
                + ((proc.stderr or "").strip().splitlines() or ["无输出"])[-1][:160])

        snap = Snapshot(platform=self.key, platform_name=self.name,
                        account_id=target,
                        nickname=prof.get("nickname") or "",
                        avatar="",
                        bio=prof.get("desc") or "",
                        home_url=f"https://www.xiaohongshu.com/user/profile/{target}",
                        followers=Metric.of(prof.get("fans_raw")),
                        # 累计获赞/收藏：用 SSR 的精确整数字段（展示串在游客态是占位符）
                        likes=(Metric.of(prof.get("liked_exact"))
                               or Metric.of(prof.get("interaction_raw"))),
                        favorites=Metric.of(prof.get("collected_exact")),
                        comments=None,
                        posts=prof.get("posted_exact"),
                        source="xhs_fetch.py（SSR 首页）",
                        fetched_at=int(time.time()))

        # 游客态（登录态失效）：SSR 的粉丝/获赞展示串会回落成 "1万+" 这类骨架值，
        # 拿它当真实数据就是误导。精确字段仍然可用，所以只把展示串相关的值留空并告警。
        guest = bool(prof.get("guest")) or prof.get("logged_in_per_ssr") is False
        snap.logged_in = not guest
        if guest:
            snap.followers = None
            snap.warnings.append(
                "小红书当前被判游客态（cookie 失效或被平台降级）：粉丝数平台只给「1万+」这类占位值，"
                "已留空而不是显示错的数字；累计获赞/收藏来自精确字段仍然有效。"
                "作品列表也拿不到作品 ID（无原文链接、无法深挖）。"
                "处理办法：在自己浏览器里重新登录小红书 → 复制新 cookie → 粘贴到"
                "「对标账号」页右上角的「🔑 Cookie」；若短时间内请求偏多，平台会把会话降级数小时，"
                "等一段时间再试即可——本工具不绕过平台风控")
        red_id = prof.get("red_id") or ""
        if red_id:
            snap.warnings.append(f"小红书号：{red_id}")
        hint = prof.get("note_total_hint")
        if isinstance(hint, (str, int)) and str(hint).strip():
            snap.warnings.append(f"主页标注作品数：{hint}（本次只取一次请求能拿到的批次）")

        works: list[Work] = []
        for n in (data.get("notes") or [])[:limit]:
            nid = n.get("note_id") or ""
            token = n.get("xsec_token") or ""
            work = Work(
                id=nid,
                title=(n.get("title") or "").strip() or "(无标题)",
                url=(f"https://www.xiaohongshu.com/explore/{nid}"
                     f"?xsec_token={token}&xsec_source=pc_user") if nid else "",
                cover_url=_https(n.get("cover") or ""),
                published_at_ms=n.get("time_ms") or None,
                likes=n.get("liked_count_num"),
                likes_raw=str(n.get("liked_count") or ""),
                is_top=bool(n.get("sticky")),
                extra={"xsec_token": token, "type": n.get("type") or ""},
            )
            # 有 id 才有详情页可读；平台不给 id 时明确标记，前端据此禁用「深挖」
            work.detail_available = bool(nid and token)
            works.append(work)
        snap.works = works
        paging = data.get("paging") or {}
        if paging:
            pages = paging.get("pages") or 1
            snap.coverage = (f"主页 SSR + 签名分页 {pages} 页，共 {len(works)} 条"
                             f"（本项目最多取最新 50 条）")
        elif data.get("cookie_used"):
            snap.coverage = f"主页 SSR 批次 {len(works)} 条（分页未启用）"
        else:
            snap.coverage = f"主页 SSR 批次 {len(works)} 条（未登录：只有这一批，且无作品 ID）"
        snap.median_likes = mark_burst(works)

        if cover_dir is not None:
            missing = 0
            for w in works:
                if not w.cover_url:
                    continue
                stem = cover_stem(w.cover_url, w.id, w.title, w.published_at_ms)
                name = download_cover(w.cover_url, Path(cover_dir), stem)
                if name:
                    w.cover = name
                else:
                    missing += 1
            if missing:
                snap.warnings.append(f"{missing} 张封面下载失败（详情页仍显示远程图，可能过期）")
        return snap

    def fetch_work_detail(self, target, work_id, *, profile_root, proxy, extra=None,
                          verbose=False, cookie_file=None):
        """深挖单条：读该作品的收藏/评论（**纯 HTTP，不用浏览器、不用登录**）。

        小红书笔记页的 ``__INITIAL_STATE__`` 里就带着 ``note.noteDetailMap``，
        但它是**带 JS 语法**的（``new Map([])``），JSON 解析器读不了；另外普通 HTTP
        客户端的 TLS 指纹会被判 300012。所以这里用 curl_cffi 伪装 Chrome 指纹 + YAML 解析
        （做法参考 XHS-Downloader 项目），1 条 = 1 个 HTTP 请求，秒级完成。
        """
        extra = extra or {}
        token = extra.get("xsec_token") or ""
        if not work_id:
            raise Unsupported(
                "小红书未返回该作品的笔记 ID，无法打开详情页；"
                "这是游客态的限制（登录后主页会给出作品 ID），无法通过重试解决")
        cookie = ""
        if cookie_file:
            try:
                cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
            except OSError:
                cookie = ""
        detail = _xhs_module().fetch_note_detail(work_id, token, cookie=cookie)
        work = Work(id=work_id, detail_fetched=True)
        if not detail:
            work.extra["detail_note"] = (
                "没读到互动数据（笔记可能已删除/仅自己可见，或页面结构变化）")
            return work
        work.title = detail.get("title") or ""
        work.likes = detail.get("liked")
        work.favorites = detail.get("collected")
        work.comments = detail.get("comment")
        work.shares = detail.get("share")
        return work


def _xhs_module():
    import xhs_fetch  # 同目录；复用其风控标记，避免两处维护同一份黑名单
    return xhs_fetch


def _browser_channel() -> str | None:
    """有头模式的真实浏览器内核（Chrome/Edge）——与 xhs_publish._browser_channel 同一套判断。"""
    getter = getattr(_xhs_module(), "_browser_channel", None)
    return getter() if getter else None


# --------------------------------------------------------------------------- #
# 抖音适配器（新写、未实测：借页面自己的手发请求）
# --------------------------------------------------------------------------- #
DOUYIN_PROFILE_API = "/aweme/v1/web/user/profile/other/"
DOUYIN_POST_API = "/aweme/v1/web/aweme/post/"


class DouyinAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__(
            key="douyin", name="抖音", auto=True, experimental=False, order=1,
            id_label="抖音 sec_uid",
            id_hint="粘贴抖音主页链接（douyin.com/user/<sec_uid>）；只给抖音号无法定位",
            fields={
                "followers": True, "likes": True, "favorites": False, "comments": False,
                "posts": True, "cover": True, "work_likes": True,
                "work_favorites": True, "work_comments": True,
                "work_published_at": True,
            },
            fetch_note="已实测可用（2026-10）：带登录态打开他人主页，拦页面自身发出的 "
                       "profile/other 与 aweme/post 响应，账号级粉丝/累计获赞 + 作品级"
                       "点赞/收藏/评论/分享一次拿全。**播放量不是公开数据**（接口恒返回 0，"
                       "按未知处理显示「—」）。登录态来自「自有账号」页的抖音扫码。",
        )

    def parse_target(self, raw: str) -> str:
        return parse_target_douyin(raw)

    def fetch(self, target, *, limit, profile_root, proxy, cover_dir, verbose,
              cookie_file=None):
        """打开他人主页，拦页面自身发出的接口响应。

        ``cookie_file`` 只为与 ``cmd_fetch`` 的调用约定对齐（每个适配器都收这个关键字）：
        抖音的登录态来自「自有账号」页扫码后的持久化浏览器 profile，不走 cookie 文件。
        """
        limit = max(1, min(int(limit or 12), 50))
        from playwright.sync_api import sync_playwright

        profile = Path(profile_root) / "DouyinProfile"
        captured: dict[str, dict] = {}
        pages_seen: list[str] = []

        with sync_playwright() as p:
            launch: dict = {"headless": True, "args": LAUNCH_ARGS, "user_agent": UA,
                            "viewport": {"width": 1440, "height": 900}}
            if proxy:
                launch["proxy"] = {"server": proxy}
            ctx = p.chromium.launch_persistent_context(str(profile), **launch)
            import platform_access
            platform_access.install_browser_guard(ctx, platform=self.key)
            try:
                page = ctx.new_page()

                def on_response(resp):
                    u = resp.url
                    try:
                        if DOUYIN_PROFILE_API in u and "profile" not in captured:
                            captured["profile"] = resp.json()
                        elif DOUYIN_POST_API in u and "post" not in captured:
                            captured["post"] = resp.json()
                    except Exception:  # noqa: BLE001 - 非 JSON / 已释放，忽略
                        pass

                page.on("response", on_response)
                page.goto(f"https://www.douyin.com/user/{target}",
                          wait_until="domcontentloaded", timeout=45000)
                deadline = time.time() + 30
                while time.time() < deadline and "post" not in captured:
                    page.wait_for_timeout(500)
                    if _looks_blocked(page):
                        pages_seen.append("风控/登录墙")
                        break
                    if "post" not in captured and not pages_seen:
                        # 首屏可能停在「作品」以外的 tab：点一下「作品」再等
                        _nudge_works_tab(page)
                        pages_seen.append("nudged")

                if "post" not in captured:
                    captured.update(_router_data_fallback(page, target))

                snap = self._build_snapshot(target, captured, limit, cover_dir, verbose)
                if not snap.works:
                    snap.warnings.append(
                        "抖音未返回作品列表：可能被风控、登录态失效或页面改版；"
                        "本工具不做绕过，请稍后在普通浏览器确认主页可正常访问")
                if _looks_blocked(page):
                    snap.warnings.append("页面出现验证码/登录墙，数据可能不完整")
                return snap
            finally:
                ctx.close()

    @staticmethod
    def _build_snapshot(target, captured, limit, cover_dir, verbose) -> Snapshot:
        prof = captured.get("profile") or {}
        user = prof.get("user") if isinstance(prof.get("user"), dict) else prof
        post = captured.get("post") or {}
        items = post.get("aweme_list") or post.get("data") or []

        snap = Snapshot(platform="douyin", platform_name="抖音", account_id=target,
                        nickname=str((user or {}).get("nickname") or ""),
                        avatar=_douyin_avatar(user or {}),
                        bio=str((user or {}).get("signature") or ""),
                        home_url=f"https://www.douyin.com/user/{target}",
                        followers=Metric.of((user or {}).get("follower_count")),
                        likes=Metric.of((user or {}).get("total_favorited")),
                        favorites=None, comments=None,
                        posts=(user or {}).get("aweme_count"),
                        logged_in=bool(user),
                        source="抖音主页接口（页面内响应拦截）",
                        fetched_at=int(time.time()))

        works: list[Work] = []
        for raw in (items or [])[:limit]:
            if not isinstance(raw, dict):
                continue
            st = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
            aid = str(raw.get("aweme_id") or raw.get("id") or "")
            works.append(Work(
                id=aid,
                title=str(raw.get("desc") or "").strip() or "(无标题)",
                url=f"https://www.douyin.com/video/{aid}" if aid else "",
                cover_url=_douyin_cover(raw),
                published_at_ms=_epoch_ms(raw.get("create_time")),
                likes=_as_int(st.get("digg_count")),
                favorites=_as_int(st.get("collect_count")),
                comments=_as_int(st.get("comment_count")),
                shares=_as_int(st.get("share_count")),
                plays=_public_plays(st.get("play_count")),
                is_top=bool(raw.get("is_top")),
            ))
        snap.works = works
        snap.coverage = f"主页作品 {len(works)} 条" + ("（未翻页）" if len(works) >= limit else "")
        snap.median_likes = mark_burst(works)

        if cover_dir is not None:
            for w in works:
                if not w.cover_url:
                    continue
                name = download_cover(w.cover_url, Path(cover_dir), _safe_stem(w.id),
                                      referer="https://www.douyin.com/")
                if name:
                    w.cover = name
        return snap

    def fetch_work_detail(self, target, work_id, *, profile_root, proxy, extra=None,
                          verbose=False, cookie_file=None):
        """抖音作品级数据（点赞/收藏/评论）在列表接口里就有，无需再深挖。"""
        raise Unsupported("抖音的作品收藏/评论数随列表一起返回，无需单独深挖")


def _epoch_ms(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n * 1000 if n < 10_000_000_000 else n


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _public_plays(v) -> int | None:
    """抖音网页接口对**他人**作品的 play_count 恒为 0（播放量不是公开数据）。

    显示 0 等于编造「这条没人看」，所以 0 一律当未知（None → 前端显示「—」）。
    """
    n = _as_int(v)
    return n if n else None


def _douyin_cover(raw: dict) -> str:
    video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
    for holder in (video.get("cover"), video.get("origin_cover"), video.get("dynamic_cover")):
        if isinstance(holder, dict):
            urls = holder.get("url_list") or []
            if urls:
                return _https(str(urls[0]))
    return ""


def _douyin_avatar(user: dict) -> str:
    for key in ("avatar_larger", "avatar_168x168", "avatar_thumb"):
        holder = user.get(key)
        if isinstance(holder, dict) and holder.get("url_list"):
            return _https(str(holder["url_list"][0]))
    return ""


def _looks_blocked(page) -> bool:
    """粗判验证码/登录墙。只读页面状态，不做任何点击绕过。"""
    try:
        url = (page.url or "").lower()
        if "/login" in url or "passport" in url:
            return True
        text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
        markers = ("验证码", "滑动验证", "安全验证", "请完成验证", "captcha", "verify")
        low = str(text or "")
        return any(m in low for m in markers)
    except Exception:  # noqa: BLE001
        return False


def _nudge_works_tab(page) -> None:
    try:
        for sel in ('div[data-e2e="user-tab"] >> text=作品',
                    'span:has-text("作品")', 'div:has-text("作品")'):
            el = page.query_selector(sel)
            if el:
                el.click(timeout=1500)
                page.wait_for_timeout(800)
                return
    except Exception:  # noqa: BLE001
        pass


def _router_data_fallback(page, target) -> dict:
    """兜底：部分抖音页面把首屏数据放在 ``window._ROUTER_DATA``。"""
    try:
        data = page.evaluate("() => window._ROUTER_DATA || null")
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    loader = data.get("loaderData") or {}
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        if "userInfo" in value and isinstance(value["userInfo"], dict):
            info = value["userInfo"].get("user") or value["userInfo"]
            if isinstance(info, dict) and (info.get("nickname") or info.get("follower_count")):
                out.setdefault("profile", {"user": info})
        for key in ("post", "awemeList", "aweme_list"):
            if isinstance(value.get(key), dict) and value[key].get("aweme_list"):
                out.setdefault("post", value[key])
    return out


register(XiaohongshuAdapter())
register(DouyinAdapter())


# --------------------------------------------------------------------------- #
# TikTok 适配器（新写、未实测：SSR JSON 优先，XHR 拦截兜底）
# --------------------------------------------------------------------------- #
TIKTOK_SSR_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
TIKTOK_POST_API = "/api/post/item_list/"
TIKTOK_ITEM_API = "/api/item/list/"          # 旧版/备用作品列表接口
TIKTOK_USER_API = "/api/user/detail/"


def parse_target_tiktok(raw: str) -> str:
    """TikTok：接受 @handle、handle 或主页链接，返回裸 handle（不含 @）。"""
    s = (raw or "").strip()
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", s)
    if m:
        return m.group(1)
    if s.startswith("@"):
        s = s[1:]
    if re.fullmatch(r"[A-Za-z0-9_.]{2,24}", s):
        return s
    raise ValueError("TikTok 需要主页链接（tiktok.com/@handle）或 @handle")


def parse_tiktok_ssr(html: str) -> tuple[dict, list]:
    """从主页 HTML 的 ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` 里取 (userInfo, itemList)。

    这是 TikTok 主页的 SSR 数据（一次页面请求即可），比逐个接口拼更稳。
    结构（2026-10 核对页面脚本 id 与字段名）::

        {"__DEFAULT_SCOPE__": {"webapp.user-detail": {
            "userInfo": {"user": {...}, "stats": {...}},
            "itemList": [{"id","desc","createTime","stats":{...},"video":{...}}]}}}

    取不到就返回 ({}, [])——调用方据此降级，不猜。
    """
    if not html:
        return {}, []
    m = re.search(
        r'<script[^>]+id="' + re.escape(TIKTOK_SSR_ID) + r'"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}, []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    scope = data.get("__DEFAULT_SCOPE__")
    if not isinstance(scope, dict):
        return {}, []
    detail = scope.get("webapp.user-detail")
    if not isinstance(detail, dict):
        return {}, []
    user_info = detail.get("userInfo") if isinstance(detail.get("userInfo"), dict) else {}
    # 实测：itemList 嵌在 userInfo 下面（不在 detail 一级），两种都认
    items = user_info.get("itemList") or detail.get("itemList")
    if not isinstance(items, list):
        items = []
    return user_info, [it for it in items if isinstance(it, dict)]


def snapshot_from_tiktok(handle: str, user_info: dict, items: list,
                         source: str) -> Snapshot:
    """把 TikTok 的 userInfo/itemList 归一成统一 Snapshot（纯函数，可单测）。

    ``user/detail`` 接口不一定触发，但每个 item 自带 ``author`` / ``authorStats``，
    所以账号级数据可以从第一条作品兜底补齐（有则用 userInfo，权威优先）。
    """
    user = user_info.get("user") if isinstance(user_info.get("user"), dict) else {}
    stats = user_info.get("stats") if isinstance(user_info.get("stats"), dict) else {}
    if not user or not stats:
        first = items[0] if items and isinstance(items[0], dict) else {}
        if not user and isinstance(first.get("author"), dict):
            user = first["author"]
        if not stats and isinstance(first.get("authorStats"), dict):
            stats = first["authorStats"]
    snap = Snapshot(
        platform="tiktok", platform_name="TikTok",
        account_id=user.get("uniqueId") or handle,
        nickname=str(user.get("nickname") or ""),
        avatar=_first_url(user.get("avatarLarger") or user.get("avatarMedium")
                          or user.get("avatarThumb")),
        bio=str(user.get("signature") or ""),
        home_url=f"https://www.tiktok.com/@{user.get('uniqueId') or handle}",
        followers=Metric.of(stats.get("followerCount")),
        likes=Metric.of(stats.get("heartCount")),      # heartCount = 累计获赞
        favorites=None, comments=None,
        posts=_as_int(stats.get("videoCount")),
        source=source,
        fetched_at=int(time.time()),
    )
    works: list[Work] = []
    for raw in items:
        st = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
        wid = str(raw.get("id") or "")
        uniq = user.get("uniqueId") or handle
        works.append(Work(
            id=wid,
            title=str(raw.get("desc") or "").strip() or "(无标题)",
            url=f"https://www.tiktok.com/@{uniq}/video/{wid}" if wid else "",
            cover_url=_tiktok_cover(video),
            published_at_ms=_epoch_ms(raw.get("createTime")),
            likes=_as_int(st.get("diggCount")),
            favorites=_as_int(st.get("collectCount")),
            comments=_as_int(st.get("commentCount")),
            shares=_as_int(st.get("shareCount")),
            plays=_as_int(st.get("playCount")),
        ))
    snap.works = works
    return snap


def _first_url(holder) -> str:
    """取图片地址：TikTok 同一个字段**两种形态都有** —— 有的接口给纯字符串，
    SSR 给 ``{"urlList": [...]}``，所以两种都要认（只认一种会让封面/头像静默为空）。"""
    if isinstance(holder, str):
        return _https(holder)
    if isinstance(holder, dict):
        urls = holder.get("urlList") or []
        if urls:
            return _https(str(urls[0]))
        return _https(str(holder.get("url") or ""))
    return ""


def _tiktok_cover(video: dict) -> str:
    for key in ("cover", "originCover", "dynamicCover", "zoomCover"):
        url = _first_url(video.get(key))
        if url:
            return url
    return ""


class TiktokAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__(
            key="tiktok", name="TikTok", auto=True, experimental=False, order=2,
            id_label="TikTok @handle",
            id_hint="粘贴主页链接（tiktok.com/@handle）或 @handle",
            fields={
                "followers": True, "likes": True, "favorites": False, "comments": False,
                "posts": True, "cover": True, "work_likes": True,
                "work_favorites": True, "work_comments": True,
                "work_published_at": True, "work_plays": True,
            },
            fetch_note="已实测可用（2026-10）：打开主页拦页面自身的 item_list 响应，"
                       "作品级播放/点赞/收藏/评论齐全，账号级粉丝与累计获赞来自 heartCount。"
                       "**需要能访问 TikTok 的网络**（默认走 EASEL_PROXY/https_proxy）；"
                       "匿名即可读公开主页，有 ~/.easel-browser-profiles/TikTokProfile 时带登录态。",
        )

    def parse_target(self, raw: str) -> str:
        return parse_target_tiktok(raw)

    def fetch(self, target, *, limit, profile_root, proxy, cover_dir, verbose,
              cookie_file=None):
        """打开他人主页，拦页面自身的 item_list 响应。

        ``cookie_file`` 只为与 ``cmd_fetch`` 的调用约定对齐（每个适配器都收这个关键字）：
        TikTok 的登录态走 ``~/.easel-browser-profiles/TikTokProfile`` 持久化 profile，
        匿名也能读公开主页，所以这里不消费 cookie 文件。
        """
        limit = max(1, min(int(limit or 20), 50))
        from playwright.sync_api import sync_playwright

        profile = Path(profile_root) / "TikTokProfile"
        persistent = profile.is_dir()
        captured: dict[str, dict] = {}
        html = ""
        blocked = False

        with sync_playwright() as p:
            launch: dict = {"headless": True, "args": LAUNCH_ARGS, "user_agent": UA,
                            "viewport": {"width": 1440, "height": 900}, "locale": "en-US"}
            if proxy:
                launch["proxy"] = {"server": proxy}
            browser = None
            if persistent:
                ctx = p.chromium.launch_persistent_context(str(profile), **launch)
            else:
                # 没有 TikTok 登录态也能读公开主页，别为它硬造一个 profile 目录
                browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS,
                                            proxy=launch.get("proxy"))
                ctx = browser.new_context(user_agent=UA, locale="en-US",
                                          viewport={"width": 1440, "height": 900})
            import platform_access
            platform_access.install_browser_guard(ctx, platform=self.key)
            try:
                page = ctx.new_page()

                def on_response(resp):
                    u = resp.url
                    try:
                        if (TIKTOK_POST_API in u or TIKTOK_ITEM_API in u) and "post" not in captured:
                            captured["post"] = resp.json()
                        elif TIKTOK_USER_API in u and "user" not in captured:
                            captured["user"] = resp.json()
                    except Exception:  # noqa: BLE001 - 非 JSON / 已释放
                        pass

                page.on("response", on_response)
                page.goto(f"https://www.tiktok.com/@{target}",
                          wait_until="domcontentloaded", timeout=45000)
                # 实测：作品列表是懒加载的，首屏不滚就不发 item_list 请求
                # （同一主页三次抓取，只有滚动过的两次拿到作品），所以到点就滚一下。
                deadline = time.time() + 30
                nudges = 0
                while time.time() < deadline and "post" not in captured:
                    page.wait_for_timeout(500)
                    if _tiktok_blocked(page):
                        blocked = True
                        break
                    waited = 30 - int(deadline - time.time())
                    if nudges < 2 and waited >= 8 + nudges * 8:
                        nudges += 1
                        try:
                            page.mouse.wheel(0, 1200 * nudges)
                        except Exception:  # noqa: BLE001
                            pass
                try:
                    html = page.content()
                except Exception:  # noqa: BLE001
                    html = ""
            finally:
                try:
                    ctx.close()
                finally:
                    if browser is not None:
                        browser.close()

        user_info, ssr_items = parse_tiktok_ssr(html)
        if not user_info and isinstance(captured.get("user"), dict):
            user_info = captured["user"]
        items = ((captured.get("post") or {}).get("itemList")
                 or (captured.get("post") or {}).get("items")
                 or ssr_items or [])
        source = "TikTok 主页 SSR" if ssr_items else (
            "TikTok 接口响应（页面内）" if captured.get("post") else "TikTok 主页")

        snap = snapshot_from_tiktok(target, user_info if isinstance(user_info, dict) else {},
                                    items[:limit], source)
        snap.coverage = f"主页作品 {len(snap.works)} 条（未翻页）"
        snap.median_likes = mark_burst(snap.works)
        if blocked:
            snap.warnings.append("页面出现验证码/登录墙，数据可能不完整；本工具不做绕过")
        if not snap.works:
            snap.warnings.append(
                "未取到作品列表：可能是网络无法访问 TikTok（需要海外出口/代理）、"
                "被风控、或页面改版。请先在普通浏览器确认该主页可正常打开")
        if cover_dir is not None:
            for w in snap.works:
                if not w.cover_url:
                    continue
                stem = _safe_stem(w.id) if w.id else cover_stem(w.cover_url, w.title)
                name = download_cover(w.cover_url, Path(cover_dir), stem,
                                      referer="https://www.tiktok.com/")
                if name:
                    w.cover = name
        return snap

    def fetch_work_detail(self, target, work_id, *, profile_root, proxy, extra=None,
                          verbose=False, cookie_file=None):
        """TikTok 作品级数据（点赞/收藏/评论/播放）随列表一起返回，无需深挖。"""
        raise Unsupported("TikTok 的作品收藏/评论数随列表一起返回，无需单独深挖")


def _tiktok_blocked(page) -> bool:
    try:
        url = (page.url or "").lower()
        if "/login" in url:
            return True
        text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 3000)")
        low = str(text or "").lower()
        return any(m in low for m in ("verify", "captcha", "too many attempts", "滑动", "验证"))
    except Exception:  # noqa: BLE001
        return False


register(TiktokAdapter())


# --------------------------------------------------------------------------- #
# 未实现的平台：占位适配器（留出统一接口，声明能力后前端直接显示「暂不支持自动抓取」）
# --------------------------------------------------------------------------- #
def _register_placeholder(key: str, name: str, id_label: str, id_hint: str, why: str) -> None:
    class _Placeholder(Adapter):
        def parse_target(self, raw: str) -> str:
            s = (raw or "").strip()
            if not s:
                raise ValueError(f"{name}需要{id_label}")
            return s

    register(_Placeholder(key=key, name=name, auto=False, id_label=id_label,
                          id_hint=id_hint, fields={}, fetch_note=why))


_register_placeholder("kuaishou", "快手", "快手号", "主页链接",
                      "仓库内快手只有本人创作后台读数，他人主页无现成路径。")
_register_placeholder("zhihu", "知乎", "知乎用户", "主页链接",
                      "他人作品列表暂无结构化路径（仅能读任意文章评论）。")
_register_placeholder("bilibili", "B站", "UID / mid", "space.bilibili.com/<mid>",
                      "公开空间接口未实现且有 wbi 签名/风控（服务器 IP 实测 -352）。")
_register_placeholder("weixin-channels", "微信视频号", "视频号ID", "主页链接",
                      "仓库内未找到任何他人视频号数据入口。")
_register_placeholder("wechat-oa", "微信公众号", "公众号名", "公众号名或文章链接",
                      "他人账号页不可取，只能按文章标题做公开搜索的粗略推断。")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _emit(payload: dict, out: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)


def cmd_platforms(_a) -> int:
    _emit({"platforms": [ad.describe() for ad in all_adapters()],
           "order": list(PLATFORM_ORDER)}, None)
    return EXIT_OK


def cmd_fetch(a) -> int:
    ad = get_adapter(a.platform)
    if not ad.auto:
        _emit({"platform": ad.key, "platform_name": ad.name, "unsupported": True,
               "error": ad.fetch_note or f"{ad.name}暂不支持自动抓取"}, a.json)
        return EXIT_UNSUPPORTED
    try:
        target = ad.parse_target(a.target)
    except ValueError as e:
        _emit({"platform": ad.key, "error": str(e)}, a.json)
        return EXIT_INPUT
    cover_dir = None if a.no_cover else (Path(a.cover_dir) if a.cover_dir else None)
    if cover_dir is not None:
        cover_dir.mkdir(parents=True, exist_ok=True)
    try:
        snap = ad.fetch(target, limit=a.limit, profile_root=Path(a.profile_root),
                        proxy=a.proxy, cover_dir=cover_dir, verbose=not a.quiet,
                        cookie_file=a.cookie_file or None)
    except Unsupported as e:
        _emit({"platform": ad.key, "platform_name": ad.name, "unsupported": True,
               "error": str(e)}, a.json)
        return EXIT_UNSUPPORTED
    except ValueError as e:
        _emit({"platform": ad.key, "error": str(e)}, a.json)
        return EXIT_INPUT
    except Exception as e:  # noqa: BLE001 - 统一转成可读错误给 Web 层
        _emit({"platform": ad.key, "platform_name": ad.name,
               "error": f"{type(e).__name__}: {e}"[:240]}, a.json)
        return EXIT_EMPTY

    payload = snap.to_dict()
    _emit(payload, a.json)
    if snap.risk:
        return EXIT_RISK
    return EXIT_OK if snap.works or snap.nickname else EXIT_EMPTY


def cmd_detail(a) -> int:
    ad = get_adapter(a.platform)
    try:
        target = ad.parse_target(a.target)
        work = ad.fetch_work_detail(target, a.work_id, profile_root=Path(a.profile_root),
                                    proxy=a.proxy, extra={"xsec_token": a.xsec_token},
                                    verbose=not a.quiet, cookie_file=a.cookie_file or None)
    except Unsupported as e:
        _emit({"platform": ad.key, "unsupported": True, "error": str(e)}, a.json)
        return EXIT_UNSUPPORTED
    except Exception as e:  # noqa: BLE001
        _emit({"platform": ad.key, "work_id": a.work_id,
               "error": f"{type(e).__name__}: {e}"[:240]}, a.json)
        return EXIT_RISK if "风控" in str(e) else EXIT_EMPTY
    _emit(work.__dict__, a.json)
    return EXIT_OK


def cmd_parse(a) -> int:
    """只做「账号 ID / 主页链接 → 规范 ID」的解析校验，不联网（Web 端新增账号时调用）。"""
    ad = get_adapter(a.platform)
    try:
        target = ad.parse_target(a.target)
    except ValueError as e:
        _emit({"platform": ad.key, "platform_name": ad.name, "error": str(e)}, a.json)
        return EXIT_INPUT
    _emit({"platform": ad.key, "platform_name": ad.name, "target": target,
           "auto": ad.auto, "experimental": ad.experimental}, a.json)
    return EXIT_OK


def cmd_selftest(_a) -> int:
    fails = []

    def eq(got, want, label):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    eq(parse_count("1.3万"), (13000, False), "万")
    eq(parse_count("1万+"), (10000, True), "万+")
    eq(parse_count("1,234"), (1234, False), "千分位")
    eq(parse_count(143), (143, False), "int")
    eq(parse_count(""), (None, False), "空")
    eq(parse_count(None), (None, False), "None")
    eq(parse_count("32.0万"), (320000, False), "32.0万")
    eq(parse_count("1.2亿"), (120000000, False), "亿")
    eq(parse_target_xhs("https://www.xiaohongshu.com/user/profile/55753cf4f5a2633511d5a652?x=1"),
       "55753cf4f5a2633511d5a652", "主页链接")
    eq(parse_target_xhs("55753CF4F5A2633511D5A652"), "55753cf4f5a2633511d5a652", "裸 ID 大写")
    try:
        parse_target_xhs("xiaohongshu.com/user/profile/abc")
        fails.append("非法 user_id 应当报错")
    except ValueError:
        pass
    eq(parse_target_douyin("https://www.douyin.com/user/MS4wLjABAAAAxyz?tab=post"),
       "MS4wLjABAAAAxyz", "抖音链接")
    eq(median([1, 2, 3, 4]), 2, "中位数偶数")
    eq(median([5]), 5, "中位数单值")
    eq(median([None, None]), None, "中位数全空")
    eq(_epoch_ms(1760000000) % 1000, 0, "秒转毫秒")
    eq(_epoch_ms(1760000000000), 1760000000000, "毫秒保持")

    ws = [Work(id=str(i), likes=v) for i, v in enumerate([100, 110, 90, 400])]
    med = mark_burst(ws)
    eq(med, 105, "爆款中位数")
    eq([w.is_burst for w in ws], [False, False, False, True], "爆款标记")
    ws2 = [Work(id="a", likes=10), Work(id="b", likes=100)]
    mark_burst(ws2)
    eq([w.is_burst for w in ws2], [False, False], "样本不足不标记")

    if fails:
        print("SELFTEST FAILED:", file=sys.stderr)
        for f in fails:
            print("  -", f, file=sys.stderr)
        return 1
    print("selftest ok")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="对标账号数据采集（读他人公开账号）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("platforms", help="列出已注册平台的能力矩阵").set_defaults(func=cmd_platforms)
    sub.add_parser("selftest", help="离线自检纯函数").set_defaults(func=cmd_selftest)

    ps = sub.add_parser("parse", help="解析账号 ID/链接（不联网）")
    ps.add_argument("--platform", required=True, help=f"平台：{', '.join(ADAPTERS)}")
    ps.add_argument("--target", required=True, help="账号 ID 或主页链接")
    ps.add_argument("--json", dest="json", help="把结果同时写到该文件")
    ps.set_defaults(func=cmd_parse)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--platform", required=True, help=f"平台：{', '.join(ADAPTERS)}")
    common.add_argument("--target", required=True, help="账号 ID 或主页链接")
    common.add_argument("--profile-root", default=str(DEFAULT_PROFILE_ROOT),
                        help="浏览器 profile 根目录（默认 ~/.easel-browser-profiles）")
    common.add_argument("--proxy", default=os.environ.get("EASEL_PROXY") or os.environ.get("https_proxy") or "",
                        help="代理（抖音等平台需要；默认取 EASEL_PROXY/https_proxy）")
    common.add_argument("--cookie-file", default="",
                        help="平台 cookie 文件（小红书：给了就走纯 HTTP，不用浏览器）")
    common.add_argument("--json", dest="json", help="把结果同时写到该文件")
    common.add_argument("-q", "--quiet", action="store_true")

    f = sub.add_parser("fetch", parents=[common], help="抓某个对标账号的公开数据")
    f.add_argument("--limit", type=int, default=12, help="最多取多少条作品（默认 12）")
    f.add_argument("--cover-dir", help="封面本地缓存目录（不传则不下载）")
    f.add_argument("--no-cover", action="store_true", help="即使给了 --cover-dir 也不下载封面")
    f.set_defaults(func=cmd_fetch)

    d = sub.add_parser("detail", parents=[common], help="深挖单条作品（收藏/评论）")
    d.add_argument("--work-id", required=True, help="作品 ID")
    d.add_argument("--xsec-token", default="", help="小红书作品页所需 xsec_token")
    d.set_defaults(func=cmd_detail)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    import platform_access
    import platform_adapters
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    platform = platform_access.cli_option(sys.argv, "--platform") or ""
    enabled = command in {"fetch", "detail"} and platform_access.get_adapter(platform) is not None
    sys.exit(platform_access.run_cli(main, platform=platform or "xiaohongshu", enabled=enabled))
