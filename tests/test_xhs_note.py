"""单条笔记解析与下载（xhs_note.py）：纯函数与媒体挑选规则。

不联网：字段路径用**实测抓到的真实结构**做夹具（见注释里的实测来源），
真实下载由手工实测覆盖（图集 17 文件 16.4MB、视频 1080p 48.2MB）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "shared" / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import xhs_note as note  # noqa: E402


# --------------------------------------------------------------------------- #
# 媒体地址抽取（结构取自 2026-10-16 实测：图集 11 图 + 5 动图；视频 5 档清晰度）
# --------------------------------------------------------------------------- #
def test_image_original_url_from_urldefault():
    """``imageList[].urlDefault`` → 抠 token → sns-img-bd 原文件地址。"""
    item = {"urlDefault": "http://sns-webpic-qc.xhscdn.com/202610162142/abc123/"
                          "1040g2sg324c6j73b7ue040pi8tuf99iigki4jdo!nc_n_webp_mw_1",
            "url": ""}
    assert note.image_original_url(item) == \
        "https://sns-img-bd.xhscdn.com/1040g2sg324c6j73b7ue040pi8tuf99iigki4jdo"


def test_image_original_url_falls_back_to_info_list():
    """``urlDefault`` 为空时用 infoList 里的 WB_DFT（SSR 形态就是这种）。"""
    item = {"urlDefault": "", "url": "",
            "infoList": [{"imageScene": "WB_PRV", "url": "http://x/prv/aa/tok1!x"},
                         {"imageScene": "WB_DFT", "url": "http://x/dft/bb/tok2!y"}]}
    assert note.image_original_url(item).endswith("/tok2")
    assert note.image_original_url({}) == ""


def test_pick_video_lowest_by_default_then_target_then_best():
    """默认**最低画质**（省磁盘/带宽）；需要时指定 1080 或 best。"""
    variants = [{"height": 720, "bitrate": 900, "url": "u720a", "size": 31_000_000},
                {"height": 720, "bitrate": 800, "url": "u720b", "size": 36_000_000},
                {"height": 1080, "bitrate": 2000, "url": "u1080", "size": 48_000_000},
                {"height": 1080, "bitrate": 3500, "url": "u1080b", "size": 50_000_000},
                {"height": 2160, "bitrate": 9000, "url": "u2160", "size": 148_559_000}]
    assert note.pick_video(variants)["url"] == "u720a", "默认取最低档，同档再取体积最小的"
    assert note.pick_video(variants, "lowest")["url"] == "u720a"
    assert note.pick_video(variants, "1080")["url"] == "u1080b", "同档取码率最高的"
    assert note.pick_video(variants, "best")["url"] == "u2160"
    # 没有 1080 时取「不低于目标里最小的」，而不是直接掉到最低档
    assert note.pick_video([v for v in variants if v["height"] != 1080], "1080")["url"] == "u2160"
    assert note.pick_video([{"height": 480, "url": "only"}], "1080")["url"] == "only"
    assert note.pick_video([], "1080") is None


def test_pick_video_keeps_origin_when_present():
    """``originVideoKey`` 是真·原片（实测今天为 None，但结构上要优先）。"""
    video = {"consumer": {"originVideoKey": "abc/def.mp4"},
             "media": {"stream": {"EF5": [{"height": 720, "masterUrl": "http://x/720.mp4"}]}}}
    variants = note.video_variants({"video": video})
    assert any(v["origin"] for v in variants)
    assert note.pick_video(variants, "best")["origin"] is True
    assert note.pick_video(variants, "1080")["height"] == 720, "选档时不吃 origin"


def test_video_variants_reads_backup_urls_and_master():
    video = {"media": {"stream": {
        "EF5": [{"height": 720, "backupUrls": ["", "http://x/b1.mp4"], "masterUrl": "http://x/m1.mp4"}],
        "EF7": [{"height": 2160, "masterUrl": "http://x/m2.mp4"}]}}}
    variants = note.video_variants({"video": video})
    assert {v["url"] for v in variants} == {"http://x/b1.mp4", "http://x/m2.mp4"}
    assert max(v["height"] for v in variants) == 2160
    # 候选地址要留住（备份域实测会被掐断，得能换一个再试）
    assert variants[0]["urls"] == ["http://x/b1.mp4", "http://x/m1.mp4"]


def test_video_variants_prefers_playable_mp4():
    """实测：同一档会同时给 .m4s（H.265 分段流）和 .mp4；
    m4s 在备份域上会被掐断、播放器也不一定认，所以要优先 mp4。"""
    video = {"media": {"stream": {
        "EF5": [{"height": 720, "size": 52_700_000,
                 "masterUrl": "http://bak/xx_3090.m4s"}],
        "EF4": [{"height": 720, "size": 53_700_000,
                 "masterUrl": "http://bak/xx_259.mp4"}]}}}
    variants = note.video_variants({"video": video})
    assert [v["playable"] for v in sorted(variants, key=lambda v: v["ext"])] == [False, True]
    chosen = note.pick_video(variants, "lowest")
    assert chosen["ext"] == "mp4", "最低档里要挑可播放的那个（哪怕大一点点）"
    # 全是 m4s 时退而求其次，而不是返回 None
    only_m4s = [v for v in variants if v["ext"] == "m4s"]
    assert note.pick_video(only_m4s, "lowest")["ext"] == "m4s"


def test_download_tries_alternate_urls(tmp_path, monkeypatch):
    """第一个地址失败要自动换下一个（curl_cffi 的 Response 不支持 with，这里也顺带覆盖）。"""
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def iter_content(self, _n):
            yield b"\xff\xd8\xff\xe0" + b"0" * 32

        def close(self):
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("Empty reply from server")
        return FakeResponse()

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    got = note._download(["http://a/1.mp4", "http://b/2.mp4"], tmp_path / "v", referer="r")
    assert calls == ["http://a/1.mp4", "http://b/2.mp4"]
    assert got["path"] == "v.jpg" and got["bytes"] > 0 and got["tries"] == 2
    assert got["error"] == ""


def test_live_photo_urls():
    note_dict = {"imageList": [
        {"stream": {"h264": [{"backupUrls": ["http://x/live1.mp4"]}]}},
        {"stream": {}},                      # 没有动图的那张要跳过
        {"stream": {"h264": [{"masterUrl": "http://x/live2.mp4"}]}},
    ]}
    assert note.live_photo_urls(note_dict) == ["http://x/live1.mp4", "http://x/live2.mp4"]


def test_note_kind_rules():
    """类型判据与参考项目一致：video+1 图=视频，video+多图=图集，normal=图文。"""
    assert note.note_kind({"type": "video", "imageList": [{}]}) == "视频"
    assert note.note_kind({"type": "video", "imageList": [{}, {}]}) == "图集"
    assert note.note_kind({"type": "normal", "imageList": [{}, {}]}) == "图文"
    assert note.note_kind({"type": "normal", "imageList": []}) == "未知"
    assert note.note_kind({}) == "未知"


def test_guess_ext_uses_magic_bytes():
    """按文件头判后缀，而不是信扩展名（参考项目的 FILE_SIGNATURES 思路）。"""
    assert note.guess_ext(b"\xff\xd8\xff\xe0" + b"0" * 12) == "jpg"
    assert note.guess_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "png"
    assert note.guess_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert note.guess_ext(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "heic"
    assert note.guess_ext(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00") == "mp4"
    assert note.guess_ext(b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00") == "mov"
    assert note.guess_ext(b"unknown-bytes", "bin") == "bin"


def test_render_md_mentions_copyright_and_files():
    """导出记录必须带文件清单与版权说明（不把他人内容当自己的）。"""
    md = note._render_md({
        "title": "标题", "kind": "图文", "published_at_ms": 1758719093000,
        "updated_at_ms": None, "author": {"nickname": "某人", "user_id": "u1"},
        "interact": {"likes": 1, "favorites": 2, "comments": 3, "shares": 4},
        "url": "https://www.xiaohongshu.com/explore/x", "desc": "正文",
        "tags": ["标签A"], "media": [{"kind": "image", "path": "001.heic", "bytes": 635519}],
        "warnings": ["没有视频"], "copyright": "版权归原作者所有",
    })
    assert "001.heic" in md and "621KB" in md
    assert "#标签A" in md
    assert "版权归原作者所有" in md
    assert "没有视频" in md


def test_note_summary_reads_written_note_json(tmp_path, monkeypatch):
    """Web 层靠 index.json + note.json 判断「已下载」——目录名现在是「作者/标题」。"""
    import app as web  # noqa: E402  需要 web 目录在 sys.path

    rel = "某作者/某标题"
    note_dir = tmp_path / rel
    note_dir.mkdir(parents=True)
    (note_dir / "note.json").write_text(json.dumps({
        "note_id": "note1", "kind": "图文", "title": "某标题", "downloaded_at": 123,
        "media": [{"kind": "image", "bytes": 100, "path": "001.jpg"},
                  {"kind": "image", "bytes": 50, "error": "HTTP 404"}],
        "warnings": ["x"],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "index.json").write_text(json.dumps({"note1": rel}, ensure_ascii=False),
                                         encoding="utf-8")
    monkeypatch.setattr(web, "NOTES_DIR", tmp_path)

    summary = web._note_summary("note1")
    assert summary["files"] == 1 and summary["bytes"] == 100, "失败的文件不计入"
    assert summary["dir"] == f"_notes/{rel}"
    assert summary["note_md"] == f"_notes/{rel}/note.md"
    assert web._note_summary("nope") is None
    # 索引指向不存在的目录 → 视为未下载，而不是抛异常
    (tmp_path / "index.json").write_text(json.dumps({"note2": "没了/也没了"}), encoding="utf-8")
    assert web._note_summary("note2") is None


# --------------------------------------------------------------------------- #
# 目录命名：<作者>/<标题>（清洗、重名、索引）
# --------------------------------------------------------------------------- #
def test_safe_name_strips_illegal_and_keeps_readable():
    assert note.safe_name('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert note.safe_name("  标题。  ") == "标题。"            # 中文与句号保留
    assert note.safe_name("带 emoji 🍅 的标题") == "带 emoji 🍅 的标题"
    assert note.safe_name("") == "未命名"
    assert note.safe_name("...") == "未命名"
    assert len(note.safe_name("长" * 200)) == note.NAME_LIMIT


def test_resolve_note_dir_uses_author_and_title(tmp_path):
    d = note.resolve_note_dir(tmp_path, "id1", "大厂升升", "💰副业思路：自由不上班？半角?要换")
    # 全角「？」在 Windows 上合法 → 保留；半角 ? 非法 → 换下划线
    assert d == tmp_path / "大厂升升" / "💰副业思路：自由不上班？半角_要换"
    # 已下载过的笔记沿用索引里的目录：标题后来变了也不搬家（否则文件散落两处）
    (d / "media").mkdir(parents=True)
    (d / "note.json").write_text(json.dumps({"note_id": "id1"}), encoding="utf-8")
    assert note.resolve_note_dir(tmp_path, "id1", "大厂升升", "标题改了") == d
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["id1"] == \
        "大厂升升/💰副业思路：自由不上班？半角_要换"


def test_resolve_note_dir_handles_title_collision(tmp_path):
    """同作者两条笔记标题相同时，第二条加 ID 后缀，不能互相覆盖。"""
    first = note.resolve_note_dir(tmp_path, "aaaaaaaa1111", "作者", "同名标题")
    (first / "media").mkdir(parents=True)
    (first / "note.json").write_text(json.dumps({"note_id": "aaaaaaaa1111"}), encoding="utf-8")
    second = note.resolve_note_dir(tmp_path, "bbbbbbbb2222", "作者", "同名标题")
    assert second != first and second.name == "同名标题-bbbbbbbb"
    third = note.resolve_note_dir(tmp_path, "cccccccc3333", "", "")
    assert third.parent.name == "未知作者" and third.name == "笔记cccccccc"


# --------------------------------------------------------------------------- #
# 刷新后的「同步」：没下过的下素材、下过的只更新互动数据（xhs_sync.py）
# --------------------------------------------------------------------------- #
import xhs_sync as sync  # noqa: E402


def _works(*ids):
    return [{"id": i, "title": f"作品{i}", "likes": 1, "favorites": None,
             "extra": {"xsec_token": f"T{i}"}} for i in ids]


def _mark_downloaded(notes_dir, note_id, rel="作者/标题"):
    (notes_dir / rel).mkdir(parents=True, exist_ok=True)
    (notes_dir / rel / "note.json").write_text(
        json.dumps({"note_id": note_id, "media": []}, ensure_ascii=False), encoding="utf-8")
    idx = note.load_index(notes_dir)
    idx[note_id] = rel
    note.save_index(notes_dir, idx)


def test_plan_sync_respects_budget(tmp_path):
    """预算内的新作品排「下载」，超出的只更新数据；已下载过的永远只更新。"""
    _mark_downloaded(tmp_path, "b")
    plan = sync.plan_sync(_works("a", "b", "c", "d"), tmp_path, max_downloads=2)
    by_id = {p["note_id"]: p for p in plan}
    assert by_id["a"]["action"] == "download" and by_id["c"]["action"] == "download"
    assert by_id["d"]["action"] == "update", "超出预算的新作品本次不下载"
    assert by_id["b"]["action"] == "update" and by_id["b"]["downloaded"] is True
    # 预算 0 = 只更新数据
    assert {p["action"] for p in sync.plan_sync(_works("a"), tmp_path, 0)} == {"update"}


def test_plan_sync_skips_works_without_id(tmp_path):
    """游客态列表没有作品 ID —— 没有 ID 就无法读笔记，直接跳过而不是报错。"""
    plan = sync.plan_sync([{"id": "", "title": "无 ID"}, {"id": "x"}], tmp_path, 5)
    assert [p["note_id"] for p in plan] == ["x"]


def test_sync_updates_metrics_and_downloads_new(tmp_path, monkeypatch):
    """核心行为：已下载的只更新数字（连本地记录一起刷新），没下过的下素材。"""
    notes_dir = tmp_path / "notes"
    _mark_downloaded(notes_dir, "old", rel="作者A/老笔记")
    works_file = tmp_path / "works.json"
    works_file.write_text(json.dumps({"works": _works("old", "new")}, ensure_ascii=False),
                          encoding="utf-8")

    fetched: list[str] = []

    def fake_parse(note_id, token, cookie="", verbose=False):
        fetched.append(note_id)
        return {"note_id": note_id, "url": f"https://x/{note_id}", "kind": "图文",
                "title": "新笔记", "desc": "d", "tags": [],
                "author": {"nickname": "作者B", "user_id": "u"},
                "interact": {"likes": 111, "favorites": 22, "comments": 3, "shares": 4},
                "images": [], "live_photos": [], "video": [], "cover": ""}

    downloaded: list[str] = []

    def fake_media(data, out_dir, **kwargs):
        downloaded.append(str(out_dir))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "note.json").write_text(json.dumps(
            {"note_id": data["note_id"], "media": [{"kind": "image", "bytes": 2048,
                                                    "path": "001.jpg"}]}), encoding="utf-8")
        return [{"kind": "image", "bytes": 2048, "path": "001.jpg", "url": "u"}]

    monkeypatch.setattr(sync.note, "parse_note", fake_parse)
    monkeypatch.setattr(sync.note, "download_media", fake_media)
    monkeypatch.setattr(sync, "gap", lambda: 0)          # 测试里不等节流

    stats = sync.sync(works_file, notes_dir, cookie="C", max_downloads=5)

    assert fetched == ["old", "new"], "每条都读了一次笔记页"
    assert downloaded == [str(notes_dir / "作者B" / "新笔记")], "新作品按「作者/标题」落盘"
    saved = json.loads(works_file.read_text(encoding="utf-8"))["works"]
    for w in saved:
        assert (w["likes"], w["favorites"], w["comments"], w["shares"]) == (111, 22, 3, 4)
        assert w["detail_fetched"] is True
    old_record = json.loads(
        (notes_dir / "作者A" / "老笔记" / "note.json").read_text(encoding="utf-8"))
    assert old_record["interact"]["likes"] == 111, "已下载的本地记录也要刷新（按索引找到它）"
    assert old_record["interact_updated_at"] > 0
    assert stats["updated"] == 1 and stats["downloaded"] == 1 and stats["downloaded_files"] == 1


def test_sync_survives_single_failure(tmp_path, monkeypatch):
    """单条读不到不能拖垮整批：记下 sync_error 继续下一条。"""
    notes_dir = tmp_path / "notes"
    works_file = tmp_path / "works.json"
    works_file.write_text(json.dumps({"works": _works("bad", "good")}, ensure_ascii=False),
                          encoding="utf-8")
    calls = {"n": 0}

    def fake_parse(note_id, token, cookie="", verbose=False):
        calls["n"] += 1
        if note_id == "bad":
            raise RuntimeError("笔记已删除")
        return {"note_id": note_id, "url": "u", "kind": "图文", "title": "t", "desc": "",
                "tags": [], "author": {}, "interact": {"likes": 7}, "images": [],
                "live_photos": [], "video": [], "cover": ""}

    monkeypatch.setattr(sync.note, "parse_note", fake_parse)
    monkeypatch.setattr(sync, "gap", lambda: 0)

    stats = sync.sync(works_file, notes_dir, max_downloads=0)
    saved = {w["id"]: w for w in json.loads(works_file.read_text(encoding="utf-8"))["works"]}
    assert stats["failed"] == 1 and stats["updated"] == 1
    assert "笔记已删除" in saved["bad"]["sync_error"]
    assert saved["good"]["likes"] == 7
    assert "sync_error" not in saved["good"]
