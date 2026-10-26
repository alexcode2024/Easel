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
                                              │  ③（显式 sync=true）xhs_sync.py 同步素材
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
├── <bid>/covers/*.jpg            # 封面本地缓存（按图片 fileId 命名，重复抓取不重下）
├── monitor_schedule.json         # 监控的全局设置：每天几点采样（默认 09:00）
├── <bid>/monitor.json            # 作品表现监控：每篇作品的按日采样序列（见第 5 节）
└── monitor_run.json              # 当前/最近一轮采样的进度（前端轮询它）
```

以 `_` 开头 → **不会出现在内容库**。`history.jsonl` 只落账号级指标，不落作品级明细。

### 1.4 约束（都是踩过坑之后定下来的）

- **共用一个并发槽位**：当前项目仅配置一份小红书 Cookie/浏览器登录态，所以所有小红书任务共用队列，不按对标账号 ID 分锁。刷新、深挖、下载、监控（含首采）、登录/会话校验、创作数据和发布相互串行；扫码登录占用槽位直到浏览器进程退出。
- **脚本直调保护**：`xhs_access.py` 的跨进程文件锁覆盖小红书采集、评论、登录、发布等 CLI 入口，避免 Agent 直调绕开 Web。取消 Web 任务时，要等正在执行的线程/子进程结束才释放槽位。排队任务只保存在内存，服务重启后不自动重放。
- **冷却 300 秒**：`BENCH_COOLDOWN`，同一账号两次抓取的最小间隔；冷却期内接口返回 429，前端显示倒计时。
- **默认只取 20 条列表**：刷新不默认深挖、同步或下载；已有详情优先复用，监控才主动读取新快照。批量深挖/单轮监控最多 20 条，显式同步最多读取 8 条详情、默认补下 5 条新作品素材。
- **共享熔断**：HTTP `429` / `461`、`300012`、`NeedVerify`、验证码及访问频繁等信号会触发一小时暂停，状态保存到 `outputs/_benchmarks/xhs_access.json`，重启不清除；等待中的任务终止，本轮不重试，也不换成游客身份继续访问。冷却结束只是允许新任务，不代表平台已经解除限制。
- **请求节流**：小红书数据请求共用持久化的请求完成时间，下一次请求的目标间隔统一为随机 8–15 秒；首个请求也等待，任务自身等待及网络耗时可能延长实际间隔。默认 CLI 数据请求预算为 20，列表抓取为 8，显式同步详情为 8。静态资源和媒体文件不计入详情请求预算。
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
| POST | `/api/benchmarks/{bid}/fetch` | `sync`（默认 false）、`max_downloads` | 默认只刷新列表；显式开启同步才读取详情/素材 |
| GET | `/api/benchmarks/{bid}/fetch/status` | — | 抓取/同步进度（`state`、`message`、`running`、`cooldown_left`） |
| POST | `/api/benchmarks/fetch-all` | — | 串行刷新全部可自动抓取的账号 |
| GET | `/api/benchmarks/fetch-all/status` | — | 批量刷新进度 |
| POST | `/api/benchmarks/{bid}/works/{wid}/detail` | — | 单条深挖（补收藏/评论） |
| POST | `/api/benchmarks/{bid}/works/detail-batch` | `limit`（0=待补，上限 20） | 批量深挖，串行节流，风控即停 |
| GET | `/api/xhs/access/status` | — | 并发上限、当前任务、排队位置、熔断原因与恢复时间；不返回凭证 |
| POST | `/api/benchmarks/{bid}/works/{wid}/download` | `quality`（默认 `lowest`） | 下载这条笔记的全部素材与记录 |
| POST | `/api/benchmarks/{bid}/works/{wid}/monitor` | body `{"enabled": bool}` | 开/关这篇作品的表现监控（开启时顺手记当天第一个点） |
| GET | `/api/benchmarks/{bid}/works/{wid}/trend` | `days`（默认 15，上限 15） | 最近 N 天的点赞/收藏/评论三条序列 |
| GET | `/api/benchmarks/monitor/schedule` | — | 每天几点采样 + 下次采样时间 + 当前监控数 |
| PUT | `/api/benchmarks/monitor/schedule` | body `{"hour": 0-23, "minute": 0-59}` | 改每日采样时刻 |
| GET | `/api/benchmarks/monitor/status` | — | 采样进度（`state`/`done`/`total`/`running`） |
| POST | `/api/benchmarks/monitor/run` | `force`（默认 true）、`bid`（可选） | 立刻采一轮（与每日采样同一条串行链路） |
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
| **会话校验 / 我是谁** | `GET /api/sns/web/v2/user/me` | 返回 `nickname`、`red_id`、`user_id`、头像。「检查会话」也遵守队列及 8–15 秒随机请求间隔 |
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
| GET | `/api/accounts/{platform}/session` | 会话健康：`web`（主站读数据）与 `creator`（创作平台发布）分开返回；`?check=1` 强制实测，也需等待队列及请求间隔 |

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

- **单条按需**：界面上是作品行「操作」列里的按钮，不做「把某个账号全部搬走」。
- **默认最低画质**（`quality=lowest`）：省磁盘与带宽；可选 `1080`、`best`、具体高度。
- **版权**：`note.md` 里固定写明「内容版权归原作者所有；仅用于拆解研究，转载/商用请先获得授权并注明出处」。
- 素材目录以 `_` 开头，**不进内容库**（避免把自己的和别人的素材混在一起）。

---

## 4. 刷新同步

### 4.1 规则

默认点「获取最新作品」只更新列表。界面选择同步 3 / 5 条，或 API 显式传 `sync=true` 后才会逐条：

- **没下载过** → 读笔记页 + 下载素材（媒体文件）；
- **已下载** → 只重读笔记页，更新点赞/收藏/评论/分享，**并刷新本地 `note.json` 里的互动数据**（让归档不停在旧数字）。

### 4.2 预算（防止一次拉 1–2GB）

界面默认「仅刷新列表」，可显式选最多 3 / 5 条新作品素材。API 的 `max_downloads` 默认 5；仅在 `sync=true` 时生效，传 0 表示只更新详情、不下载。
同步还受 `--max-requests=8` 限制，已下载作品也会消耗详情读取预算，达到预算立即停止，并保留已完成的数据。

### 4.3 节流与落盘

- 保留原有任务级抖动间隔，并对实际小红书数据请求施加全局 8–15 秒随机节流。
- **逐条落盘** `works.json`：中途中断/失败不丢已完成部分。
- 普通单条失败记 `sync_error`；风控/验证码立即停止整批，不访问后续作品。
- 进度直接写进 `outputs/_benchmarks/<bid>/fetch.json`（前端本来就在轮询这个文件），所以界面无需额外接口就有「同步 7/50：已更新互动数据」。

### 4.4 参数

| 位置 | 参数 | 默认 | 含义 |
|---|---|---|---|
| `POST /api/benchmarks/{bid}/fetch` | `sync` | `false` | 是否在抓列表后同步素材 |
| 同上 | `max_downloads` | `5` | 本次最多下载几条新作品 |
| `POST .../works/{wid}/download` | `quality` | `lowest` | 画质：`lowest` / `best` / `1080` 等 |
| `POST .../works/detail-batch` | `limit` | `0`（待补） | 批量深挖条数，上限 20 |
| `xhs_fetch.py` | `--request-budget` | `8` | 本轮列表/指标数据请求上限 |
| `xhs_sync.py` | `--max-requests` | `8` | 本轮详情读取上限，已下载作品同样计数 |
| `xhs_sync.py` | `--works-file`、`--notes-dir`、`--progress-file`、`--max-downloads`、`--quality`、`--cookie-file` | — | 脚本直调 |
| `xhs_note.py` | `--note-id`、`--xsec-token`、`--notes-dir`/`--out-dir`、`--quality`、`--no-images`/`--no-video`/`--no-live`、`--cookie-file` | — | 脚本直调（`parse` / `download` 两个子命令） |

---

## 5. 作品表现监控（15 天趋势）

### 5.1 为什么是「自己记」

平台的互动数（点赞/收藏/评论）是**累计快照**，没有任何历史接口——小红书、抖音、TikTok 都一样。
所以「趋势」只能靠按天采样自己攒：每篇作品一个监控开关，开了之后每天固定时刻记一笔，
最多连续记 15 天，然后在作品行点「趋势」（或点整行）看折线：**点赞 / 收藏 / 评论**三条，
平台公开播放量时再多一条**播放量**（即阅读/观看次数，目前只有 TikTok 声明 `work_plays`；
小红书没有这个字段，抖音的 `play_count` 对他人作品恒为 0、不是公开数据，都不画）。

### 5.2 一天一条，缺的不是 0

- **按自然日归档**：采样按「YYYY-MM-DD」落一条，同一天重采**覆盖**（不会一天长出好几个点）。
- **x 轴从第一个有记录的日子开始画**（不是固定贴着今天往回数满 15 天）：刚开始监控时第一天就在
  最左侧，只有一天数据时也只有一格；还在监控中时右端画到今天，停掉/到期后右端画到最后一个点，
  不留一排空刻度。`days` 参数只是「最多显示几天」的上限。
- **没采到就是缺口**：那天没读到 → 序列里是 `null`，**不补 0、不重复前一天**，也不显示数据点。
  这是本仓库「不估算」红线在图表上的落地。
- **真实点之间照常连线**：按日期顺序把相邻两个真实观测值连成折线；中间隔了没采到的日子，
  那一段用**虚线**表示跨越连接（悬停会写明两端日期），所以「昨天有、今天没采、明天有」也能看出走势，
  而不会只剩几个孤立圆点。
- **失败也留痕**：采样失败记一条 `ok=false` + 原因，界面显示「最近一次采样有问题」；
  但如果今天已经读到过真实数字，之后的重采失败**不会**把它覆盖成空值（只更新错误信息）。
- **y 轴按实际区间缩放**：累计数从 1000 涨到 1200，从 0 起画就是一条水平线，所以上下界都标在轴上。

### 5.3 「固定时间」与补采

- 默认每天 **09:00**（本地时区），可在趋势弹窗里改（`PUT /api/benchmarks/monitor/schedule`）。
- 进程当时没在跑怎么办？调度器每 60 秒检查一次：**过了当天的固定时刻、今天又还没采过 → 立刻补采**。
  本地 Web 服务本来就不会 24 小时开着，没有补采语义的话，用户第二天上午才打开 Easel 就会永久缺一天。
- 只补**当天**那一次：不会一次性回填好几天的空档（那些天就是没有数据，如实留空）。
- 一天最多一条：补采/手动重采都落在同一天那一条上。

### 5.4 15 天上限

窗口 = `开始那天` 起 15 个自然日（含当天）。到期判定**不用定时器**，而是每次读取时按日期算
（进程重启、机器休眠都不会让状态漂），到期后自动置 `enabled=false` + `expired=true`，
已记录的点**保留**（能回看），想继续就点「重新开始 15 天」（新一轮窗口从当天重算）。

### 5.5 两种采样路径（按平台能力选，不是每个平台都逐条打）

| 平台类型 | 判断依据 | 采样方式 | 代价 |
|---|---|---|---|
| 详情型（小红书） | `fields.work_detail_manual = true` | 逐条读笔记详情页（纯 HTTP） | 1 条 = 1 个请求，条间**对数正态间隔，均值 6 秒、最小 1 秒**（与深挖同一套标定） |
| 列表型（抖音/TikTok） | `work_detail_manual` 为假 | 一个账号刷**一次**主页，指标从列表里取 | 整号一次全量；采样后**打上 5 分钟冷却**，避免用户紧接着再刷一次 |

列表型平台的播放量（TikTok）跟点赞/收藏/评论一起从 `item_list` 回来，**不额外请求**；
详情型平台没有播放量字段，那一条序列就不会出现。

两条路径都遵守既有约束：串行（同一时刻只跑一轮，`BENCH_MONITOR_TASK`）、命中风控（`300012`/「风险」/「风控」/`461`）
**立即停手不重试**、逐条落盘（中途中断不丢已采到的点）。

开启监控时：详情型平台会**同步采第一个点**（约 0.5–1 秒，与「单条深挖」同量级），
列表型平台只登记、等每日刷新（为一个点去刷整个主页不值得）。

### 5.6 参数

| 位置 | 参数 | 默认 | 含义 |
|---|---|---|---|
| `POST .../works/{wid}/monitor` | body `enabled` | — | 开/关监控 |
| `GET .../works/{wid}/trend` | `days` | `15` | 最多显示几天（上限 15）；实际区间由数据决定，从第一个有记录的日子起画 |
| `PUT /api/benchmarks/monitor/schedule` | `hour` / `minute` | `9` / `0` | 每日固定采样时刻 |
| `POST /api/benchmarks/monitor/run` | `force` / `bid` | `true` / 空 | 立刻采一轮（`force=true` 覆盖今天的点），可按账号限定 |
| `web/app.py` | `BENCH_MONITOR_DAYS` | `15` | 连续记录上限 |
| 同上 | `BENCH_MONITOR_MAX_POINTS` | `30` | 落盘保留的点数上限（窗口外的老点留着，重开不必从零） |
| 同上 | `BENCH_MONITOR_TICK` | `60` | 调度器检查间隔（秒） |
| 同上 | `BENCH_MONITOR_STARTUP_DELAY` | `5` | 服务启动后等几秒再开始检查（避开启动期） |
| 环境变量 | `EASEL_BENCH_MONITOR` | 开 | 置 `0`/`off`/`false` 可关掉调度循环（测试/CI 用） |

### 5.7 界面

- **作品列表**：一行一篇作品——左侧封面 + 三行字（标题 / 发布时间 / 更新时间），后面依次是阅读量、点赞、
  收藏、评论四个指标列，再后面是「监控」开关和「操作」（趋势 / 下载 / ⋯ 菜单里放拆解·存选题·深挖）。
  三行字下面还有一行「原文 ↗」——点它在新页签打开平台原文，不会顺带打开趋势弹窗。
  游客态没有作品 ID 时，趋势/监控这两个入口不出现。「更新时间」= 这条作品的表现数据最近一次采集时间。
- **账号详情**：开了监控才出现一行概况「📈 表现监控 · 监控中 N 篇 · 每天 09:00 采样 · 下次 … · ⟳ 立即采样」。
- **趋势弹窗**：标题 + 监控条（状态/已记录 N/15 天/改时间/立即采样）+ 折线：点赞 / 收藏 / 评论，
  平台公开播放量时多一条播放量（TikTok）。x 轴从第一个有记录的日子起画（第一天贴最左），
  右端在监控中时标「今天」。平台不公开的指标（如某些平台的收藏数、小红书的播放量）
  显示「该平台不公开」，**不画空图、不用别的字段顶替**。

---

## 6. 风控与合规红线

1. **不绕过平台风控**：命中风控立即停止、不重试、不换手段继续打；不伪装成真人去骗过检测。
2. **串行 + 冷却**：本项目中同一小红书登录态只有一条任务链运行；抓取有 5 分钟冷却，数据请求另有 8–15 秒随机共享节流。外部浏览器、其他项目及其他机器的访问不在本项目控制范围内；串行只能降低风险，不能保证不会被平台限制。
3. **不做批量搬运**：单条按需下载；主页作品列表最多取最新 50 条；不提供"整号导出"。
4. **不登录他人账号、不模拟互动**：只读公开页面，不点赞、不关注、不发评论。
5. **如实标注**：拿不到的字段显示「—」而不是估算；游客态明确告警；导出文件带版权声明。
6. **凭证不外泄**：cookie 明文仅存本机 `outputs/`（gitignore），接口不回值，日志不打印。

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 签名接口 **406** | 请求 URL 不是 `build_url()` 生成的，参数顺序与签名不一致 | 用 `Xhshow().build_url()` 生成 URL，不要自己拼接 |
| 列表接口 **461**（正文却 `success=true`） | 平台对该接口限流（请求太密） | 停手等数小时，不要反复刷；期间作品列表会退回游客批次并明确告警 |
| 登录页 **300012** | 用了无头浏览器内核 | 小红书链路已改为纯 HTTP，不应再出现；若出现说明走了浏览器分支 |
| 粉丝显示「—」、作品无「原文 ↗」 | 当前是游客态（cookie 过期或被降级） | 重新粘贴 cookie 或等降级结束；降级期间精确的获赞/收藏仍有效 |
| 视频下载 `curl: (52) Empty reply` | 选中了备份域的 `.m4s` 分段流 | 已修：优先 `.mp4` + 失败自动换备用地址 |
| 作品行「✓ N」消失 | `index.json` 被删或素材目录被移走 | 重新下载即可（会重新写索引） |
| 监控天数不涨 | 当天还没到固定时刻，或 Easel Web 服务当天没在运行 | 服务起来后 1 分钟内会补采当天这一次；也可以点「⟳ 立即采样」 |
| 趋势弹窗里某条线是空的 | 平台不公开这个指标（如部分平台的收藏数），或还没有采样点 | 看弹窗里的说明；开监控后每天自动记一条 |
| 采样状态卡在 `risk` | 命中平台风控 | 数小时内别再点采样；已完成的部分都已落盘，不会再重试打平台 |
| 第 16 天起不再采样 | 15 天窗口已满，自动停止（`expired`） | 弹窗里点「重新开始 15 天」开新一轮 |

---

## 8. 已知限制

- **不做全量翻页**：小红书作品列表默认只取最近 20 条，不做历史全量采集。
- **作品级收藏/评论需逐条深挖**：主页列表只给点赞，深挖是"每条一次请求"，所以默认不放进刷新流程（有单独按钮 + 预算）。
- **小红书无头不可用**：任何依赖 Playwright 无头内核的小红书操作都会被判 300012，别再尝试。
- **抖音/TikTok 仍走浏览器适配器**：它们的收藏/评论随列表返回，但取数依赖有头浏览器（抖音需 `sec_uid`，TikTok 需能访问的网络）。
- **作品监控只记 15 天**：平台没有互动数的历史接口，趋势是本地按天攒的；窗口满了要手动「重新开始」，旧点保留可回看。
- **监控只在服务运行时采样**：关机/关页面期间的日子不会有数据（启动后只补**当天**那一次，不回填空档）。
- **列表型平台的监控依赖整号刷新**：一次刷新约 30–60 秒并打 5 分钟冷却，所以监控它和手动刷新会互相占用。
- **素材不含视频号/知乎等未接平台**：其余平台在能力矩阵里是占位（`auto=false`），也就无法开启监控。
