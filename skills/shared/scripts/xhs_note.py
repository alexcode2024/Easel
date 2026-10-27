"""小红书单条笔记：解析全部数据 + 下载图片/视频原片。

输入来自**对标账号的作品列表**（那里已经有 ``note_id`` 和 ``xsec_token``），不需要用户粘贴链接。

做法参考 XHS-Downloader（JoeanAmier）的笔记处理链路，字段路径已用真实笔记逐项核对：

- 详情页：``window.__INITIAL_STATE__`` → ``note.noteDetailMap[-1].note``（YAML 解析，见 xhs_fetch）
- 视频：``video.media.stream.*[]``（``EF4``~``EF7`` 多档清晰度）按高度挑档；
  ``video.consumer.originVideoKey`` 是真·原片，但**实测今天的 payload 里是 None**，所以按高度挑才是有效路径
- 图文/图集：``imageList[].urlDefault`` → 抠 token → ``sns-img-bd.xhscdn.com/{token}`` 拿**原始上传文件**
  （``ci.xiaohongshu.com/{token}?imageView2/format/jpeg`` 可转格式）；动图在 ``imageList[].stream``

护栏：单条按需下载（不做批量搬运）；默认 1080p；单文件超过 ``MAX_FILE_BYTES`` 直接拒绝而不是硬下；
导出文件里注明**版权归原作者**。

CLI::

    python xhs_note.py parse    --note-id <id> [--xsec-token T] [--cookie-file F]
    python xhs_note.py download --note-id <id> --notes-dir DIR [--quality lowest|best] [...]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xhs_fetch as xf  # noqa: E402  复用 HTTP（指纹伪装）与 YAML 解析
import xhs_access
import xhs_browser

__all__ = ["parse_note", "download_note", "pick_video", "image_original_url", "guess_ext"]

MAX_FILE_BYTES = 220 * 1024 * 1024      # 单文件上限（2160p 视频实测 141MB，留余量）
DEFAULT_QUALITY = "lowest"               # 默认最低画质：省磁盘与带宽，需要时再指定 1080/best
INDEX_NAME = "index.json"                # 笔记ID → 目录 的映射（目录改成「作者/标题」后靠它定位）
ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
NAME_LIMIT = 48                          # 目录名长度上限（给作者层与后缀留余量）


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def safe_name(text: str, fallback: str = "未命名") -> str:
    """把标题/昵称清洗成合法目录名。

    Windows 非法字符（``\\ / : * ? " < > |``）换成下划线；**中文与 emoji 保留**
    （NTFS 支持，名字更好认）；去掉首尾空格与点（Windows 不允许结尾是点/空格）；
    过长截断，避免撞上路径长度上限。
    """
    name = ILLEGAL_NAME.sub("_", (text or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > NAME_LIMIT:
        name = name[:NAME_LIMIT].rstrip(" .")
    return name or fallback


def load_index(notes_dir: Path) -> dict:
    """读 ``notes_dir/index.json``：``{笔记ID: 相对目录}``。"""
    fp = Path(notes_dir) / INDEX_NAME
    if not fp.is_file():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def save_index(notes_dir: Path, index: dict) -> None:
    fp = Path(notes_dir) / INDEX_NAME
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fp)


def resolve_note_dir(notes_dir: Path, note_id: str, author: str = "", title: str = "") -> Path:
    """算出这条笔记的目录：``<作者>/<标题>``（已下过就沿用原目录，不来回搬家）。

    重名：``<作者>/<标题>`` 已被**另一条**笔记占用时追加 ``-<ID前8位>``。
    映射写进 ``index.json``——之后靠 note_id 也能稳定找回，不受标题改动影响。
    """
    notes_dir = Path(notes_dir)
    index = load_index(notes_dir)
    rel = index.get(note_id)
    if rel and (notes_dir / rel / "note.json").is_file():
        return notes_dir / rel
    author_dir = safe_name(author, "未知作者")
    title_dir = safe_name(title, f"笔记{note_id[:8]}")
    target = notes_dir / author_dir / title_dir
    if (target / "note.json").is_file():
        try:
            other = json.loads((target / "note.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            other = {}
        if other.get("note_id") not in (None, note_id):
            target = notes_dir / author_dir / f"{title_dir}-{note_id[:8]}"
    index[note_id] = target.relative_to(notes_dir).as_posix()
    save_index(notes_dir, index)
    return target


def _pick(container: dict, *keys, default=None):
    """蛇形/驼峰两种命名都认（接口是 snake_case，SSR 是 camelCase）。"""
    for key in keys:
        if isinstance(container, dict) and container.get(key) is not None:
            return container[key]
    return default


def image_original_url(item: dict) -> str:
    """从 ``imageList[i]`` 取原始文件地址（token 形式，不含展示水印叠加）。"""
    raw = _pick(item, "urlDefault", "url_default", "url", default="") or ""
    if not raw:
        infos = _pick(item, "infoList", "info_list", default=[]) or []
        for info in infos:
            if _pick(info, "imageScene", "image_scene") == "WB_DFT":
                raw = info.get("url") or ""
                break
        raw = raw or next((i.get("url") for i in infos if i.get("url")), "")
    if not raw:
        return ""
    token = "/".join(str(raw).split("/")[5:]).split("!")[0]
    return f"https://sns-img-bd.xhscdn.com/{token}" if token else ""


def video_variants(note: dict) -> list[dict]:
    """把所有清晰度变体摊平成一个列表（带 url 与元信息）。"""
    video = note.get("video") or {}
    stream = ((video.get("media") or {}).get("stream") or {})
    out: list[dict] = []
    for key, arr in stream.items():
        for item in arr or []:
            if not isinstance(item, dict):
                continue
            urls = [u for u in (item.get("backupUrls") or []) if u]
            master = item.get("masterUrl") or ""
            if master and master not in urls:
                urls.append(master)
            if not urls:
                continue
            primary = urls[0]
            ext = primary.split("?")[0].rsplit(".", 1)[-1].lower()
            out.append({
                "url": primary,
                "urls": urls,
                "ext": ext,
                # .m4s 是 H.265/AV1 的分段流：播放器兼容性差，且实测在备份域上会被掐断，
                # 所以有 mp4 就优先 mp4
                "playable": ext in ("mp4", "mov"),
                "height": _int(item.get("height")) or 0,
                "width": _int(item.get("width")) or 0,
                "bitrate": _int(item.get("videoBitrate") or item.get("avgBitrate")) or 0,
                "size": _int(item.get("size")) or 0,
                "format": item.get("format") or "",
                "stream": key,
                "origin": False,
            })
    # 真·原片（实测常为 None，有则优先）
    origin_key = ((video.get("consumer") or {}).get("originVideoKey")) or ""
    if origin_key:
        url = f"https://sns-video-bd.xhscdn.com/{origin_key}"
        out.append({"url": url, "urls": [url], "ext": "mp4", "playable": True, "height": 99999,
                    "width": 0, "bitrate": 0, "size": 0, "format": "mp4", "stream": "origin",
                    "origin": True})
    return out


def pick_video(variants: list[dict], quality: str = DEFAULT_QUALITY) -> dict | None:
    """挑一档视频。

    ``quality`` 取值：
    - ``lowest``（**默认**）：最低画质——省磁盘/带宽，看内容够用
    - ``best``：最高画质（2160p 实测 141MB 一条，慎用）
    - 数字（如 ``1080``）：最接近该高度的一档；没有就用「不低于它里最小的」

    ``originVideoKey`` 那条真·原片只在 ``best`` 时才可能被选中（它通常最大）。
    """
    if not variants:
        return None
    usable = [v for v in variants if not v.get("origin")] or variants
    playable = [v for v in usable if v.get("playable")] or usable
    if quality == "best":
        pool = variants if any(v.get("origin") for v in variants) else playable
        return max(pool, key=lambda v: v.get("height") or 0)
    if quality in ("lowest", "", None):
        low = min(v.get("height") or 0 for v in playable)
        same = [v for v in playable if (v.get("height") or 0) == low]
        # 同档里再挑体积/码率最小的，真正把带宽省下来
        return min(same, key=lambda v: (v.get("size") or 0, v.get("bitrate") or 0))
    target = _int(quality) or 1080
    exact = [v for v in playable if v.get("height") == target]
    if exact:
        return max(exact, key=lambda v: v.get("bitrate") or 0)
    above = [v for v in playable if (v.get("height") or 0) >= target]
    if above:
        return min(above, key=lambda v: (v.get("height") or 0, -(v.get("bitrate") or 0)))
    return max(playable, key=lambda v: v.get("height") or 0)


def live_photo_urls(note: dict) -> list[str]:
    """动图（live photo）地址。"""
    urls: list[str] = []
    for item in note.get("imageList") or []:
        stream = item.get("stream") or {}
        url = ""
        for key in list(stream)[:3]:
            first = (stream.get(key) or [{}])[0] if isinstance(stream.get(key), list) else {}
            url = next((u for u in (first.get("backupUrls") or []) if u), "") or first.get("masterUrl") or ""
            if url:
                break
        if url:
            urls.append(url)
    return urls


def note_kind(note: dict) -> str:
    """作品类型：视频 / 图文 / 图集（与参考项目同一判据）。"""
    type_ = (note.get("type") or "").lower()
    images = note.get("imageList") or []
    if type_ not in {"video", "normal"} or not images:
        return "未知"
    if type_ == "video":
        return "视频" if len(images) == 1 else "图集"
    return "图文"


def parse_note(note_id: str, xsec_token: str = "", *, cookie: str = "",
               verbose: bool = False) -> dict:
    """解析一条笔记的全部数据（元信息 + 媒体地址清单），不下载。"""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_user"
    response = xf._http_get(url, cookie)
    if response.status_code != 200:
        raise RuntimeError(f"笔记页 HTTP {response.status_code}")
    state = xf._state_from_html(response.text)
    detail_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if not isinstance(detail_map, dict) or not detail_map:
        raise RuntimeError("笔记详情解析失败（笔记可能已删除/仅自己可见，或页面结构变化）")
    note = (list(detail_map.values())[-1] or {}).get("note") or {}
    info = note.get("interactInfo") or note.get("interact_info") or {}
    user = note.get("user") or {}
    images = note.get("imageList") or note.get("image_list") or []
    variants = video_variants(note)
    kind = note_kind(note)
    return {
        "note_id": note.get("noteId") or note_id,
        "url": url,
        "kind": kind,
        "type": note.get("type") or "",
        "title": note.get("title") or "",
        "desc": note.get("desc") or "",
        "tags": [t.get("name") for t in (note.get("tagList") or []) if isinstance(t, dict)],
        "published_at_ms": _int(note.get("time")),
        "updated_at_ms": _int(note.get("lastUpdateTime") or note.get("last_update_time")),
        "ip_location": note.get("ipLocation") or "",
        "author": {
            "nickname": user.get("nickname") or user.get("nickName") or "",
            "user_id": user.get("userId") or user.get("user_id") or "",
            "avatar": user.get("avatar") or "",
        },
        "interact": {
            "likes": _int(xf._count_number(info.get("likedCount") or info.get("liked_count"))),
            "favorites": _int(xf._count_number(info.get("collectedCount") or info.get("collected_count"))),
            "comments": _int(xf._count_number(info.get("commentCount") or info.get("comment_count"))),
            "shares": _int(xf._count_number(info.get("shareCount") or info.get("share_count"))),
        },
        "images": [{"url": u} for u in (image_original_url(i) for i in images) if u],
        "live_photos": live_photo_urls(note),
        "video": variants,
        "cover": xf._cover_url(note),
    }


def guess_ext(content: bytes, fallback: str = "bin") -> str:
    """按文件头判后缀（参考项目用 FILE_SIGNATURES，这里取常用几种）。"""
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG"):
        return "png"
    if content.startswith(b"GIF8"):
        return "gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "heic"
    if content[4:8] == b"ftyp":
        brand = content[8:12]
        return "mp4" if brand in (b"isom", b"mp42", b"iso2", b"avc1", b"mp41") else "mov"
    if content.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return fallback


def _download(urls, dest: Path, *, referer: str, limit: int = MAX_FILE_BYTES) -> dict:
    """下载文件到 ``dest``（先下临时文件，成功再改名）。

    ``urls`` 可以是单个地址，也可以是**候选地址列表**：实测某些 CDN 备份域会中途
    掐断连接（``curl: (52) Empty reply from server``），所以一个不行就换下一个。
    """
    from curl_cffi import requests as creq

    candidates = [urls] if isinstance(urls, str) else [u for u in urls if u]
    part = dest.with_suffix(dest.suffix + ".part")
    result = {"url": candidates[0] if candidates else "", "path": "", "bytes": 0, "error": "",
              "tries": 0}
    last_error = "没有可用地址"
    for url in candidates:
        result["tries"] += 1
        response = None
        try:
            if xhs_access.active():
                xhs_access.check_breaker()
            result["url"] = url
            # 注意：curl_cffi 的 Response 不支持 with 语句（实测 TypeError），手动关闭
            response = creq.get(url, **xhs_browser.http_options("media", referer=referer),
                                timeout=180, verify=False, allow_redirects=True, stream=True)
            xhs_access.inspect_status(response)
            if response.status_code not in (200, 206):
                last_error = f"HTTP {response.status_code}"
                continue
            total = 0
            with part.open("wb") as fh:
                for chunk in response.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        fh.close()
                        part.unlink(missing_ok=True)
                        result["error"] = f"超过单文件上限 {limit // 1048576}MB，已中止"
                        return result
                    fh.write(chunk)
            if total == 0:
                last_error = "服务端返回空内容"
                part.unlink(missing_ok=True)
                continue
            head = part.open("rb").read(16)
            ext = guess_ext(head, dest.suffix.lstrip(".") or "bin")
            final = dest.with_suffix("." + ext)
            if final.exists():
                final.unlink()
            part.replace(final)
            result.update({"path": final.name, "bytes": final.stat().st_size, "error": ""})
            return result
        except xhs_access.AccessStopped:
            part.unlink(missing_ok=True)
            raise
        except Exception as error:  # noqa: BLE001
            last_error = f"{type(error).__name__}: {error}"
            part.unlink(missing_ok=True)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:  # noqa: BLE001
                pass
    result["error"] = last_error
    return result


def download_media(data: dict, out_dir: Path, *, quality: str = DEFAULT_QUALITY, images: bool = True,
                   video: bool = True, live: bool = True, verbose: bool = False,
                   progress=None) -> list[dict]:
    """把**已解析好**的笔记数据落盘（``media/`` + ``note.json`` + ``note.md``）。

    与 ``download_note`` 拆开是为了让「同步」复用同一次解析结果——
    否则每条要白白多请求一次笔记页。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / "media"
    media_dir.mkdir(exist_ok=True)
    referer = data.get("url") or ""
    manifest: list[dict] = []
    warnings: list[str] = list(data.get("warnings") or [])

    def step(msg: str) -> None:
        if verbose:
            print(msg)
        if progress:
            progress(msg)

    if images:
        for idx, item in enumerate(data.get("images") or [], 1):
            step(f"[i] 图片 {idx}/{len(data['images'])}")
            got = _download(item["url"], media_dir / f"{idx:03d}", referer=referer)
            got["kind"] = "image"
            manifest.append(got)
            time.sleep(random.uniform(0.4, 1.2))
    if live and data.get("live_photos"):
        for idx, url in enumerate(data["live_photos"], 1):
            step(f"[i] 动图 {idx}/{len(data['live_photos'])}")
            got = _download(url, media_dir / f"live_{idx:03d}", referer=referer)
            got["kind"] = "live"
            manifest.append(got)
            time.sleep(random.uniform(0.4, 1.2))
    if video and data.get("video"):
        chosen = pick_video(data["video"], quality)
        if chosen:
            label = "原片" if chosen.get("origin") else f"{chosen.get('height')}p"
            step(f"[i] 视频（{label}，约 {round((chosen.get('size') or 0) / 1048576, 1)}MB）")
            got = _download(chosen.get("urls") or chosen["url"],
                            media_dir / f"video_{chosen.get('height') or 'x'}", referer=referer)
            got.update({"kind": "video", "height": chosen.get("height"), "quality": label})
            manifest.append(got)
    elif video and not data.get("video"):
        warnings.append("这条笔记没有视频（图文/图集）")

    failed = [m for m in manifest if m.get("error")]
    if failed:
        warnings.append(f"{len(failed)} 个文件没下成功：" +
                        "；".join(f"{m['kind']}:{m['error']}" for m in failed[:3]))

    payload = {
        **{k: v for k, v in data.items()
           if k not in ("images", "video", "warnings", "quality_requested")},
        "quality_requested": quality,
        "downloaded_at": int(time.time()),
        "media": manifest,
        "warnings": warnings,
        "copyright": "内容版权归原作者所有；此处仅用于拆解研究，转载/商用请先获得授权并注明出处。",
    }
    (out_dir / "note.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (out_dir / "note.md").write_text(_render_md(payload), encoding="utf-8")
    return manifest


def download_note(note_id: str, out_dir: Path, *, xsec_token: str = "", cookie: str = "",
                  quality: str = DEFAULT_QUALITY, images: bool = True, video: bool = True,
                  live: bool = True, verbose: bool = False,
                  progress=None) -> dict:
    """解析并下载一条笔记：写入 ``note.json`` / ``note.md`` 与 ``media/`` 下的文件。"""
    data = parse_note(note_id, xsec_token, cookie=cookie, verbose=verbose)
    manifest = download_media(data, out_dir, quality=quality, images=images, video=video,
                              live=live, verbose=verbose, progress=progress)
    payload = json.loads((out_dir / "note.json").read_text(encoding="utf-8"))
    payload["media"] = manifest
    payload["dir"] = Path(out_dir).name
    return payload


def _render_md(data: dict) -> str:
    inter = data.get("interact") or {}
    author = data.get("author") or {}
    tags = " ".join(f"#{t}" for t in data.get("tags") or [])
    lines = [
        f"# {data.get('title') or '（无标题）'}", "",
        f"- 类型：{data.get('kind')} | 发布：{_fmt_time(data.get('published_at_ms'))}"
        f" | 最后更新：{_fmt_time(data.get('updated_at_ms'))}",
        f"- 作者：{author.get('nickname')}（{author.get('user_id')}）",
        f"- 互动：❤️ {inter.get('likes')} ⭐ {inter.get('favorites')} "
        f"💬 {inter.get('comments')} 🔗 {inter.get('shares')}",
        f"- 原文：{data.get('url')}", "",
        "## 正文", "", (data.get("desc") or "（无）"), "",
    ]
    if tags:
        lines += ["## 标签", "", tags, ""]
    media = data.get("media") or []
    if media:
        lines += ["## 已下载文件", ""]
        for m in media:
            name = m.get("path") or "（失败）"
            lines.append(f"- `{m.get('kind')}` {name} {round((m.get('bytes') or 0) / 1024)}KB"
                         + (f" —— {m['error']}" if m.get("error") else ""))
        lines.append("")
    for warning in data.get("warnings") or []:
        lines.append(f"> ⚠️ {warning}")
    lines += ["", "---", "", f"*{data.get('copyright')}*", ""]
    return "\n".join(lines)


def _fmt_time(ms) -> str:
    if not ms:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="小红书单条笔记：解析 / 下载")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("parse", "download"):
        p = sub.add_parser(name)
        p.add_argument("--note-id", required=True)
        p.add_argument("--xsec-token", default="")
        p.add_argument("--cookie-file", default="")
        p.add_argument("--notes-dir", default="", help="素材根目录（内部按 <作者>/<标题> 建子目录）")
        p.add_argument("--out-dir", default="", help="直接指定目录（可选，给了就不按作者/标题命名）")
        p.add_argument("--quality", default=DEFAULT_QUALITY,
                       help="lowest（默认，最低画质）/ best / 1080 等")
        p.add_argument("--no-images", action="store_true")
        p.add_argument("--no-video", action="store_true")
        p.add_argument("--no-live", action="store_true")
        p.add_argument("--json", dest="json_out", default="")
        p.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    cookie = ""
    if a.cookie_file:
        try:
            cookie = Path(a.cookie_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"cookie 文件读不到：{error}", file=sys.stderr)
            return 4
    try:
        if a.cmd == "parse":
            data = parse_note(a.note_id, a.xsec_token, cookie=cookie, verbose=not a.quiet)
        else:
            if not (a.out_dir or a.notes_dir):
                print("download 需要 --notes-dir（或 --out-dir）", file=sys.stderr)
                return 4
            target = Path(a.out_dir) if a.out_dir else None
            if target is None:
                # 先解析出作者/标题，再按 <作者>/<标题> 定目录（同一笔记不会重复建目录）
                peek = parse_note(a.note_id, a.xsec_token, cookie=cookie)
                target = resolve_note_dir(Path(a.notes_dir), a.note_id,
                                          (peek.get("author") or {}).get("nickname", ""),
                                          peek.get("title", ""))
            data = download_note(a.note_id, Path(target), xsec_token=a.xsec_token,
                                 cookie=cookie, quality=a.quality,
                                 images=not a.no_images, video=not a.no_video,
                                 live=not a.no_live, verbose=not a.quiet)
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 3
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.quiet:
        print(json.dumps({k: v for k, v in data.items() if k != "video"}, ensure_ascii=False))
    else:
        print(f"\n  标题   {data.get('title')}")
        print(f"  类型   {data.get('kind')} | 作者 {data['author'].get('nickname')}")
        print(f"  互动   ❤️{data['interact'].get('likes')} ⭐{data['interact'].get('favorites')} "
              f"💬{data['interact'].get('comments')} 🔗{data['interact'].get('shares')}")
        if a.cmd == "parse":
            print(f"  图片   {len(data.get('images') or [])} 张 | 动图 {len(data.get('live_photos') or [])} "
                  f"| 视频变体 {len(data.get('video') or [])} 档")
        else:
            ok = [m for m in data.get("media") or [] if not m.get("error")]
            print(f"  文件   成功 {len(ok)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(xhs_access.run_cli(main))
