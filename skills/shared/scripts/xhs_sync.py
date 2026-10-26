"""刷新对标账号时的一次「同步」：没下过的下素材，下过的只更新互动数据。

用户要的行为：刷新 → 本地素材库跟着对齐。

- **已下载**（``<notes-dir>/<note_id>/note.json`` 存在）→ 只重读笔记页更新 点赞/收藏/评论/分享
- **没下载** → 读笔记页 + 下载素材（图片原图/动图 + 视频，默认**最低画质**）
- **预算**：``--max-downloads`` 限制一次最多下载几条新作品（默认 5）。
- **请求上限**：``--max-requests`` 限制一次最多读取几条作品详情（默认 8）。
  不限制的话，50 条作品首次同步会一口气拉 1-2GB，还容易把接口打到限流（实测 HTTP 461）。

节流沿用参考项目标定的对数正态分布（均值 6s、最小 1s）；逐条落盘 + 写进度文件，
所以中途停掉/失败都不会丢已完成的部分。

CLI::

    python xhs_sync.py --works-file outputs/_benchmarks/<bid>/works.json \\
        --notes-dir outputs/_notes --cookie-file outputs/_login/xiaohongshu.cookie \\
        --progress-file outputs/_benchmarks/<bid>/sync.json --max-downloads 5 --max-requests 8
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xhs_note as note  # noqa: E402
import xhs_access

AVG_GAP, SIGMA = 6.0, 0.6          # 与 benchmark_fetch / xhs_fetch 同一套节流参数
DEFAULT_MAX_REQUESTS = 8


def gap() -> float:
    return max(1.0, random.lognormvariate(math.log(AVG_GAP) - (SIGMA ** 2) / 2, SIGMA))


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def plan_sync(works: list[dict], notes_dir: Path, max_downloads: int,
              index: dict | None = None) -> list[dict]:
    """给每条作品定动作：``update``（只更新数据）或 ``download``（补素材）。

    「已下载」以 ``index.json``（笔记ID → 目录）为准：目录名现在是「作者/标题」，
    不能再靠「目录名 == note_id」判断。
    """
    index = note.load_index(notes_dir) if index is None else index
    budget = max(0, int(max_downloads))
    plan: list[dict] = []
    for work in works:
        note_id = str(work.get("id") or "")
        if not note_id:
            continue
        rel = index.get(note_id)
        downloaded = bool(rel) and (notes_dir / rel / "note.json").is_file()
        action = "update"
        if not downloaded and budget > 0:
            action = "download"
            budget -= 1
        plan.append({"note_id": note_id, "action": action, "downloaded": downloaded,
                     "title": work.get("title") or ""})
    return plan


def sync(works_file: Path, notes_dir: Path, *, cookie: str = "", max_downloads: int = 5,
         quality: str = note.DEFAULT_QUALITY, progress_file: Path | None = None,
         verbose: bool = False, max_requests: int = DEFAULT_MAX_REQUESTS) -> dict:
    payload = json.loads(works_file.read_text(encoding="utf-8"))
    works: list[dict] = payload.get("works") or []
    index = note.load_index(notes_dir)          # 笔记ID → 「作者/标题」目录
    plan = plan_sync(works, notes_dir, max_downloads, index=index)
    total = len(plan)
    stats = {"total": total, "updated": 0, "downloaded": 0, "failed": 0,
             "downloaded_files": 0, "downloaded_bytes": 0,
             "skipped_due_budget": sum(1 for p in plan if p["action"] == "update" and not p["downloaded"]),
             "skipped_due_request_budget": 0, "requests_made": 0}
    by_id = {str(w.get("id")): w for w in works}

    def progress(index: int, entry: dict, message: str) -> None:
        if verbose:
            print(f"[i] 同步 {index}/{total}：{message}")
        if progress_file:
            write_json(progress_file, {
                "state": "running", "done": index, "total": total, "phase": entry["action"],
                "message": f"同步 {index}/{total}：{message}", "ts": int(time.time()),
                **{k: stats[k] for k in ("updated", "downloaded", "downloaded_files")},
            })

    for position, entry in enumerate(plan, 1):
        if stats["requests_made"] >= max(0, int(max_requests)):
            stats["skipped_due_request_budget"] = total - position + 1
            break
        index = note.load_index(notes_dir)      # 下载可能刚写入新映射，逐条重读（文件很小）
        note_id = entry["note_id"]
        work = by_id.get(note_id) or {}
        token = (work.get("extra") or {}).get("xsec_token") or ""
        stats["requests_made"] += 1
        try:
            data = note.parse_note(note_id, token, cookie=cookie)
        except Exception as error:  # noqa: BLE001 —— 单条失败不拖垮整批
            if isinstance(error, xhs_access.RequestBudgetExhausted):
                stats["skipped_due_request_budget"] = total - position + 1
                break
            if isinstance(error, xhs_access.AccessStopped) or xhs_access.is_risk_error(error):
                xhs_access.trip("平台校验信号，本轮停止")
                write_json(works_file, payload)
                stats.update(risk="平台校验信号，本轮停止", error=str(error))
                if progress_file:
                    write_json(progress_file, {"state": "risk", "message": stats["risk"], **stats})
                return stats
            stats["failed"] += 1
            work["sync_error"] = f"{type(error).__name__}: {error}"[:160]
            progress(position, entry, f"读取失败（{str(error)[:40]}）")
            time.sleep(gap())
            continue

        inter = data.get("interact") or {}
        for key, field in (("likes", "likes"), ("favorites", "favorites"),
                           ("comments", "comments"), ("shares", "shares")):
            if inter.get(key) is not None:
                work[field] = inter[key]
        work["detail_fetched"] = True
        work.pop("sync_error", None)

        if entry["action"] == "download":
            try:
                target = note.resolve_note_dir(
                    notes_dir, note_id, (data.get("author") or {}).get("nickname", ""),
                    data.get("title", ""))
                manifest = note.download_media(data, target, quality=quality)
                ok = [m for m in manifest if not m.get("error")]
                stats["downloaded"] += 1
                stats["downloaded_files"] += len(ok)
                stats["downloaded_bytes"] += sum(int(m.get("bytes") or 0) for m in ok)
                work["downloaded"] = True
                work["download_files"] = len(ok)
                progress(position, entry,
                         f"已下载素材 {len(ok)} 个（{round(sum(int(m.get('bytes') or 0) for m in ok) / 1048576, 1)}MB）")
            except Exception as error:  # noqa: BLE001
                if isinstance(error, xhs_access.AccessStopped) or xhs_access.is_risk_error(error):
                    xhs_access.trip("平台校验信号，本轮停止")
                    write_json(works_file, payload)
                    stats.update(risk="平台校验信号，本轮停止", error=str(error))
                    if progress_file:
                        write_json(progress_file, {"state": "risk", "message": stats["risk"], **stats})
                    return stats
                stats["failed"] += 1
                work["sync_error"] = f"下载失败：{type(error).__name__}: {error}"[:160]
                progress(position, entry, f"下载失败（{str(error)[:40]}）")
        else:
            stats["updated"] += 1
            # 已下载的：把本地记录里的互动数据也刷新，别让归档停在旧的数字上
            rel = index.get(note_id) or ""
            meta_fp = (notes_dir / rel / "note.json") if rel else (notes_dir / note_id / "note.json")
            if meta_fp.is_file():
                try:
                    record = json.loads(meta_fp.read_text(encoding="utf-8"))
                    record["interact"] = inter
                    record["interact_updated_at"] = int(time.time())
                    write_json(meta_fp, record)
                except Exception:  # noqa: BLE001 —— 记录刷新失败不影响主流程
                    pass
            progress(position, entry, "已更新互动数据"
                     if entry["downloaded"] else "已更新数据（素材待下，本次超出预算）")

        # 逐条落盘：中途停掉也不会丢已完成的部分
        write_json(works_file, payload)
        time.sleep(gap())

    if progress_file:
        write_json(progress_file, {
            "state": "done", "done": total, "total": total, "phase": "done",
            "message": (f"同步完成：更新 {stats['updated']} 条，下载 {stats['downloaded']} 条"
                        + (f"，失败 {stats['failed']} 条" if stats["failed"] else "")),
            "ts": int(time.time()), **stats,
        })
    write_json(works_file, payload)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对标账号同步：更新数据 + 补下素材")
    ap.add_argument("--works-file", required=True)
    ap.add_argument("--notes-dir", required=True)
    ap.add_argument("--cookie-file", default="")
    ap.add_argument("--progress-file", default="")
    ap.add_argument("--max-downloads", type=int, default=5,
                    help="本次最多下载几条新作品（默认 5；0 = 只更新数据不下载）")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                    help=f"本次最多读取几条作品详情（默认 {DEFAULT_MAX_REQUESTS}）")
    ap.add_argument("--quality", default=note.DEFAULT_QUALITY,
                    help="lowest（默认，最低画质）/ best / 1080 等")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    xhs_access.set_budget(max(0, a.max_requests))

    cookie = ""
    if a.cookie_file:
        try:
            cookie = Path(a.cookie_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            print(json.dumps({"error": f"cookie 文件读不到：{error}"}, ensure_ascii=False))
            return 4
    try:
        stats = sync(Path(a.works_file), Path(a.notes_dir), cookie=cookie,
                     max_downloads=a.max_downloads, quality=a.quality,
                     progress_file=Path(a.progress_file) if a.progress_file else None,
                     verbose=not a.quiet, max_requests=max(0, a.max_requests))
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 3
    print(json.dumps(stats, ensure_ascii=False))
    return 2 if stats.get("risk") else 0


if __name__ == "__main__":
    raise SystemExit(xhs_access.run_cli(main))
