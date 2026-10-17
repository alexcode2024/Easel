# 对标账号与小红书取数

本页说明「对标账号」模块、小红书 cookie 取数、单条笔记全量下载与刷新同步的**原理、约束、接口与参数**。
涉及文件：`web/app.py`（后端）、`skills/shared/scripts/{benchmark_fetch,xhs_fetch,xhs_note,xhs_sync}.py`（采集侧）、
`web/frontend/src/components/{BenchmarksPage,CookieModal,AccountsPage}.tsx`（界面）。

**一句话定位**：只看平台**公开**可见的信息，不登录对方账号、不绕过平台风控、不批量搬运内容。

---

## 1. 对标账号模块

### 1.1 数据流

```
浏览器 ──POST /api/benchmarks/{bid}/fetch──▶ 后端 benchmark_fetch.py fetch
                                              │  ① 平台适配器抓列表（含账号指标）
                                              │  ② 写 works.json / accounts.json / history.jsonl
                                              │  ③（小红书）xhs_sync.py 同步素材
                                              ▼
前端 ◀──GET /api/benchmarks、/api/benchmarks/{bid}── 读本地快照（**列表接口永不触发抓取**）
```

设计原则：**读取与抓取彻底分离**。页面上任何一次刷新列表都只读本地 JSON，只有显式点「获取最新作品」才会打平台。

### 1.2 平台适配器接口

`benchmark_fetch.py` 里每个平台是一个 `Adapter` 实例，注册进 `ADAPTERS`，按 `PLATFORM_ORDER = ("xiaohongshu", "douyin", "tiktok")` 输出：

| 成员 | 说明 |
|---|---|
| `key` / `name` / `order` | 平台标识、中文名、展示顺序（未接的平台 `order=100`） |
| `auto` / `experimental` | 是否支持自动抓取 / 是否实验性（前端会打「未实测」标签） |
| `fields` | **能力矩阵**：`followers`、`likes`、`favorites`、`comments`、`posts`、`cover`、`work_likes`、`work_favorites`、`work_comments`、`work_plays`、`work_published_at`、`work_detail_manual` |
| `id_label` / `id_hint` | 账号 ID 的形态与示例（前端表单提示） |
| `parse_target(raw)` | 把主页链接/ID 归一成平台 ID，非法输入直接报错 |
| `fetch(target, ...)` | 抓列表，返回 `Snapshot`（账号指标 + 作品列表 + 覆盖说明 + 告警） |
| `fetch_work_detail(...)` | 单条深挖（按需补收藏/评论） |

**能力矩阵必须如实**：拿不到就标 `false`，界面显示「—」。例如小红书 `work_favorites/work_comments=true` 但 `work_detail_manual=true`（要逐条读详情页），抖音/TikTok 的收藏评论随列表返回所以 `work_detail_manual` 为假。

### 1.3 存储布局

```
outputs/_benchmarks/
├── accounts.json                 # 账号列表 + 最近一次快照的指标/覆盖说明/告警
├── <bid>/works.json              # 作品列表（整批替换，但深挖数据按 note id 合并保留）
├── <bid>/history.jsonl           # 账号级历史：ts / followers / likes / work_count / median_likes
├── <bid>/fetch.json              # 抓取与同步的实时状态（前端轮询它）
└── <bid>/covers/*.jpg            # 封面本地缓存（按图片 fileId 命名，重复抓取不重下）
```

以 `_` 开头 → **不会出现在内容库**。`history.jsonl` 只落账号级指标，不落作品级明细。

### 1.4 约束（都是踩过坑之后定下来的）

- **串行**：同一账号同时只跑一个任务（`BENCH_RUNNING`），批量刷新也是串行，绝不并发——并发读会招平台风控。
- **冷却 300 秒**：`BENCH_COOLDOWN`，同一账号两次抓取的最小间隔；冷却期内接口返回 429，前端显示倒计时。
- **小红书最多 50 条**：主页 SSR 一次只给 30 条，多出来的走签名分页接口；再往上不做（避免变成全量搬运）。
- **命中风控立即停**：识别到 `300012`、「风险」、「风控」等信号就熔断，**不重试**——重试会把软限流升级成数小时封禁。
- **超时**：单账号抓取 `BENCH_FETCH_TIMEOUT=300s`；同步 `BENCH_SYNC_TIMEOUT=3600s`。
- **数据保护**：
  - 刷新时按作品 ID **保留已深挖的收藏/评论**，并自愈「标记完成但没数据」的残留；
  - cookie 失效被判游客态时**保留上次的作品列表**（含作品 ID），不用无 ID 的批次覆盖；
  - 抓取失败不清空旧数据，只在 `last_error` 里记原因。

### 1.5 HTTP 接口

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/benchmarks` | — | 账号列表（含 `fetch` 状态、`cooldown_left`、`running`） |
| POST | `/api/benchmarks` | `platform`、`target`、`remark`、`tags` | 新增账号（先 `parse_target` 归一，重复会拒绝） |
| DELETE | `/api/benchmarks/{bid}` | — | 删除账号 + 作品 + 封面缓存（**素材目录不删**） |
| GET | `/api/benchmarks/{bid}` | — | 详情：账号 + 作品列表（含 `downloaded`/`download` 摘要）+ 覆盖说明 + 状态 |
| POST | `/api/benchmarks/{bid}/fetch` | `sync`（默认 true）、`max_downloads` | 触发抓取（异步）；小红书顺带同步素材 |
| GET | `/api/benchmarks/{bid}/fetch/status` | — | 抓取/同步进度（`state`、`message`、`running`、`cooldown_left`） |
| POST | `/api/benchmarks/fetch-all` | — | 串行刷新全部可自动抓取的账号 |
| GET | `/api/benchmarks/fetch-all/status` | — | 批量刷新进度 |
| POST | `/api/benchmarks/{bid}/works/{wid}/detail` | — | 单条深挖（补收藏/评论） |
| POST | `/api/benchmarks/{bid}/works/detail-batch` | `limit`（0=全部待补，上限 40） | 批量深挖，串行 + 6 秒间隔，风控即停 |
| POST | `/api/benchmarks/{bid}/works/{wid}/download` | `quality`（默认 `lowest`） | 下载这条笔记的全部素材与记录 |
| GET | `/api/benchmarks/platforms` | — | 平台能力矩阵 |

> `state` 取值：`running` / `done` / `error` / `risk`。前端只在 `running` 期间轮询。

---

## 2. 小红书取数：手动 cookie + 纯 HTTP

### 2.1 为什么不再用浏览器

实测结论（2026-10 多次抓包）：

- **无头 Playwright Chromium 会被判风险**：登录页直接返回 `300012`（IP 风险限制），二维码都拿不到。
- **有头浏览器可以，但仍然重**：每次抓取弹窗 30 秒，且「带 cookie 的主页请求」拿到的是 **10KB 的登录态空壳页**（没有 `__INITIAL_STATE__`），照样读不到数据。
- **伪装指纹的纯 HTTP 最稳**：`curl_cffi` + `impersonate=chrome` 伪装 TLS/JA3 指纹，1 秒内拿到数据，不触发 300012。

所以小红书的读数据链路**完全不走浏览器**：不要扫码、不弹窗、不需要无头内核。

### 2.2 三个接口各管一件事

| 用途 | 接口 | 说明 |
|---|---|---|
| **会话校验 / 我是谁** | `GET /api/sns/web/v2/user/me` | 返回 `nickname`、`red_id`、`user_id`、头像。约 1 秒，是「检查会话」按钮的实现 |
| **账号指标** | `GET /api/sns/web/v1/user/otherinfo?target_user_id=` | 粉丝（精确整数）、获赞、收藏、作品数、昵称、头像。**带 cookie 时主页 SSR 是空壳，指标只能走这里** |
| **作品列表（分页）** | `GET /api/sns/web/v1/user_posted?user_id=&cursor=&num=30` | 游标分页；字段是**顶层蛇形**（`note_id`/`display_title`/`interact_info`），与 SSR 的驼峰形态不同 |
| **单条笔记详情** | 笔记页 HTML 的 `__INITIAL_STATE__` | `note.noteDetailMap[-1].note` |

### 2.3 解析原理（两个必须知道的坑）

**坑一：state 不是合法 JSON。** 里面有 `new Map([])` 这类 JS 语法，`json.loads` 必然失败——本仓库早期读不到详情数据就是这个原因。正确做法：替换 `new Map([])` → `[]`、裸 `undefined` → `null`，然后**用 YAML 解析**（`yaml.safe_load`）。

**坑二：签名覆盖查询串，URL 必须由签名库生成。** 用 `Xhshow().sign_headers_get(...)` 生成 `x-s`/`x-s-common`/`x-t`，但请求 URL 必须用 `Xhshow().build_url(endpoint, params)` 生成——
自己 `urlencode` 拼出来的参数顺序与库内部不一致时会稳定返回 **406**。另外 `xsec_token`/`xsec_source` 传空串即可（浏览器真实请求里就是空的）。

### 2.4 凭证约束

- **一份 cookie 管两件事**：`www.xiaohongshu.com` 的 `web_session` 用于读数据与校验；`creator.xiaohongshu.com` 的 `galaxy_creator_session_id` / `customer-sso-sid` 用于发布/后台，**手动发布则无需配置**。
- **cookie 是凭证**：存在 `outputs/_login/xiaohongshu.cookie`（纯文本、已 gitignore）；接口**只回状态，绝不回 cookie 值**；前端密码框式输入。
- **会过期、会被降级**：平台会因请求量把会话降级成游客态（持续数小时）。降级期间：粉丝显示「—」（平台只给「1万+」占位值）、作品列表拿不到 ID（无原文链接、无法深挖），但累计获赞/收藏来自精确字段仍然有效。
- **不绕过风控**：被拦就是被拦，工具只负责如实报告并停手。

### 2.5 接口与参数

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/benchmarks/cookies` | 各平台 cookie 状态（`saved`、`cookie_count`、`has_required`、`missing`、`saved_at`） |
| POST | `/api/benchmarks/cookies/{platform}` | body `{"cookie": "..."}`；小红书**必须含 `web_session`**，否则 400 |
| DELETE | `/api/benchmarks/cookies/{platform}` | 清除 |
| GET | `/api/accounts/{platform}/session` | 会话健康：`web`（主站读数据）与 `creator`（创作平台发布）分开返回；`?check=1` 强制实测（约 1 秒） |

脚本侧：`xhs_publish.py whoami --cookie-file <f>`、`benchmark_fetch.py fetch --cookie-file <f>`、
`xhs_note.py download --notes-dir <d> --cookie-file <f>`。

---

## 3. 单条笔记全量下载

### 3.1 取什么

- **元数据**：标题、正文、标签、类型（视频/图文/图集）、发布时间、最后更新时间、作者昵称/ID、点赞/收藏/评论/分享。
- **图片**：`imageList[].urlDefault` → 抠出 token → `sns-img-bd.xhscdn.com/{token}`（**原始上传文件**，多为 HEIC）。
- **动图**：`imageList[].stream.*[0].backupUrls[0]` / `masterUrl`。
- **视频**：`video.media.stream.*[]`（EF4~EF7 多档），按高度挑档；`video.consumer.originVideoKey` 是真·原片，但**实测今天的 payload 里是 `None`**，所以"按高度挑"才是有效路径。

### 3.2 下载工程细节

- **按文件头判后缀**（`guess_ext`），不信 URL 扩展名：JPEG/PNG/WebP/HEIC/MP4/MOV/WebM 各取魔数。
- **优先可播放的 mp4**：同一档常同时给 `.m4s`（H.265 分段流）和 `.mp4`；`.m4s` 在备份域上会被掐断（`curl: (52) Empty reply`）且播放器兼容性差。
- **失败自动换地址**：每个变体保留 `backupUrls` 全部 + `masterUrl`，一个不行换下一个再试。
- **单文件上限 220MB**（`MAX_FILE_BYTES`）：超了直接中止而不是硬下（2160p 实测 141MB，留余量）。
- **先写 `.part` 再改名**：中断不会留下半个文件冒充成品。

### 3.3 目录与命名

```
outputs/_notes/
├── index.json                       # {笔记ID: "作者/标题"} —— 定位靠它，不靠目录名
├── 大厂升升🎬/
│   └── 💰副业思路：自由不上班，她们是怎样创业的？/
│       ├── note.md                  # 人读：正文/标签/互动/文件清单/版权说明
│       ├── note.json                # 机读：结构化记录
│       └── media/                   # 001.jpg 001.heic live_001.mp4 video_720.mp4
```

- **文件名清洗**：Windows 非法字符（`\ / : * ? " < > |`）→ 下划线；**中文与 emoji 保留**；截断 48 字符；全角标点合法所以保留。
- **重名**：同作者两条笔记标题相同时，第二条自动加 `-<笔记ID前8位>`。
- **不搬家**：已下载过的笔记沿用索引里的目录，标题后来改了也不移动（否则文件会散落两处）。

### 3.4 约束

- **单条按需**：界面上是作品卡上的按钮，不做「把某个账号全部搬走」。
- **默认最低画质**（`quality=lowest`）：省磁盘与带宽；可选 `1080`、`best`、具体高度。
- **版权**：`note.md` 里固定写明「内容版权归原作者所有；仅用于拆解研究，转载/商用请先获得授权并注明出处」。
- 素材目录以 `_` 开头，**不进内容库**（避免把自己的和别人的素材混在一起）。

---

## 4. 刷新同步

### 4.1 规则

点「获取最新作品（含同步）」后，除了抓列表，还会逐条：

- **没下载过** → 读笔记页 + 下载素材（媒体文件）；
- **已下载** → 只重读笔记页，更新点赞/收藏/评论/分享，**并刷新本地 `note.json` 里的互动数据**（让归档不停在旧数字）。

### 4.2 预算（防止一次拉 1–2GB）

`max_downloads`（界面「同步下载」下拉）：`0` = 只更新数据不下素材 / `5` / `10`（**默认**）/ `20` / `999`（全部补齐）。
超预算的作品只更新数据，并在完成消息里提示「还有 N 条未下载（本次预算 X 条）」。

### 4.3 节流与落盘

- 每条之间 **对数正态间隔，均值 6 秒、最小 1 秒**（沿用成熟工具的标定，不自己造更激进的参数）。
- **逐条落盘** `works.json`：中途中断/失败不丢已完成部分。
- **单条失败不拖垮整批**：记 `sync_error` 继续下一条。
- 进度直接写进 `outputs/_benchmarks/<bid>/fetch.json`（前端本来就在轮询这个文件），所以界面无需额外接口就有「同步 7/50：已更新互动数据」。

### 4.4 参数

| 位置 | 参数 | 默认 | 含义 |
|---|---|---|---|
| `POST /api/benchmarks/{bid}/fetch` | `sync` | `true` | 是否在抓列表后同步素材 |
| 同上 | `max_downloads` | `10` | 本次最多下载几条新作品 |
| `POST .../works/{wid}/download` | `quality` | `lowest` | 画质：`lowest` / `best` / `1080` 等 |
| `POST .../works/detail-batch` | `limit` | `0`（全部待补） | 批量深挖条数，上限 40 |
| `xhs_sync.py` | `--works-file`、`--notes-dir`、`--progress-file`、`--max-downloads`、`--quality`、`--cookie-file` | — | 脚本直调 |
| `xhs_note.py` | `--note-id`、`--xsec-token`、`--notes-dir`/`--out-dir`、`--quality`、`--no-images`/`--no-video`/`--no-live`、`--cookie-file` | — | 脚本直调（`parse` / `download` 两个子命令） |

---

## 5. 风控与合规红线

1. **不绕过平台风控**：命中风控立即停止、不重试、不换手段继续打；不伪装成真人去骗过检测。
2. **串行 + 冷却**：任何时刻同一平台只有一条请求链在跑；抓取有 5 分钟冷却，同步/深挖有 6 秒均值的抖动间隔。
3. **不做批量搬运**：单条按需下载；主页作品列表最多取最新 50 条；不提供"整号导出"。
4. **不登录他人账号、不模拟互动**：只读公开页面，不点赞、不关注、不发评论。
5. **如实标注**：拿不到的字段显示「—」而不是估算；游客态明确告警；导出文件带版权声明。
6. **凭证不外泄**：cookie 明文仅存本机 `outputs/`（gitignore），接口不回值，日志不打印。

---

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 签名接口 **406** | 请求 URL 不是 `build_url()` 生成的，参数顺序与签名不一致 | 用 `Xhshow().build_url()` 生成 URL，不要自己拼接 |
| 列表接口 **461**（正文却 `success=true`） | 平台对该接口限流（请求太密） | 停手等数小时，不要反复刷；期间作品列表会退回游客批次并明确告警 |
| 登录页 **300012** | 用了无头浏览器内核 | 小红书链路已改为纯 HTTP，不应再出现；若出现说明走了浏览器分支 |
| 粉丝显示「—」、作品无「原文 ↗」 | 当前是游客态（cookie 过期或被降级） | 重新粘贴 cookie 或等降级结束；降级期间精确的获赞/收藏仍有效 |
| 视频下载 `curl: (52) Empty reply` | 选中了备份域的 `.m4s` 分段流 | 已修：优先 `.mp4` + 失败自动换备用地址 |
| 作品卡「✓ 已下载」消失 | `index.json` 被删或素材目录被移走 | 重新下载即可（会重新写索引） |

---

## 7. 已知限制

- **不做全量翻页**：小红书作品列表上限 50 条（平台单次给 30，分页补到 50 即停）。
- **作品级收藏/评论需逐条深挖**：主页列表只给点赞，深挖是"每条一次请求"，所以默认不放进刷新流程（有单独按钮 + 预算）。
- **小红书无头不可用**：任何依赖 Playwright 无头内核的小红书操作都会被判 300012，别再尝试。
- **抖音/TikTok 仍走浏览器适配器**：它们的收藏/评论随列表返回，但取数依赖有头浏览器（抖音需 `sec_uid`，TikTok 需能访问的网络）。
- **素材不含视频号/知乎等未接平台**：其余平台在能力矩阵里是占位（`auto=false`）。
