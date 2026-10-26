# 新平台适配规范

本规范适用于新增社交平台，或为已有平台扩展对标抓取、自有账号数据、登录、发布和访问保护能力。实现前先确认目标业务范围，并遵循“业务层只依赖公共结构和接口，平台差异封装在适配器内部”的边界。

访问控制适配的运行机制见 [`platform-access.md`](platform-access.md)。对标账号的数据模型和适配接口目前定义在 `skills/shared/scripts/benchmark_fetch.py`。每项业务能力需单独接入与声明；注册了访问策略不代表该平台已具备登录、发布或数据抓取能力。

## 1. 分层职责

| 层 | 职责 | 平台差异允许出现的位置 |
|---|---|---|
| 通用访问层 | 按凭证资源排队、跨进程互斥、请求节奏、预算、风险熔断和状态查询 | `platform_access.py` 中的通用机制；不得增加某平台专属分支 |
| 访问策略适配层 | 声明平台访问域名、API 路径、风险信号和策略参数 | `platform_adapters.py` 注册配置；复杂信号可通过专属适配器实现 `PlatformAdapter` |
| 业务适配层 | 解析平台输入、采集平台响应，并转换为 Easel 公共业务模型 | 具体平台 `Adapter` 子类的方法内部 |
| Web / 前端 | 发起通用业务操作、展示规范化字段和已声明能力 | 不解析平台原始响应，不复制平台 API 或风控判断 |

不要在 Web 路由、共享队列或前端组件中新增 `if platform == ...` 来处理平台协议差异。平台字段转换、ID 规则、Cookie/登录态使用方式、响应解析和平台错误映射都留在对应适配器内。只有确实缺少可复用的公共行为时，才扩展共享接口；扩展时同步更新所有实现、测试和本规范。

## 2. 公共访问结构与接口

访问层定义在 `skills/shared/scripts/platform_access.py`：

- `AccessResource(platform, account_id)` 是并发和风险隔离的资源标识。`account_id` 必须稳定代表实际使用的登录态或凭证，不能填正在读取的公开目标账号 ID。多个操作使用同一份凭证时必须得到同一资源键。
- `AccessPolicy` 声明平台名、并发数、请求间隔、风险冷却、单轮请求预算和命令超时。策略值必须来自明确的业务需求与离线测试，不得称为平台保证安全的阈值。
- `AccessResult(exit_code, payload, text)` 统一承载命令或 HTTP 包装层结果；凭证、Cookie、完整请求头不得写进 `payload`、日志、异常或状态接口。
- `PlatformAdapter` 是运行时依赖的协议。优先复用 `PlatformPolicyAdapter` 的配置式实现；仅当平台风险信号或状态行为确实无法由配置描述时，才实现专属适配器。
- 所有有网络副作用的 CLI 操作都要经 `platform_access.run_cli()`；Playwright 上下文创建后调用 `install_browser_guard()`；直接 HTTP 请求放在 `session()` 中并通过 `request()` 发出。
- Web 操作要经 `_platform_access()` / `_platform_access_context()` 排队，并使用 `_run_platform_command()` 传递资源身份给子进程。不得另建平台私有队列、锁、节流器或熔断文件。

Web、CLI、后台任务和浏览器请求必须使用相同的平台键与凭证身份。当前每个平台的默认登录态使用 `account_id="default"`；未来多账号时，由账号管理层提供稳定 credential ID，再让所有入口统一传递该 ID。

## 3. 对标账号公共数据模型

如新增平台对标账号抓取，适配 `skills/shared/scripts/benchmark_fetch.py` 的 `Adapter`，返回统一的 `Snapshot` / `Work`。Web 层只消费这些结构，不消费平台原始 JSON。

### `Snapshot`

账号级公共字段包括 `platform`、`platform_name`、规范化目标 `account_id`、`nickname`、`avatar`、`bio`、`home_url`、`followers`、`likes`、`favorites`、`comments`、`posts`、`works`、`coverage`、`source`、`logged_in`、`risk`、`warnings`、`median_likes` 和 `fetched_at`。

- `followers`、`likes`、`favorites`、`comments` 使用 `Metric(raw, num, lower_bound)`；`raw` 保存平台原始显示文本，`num` 为解析后的非负整数，`lower_bound` 表示原文只是下限（例如“1万+”）。无法确认的数值用 `None`，不能用 `0` 代替未知。
- `posts` 为可确认的作品总数；若平台只提供主页当前页数量，不能把它伪装成总数，应通过 `coverage` / `warnings` 描述实际覆盖范围。
- `fetched_at` 使用 Unix 秒时间戳；作品 `published_at_ms` 使用 Unix 毫秒时间戳。
- `logged_in` 只表示本次采集结果所依赖的会话状态；匿名公开抓取应明确按实际语义设置并写明来源，不能把游客态数据标成登录态。

### `Work`

作品级公共字段包括 `id`、`title`、`url`、`cover_url`、`cover`、`published_at_ms`、`likes`、`likes_raw`、`favorites`、`comments`、`shares`、`plays`、`is_top`、`is_burst`、`median_likes`、`detail_fetched`、`detail_available` 和 `extra`。

- `id` 是平台内稳定作品 ID；缺失时用空值，不能制造 ID。若定位详情所需令牌存在，将其放进 `extra`，不要改变公共详情接口参数。
- `likes`、`favorites`、`comments`、`shares`、`plays` 都是非负整数或 `None`。平台不公开或当前接口无法验证的指标为 `None`，不是 `0`。
- `detail_available` 仅在作品 ID 和适配器所需定位信息足以支持详情读取时为真；`detail_fetched` 仅在详情读取成功后为真。
- `extra` 仅承载平台适配器再次读取所需的非敏感定位元数据。禁止 Cookie、会话令牌、签名密钥、完整响应体或不必要的个人数据。
- 跨平台新增字段前先确认它具有通用业务语义；不要将平台字段直接加到公共模型。平台专有、暂时无法通用化的数据留在 `extra`，并避免让 Web/前端依赖它。

### `Adapter` 合约

在 `benchmark_fetch.py` 中继承 `Adapter` 并注册：

- `parse_target(raw) -> str`：接受产品说明支持的 ID 或主页链接，校验后返回唯一规范格式；非法输入抛 `ValueError` 并给出可操作提示，不发网络请求。
- `fetch(target, *, limit, profile_root, proxy, cover_dir, verbose, cookie_file=None) -> Snapshot`：完成本平台采集和公共字段映射。网络、浏览器与平台响应的处理封装在此方法或其平台私有辅助方法中。
- `fetch_work_detail(target, work_id, *, profile_root, proxy, extra=None, verbose=False, cookie_file=None) -> Work`：仅在平台支持时实现；不支持时保留默认 `Unsupported` 行为。不得伪造缺失互动数据。
- `describe()` 由基类生成平台能力声明。设置 `fields`、`auto`、`experimental`、`order`、`id_label`、`id_hint` 和 `fetch_note`，确保能力矩阵准确反映真实实现。

`fields` 是 UI 能力合约：未声明的能力不能在 UI 中显示为可用。尤其是 `work_detail_manual` 表示作品字段需要单独详情请求；作品指标随列表返回时不要设置该字段。播放量等字段只在平台确实返回且可验证时声明。没有实现自动抓取时 `auto=False`，即使已支持手动登记也不能伪装成自动可用。

CLI 退出码和 JSON 错误需遵循 `benchmark_fetch.py` 的既有语义；命中风险返回风险结果并停止本轮，不重试、不切换游客身份、不尝试其他接口绕过限制。

## 4. 接入步骤

1. **界定能力**：明确是对标数据、自有账号统计、登录、发布还是其中一项；确认登录态/匿名访问、可得字段、详情能力和依赖。不要因某平台已注册访问策略就声称所有能力已支持。
2. **定义身份**：为平台选定稳定小写 `platform` key；分清 Easel 自有凭证 `account_id` 与被读取的公开目标账号 ID。所有共享凭证的任务落入同一个 `AccessResource`。
3. **注册访问适配**：在 `platform_adapters.py` 配置展示名、官方/实际使用的 API 域名和路径、响应风险码/文本、状态文件和策略。域名匹配应限制到真实平台主机，不能使用宽泛后缀或拦截无关域名。
4. **实现业务适配**：新增能力时继承对应公共接口（对标数据使用 `benchmark_fetch.Adapter`），只在适配器内部完成平台解析和映射；保持 Web 路由与公共数据结构平台无关。准确设置 `fields` 与产品排序。
5. **接通访问控制**：将每个可能访问平台的入口（Web、CLI、后台任务、浏览器、直接 HTTP）接入共用运行时。特殊风险响应可由自定义 `PlatformAdapter` 识别，但锁、排队和状态仍复用通用实现。
6. **验证能力与界面**：通过 `platforms` 能力矩阵确认注册信息；确认状态接口按平台返回排队/熔断状态；确认前端根据 capability 字段渲染，不需要新增平台专属分支。
7. **记录兼容性**：更新 `docs/platform-access.md` 中平台保护范围、`docs/benchmarks.md` 中业务能力（如适用）和 `CHANGELOG.md`。明确未实现的流程及网络/登录限制。

## 5. 测试要求

新增平台至少覆盖以下离线用例；测试需 mock 浏览器、HTTP、子进程和时间，不使用真实账号或真实平台请求：

- 目标 ID / 主页 URL 的有效、无效、规范化解析。
- 原始响应到 `Snapshot` / `Work` 的字段映射，包括缺失值、非公开数据、时间单位、计数格式和 `coverage`。
- `fields` 与实际支持的详情/播放/收藏/评论能力一致；不支持操作返回 `Unsupported`。
- 风险响应码、平台错误码与文本会开启正确资源熔断；普通业务错误不会误触发熔断。
- 不同平台和不同凭证资源互相隔离；同凭证 Web/CLI 共享并发锁与状态，队列取消不会遗留槽位。
- 子进程继承 Web 资源身份，不会重复抢占父任务锁；请求节奏和预算通过共享运行时生效。
- CLI 单行 JSON、错误/退出码契约和状态接口可被现有调用方解析。

建议先跑针对性测试，再跑相关业务回归和前端构建，例如：

```bash
.venv/bin/pytest -q tests/test_platform_access.py tests/test_benchmarks.py tests/test_benchmark_monitor.py
cd web/frontend && npm run build
```

若平台实现新增独立测试文件，也将其加入针对性命令。报告验证结果时区分本次新增失败和仓库已有环境/时序失败。

## 6. 合并前检查清单

- [ ] 新平台代码通过公共结构和接口接入，Web/前端未出现平台协议分支。
- [ ] 访问队列和熔断以真实登录态资源隔离，所有入口使用同一资源身份。
- [ ] 能力元数据与实际支持相符，未知字段为 `None`，未支持功能明确标记。
- [ ] 所有请求入口均受共享访问控制保护；风控后立即停止且不重试。
- [ ] 测试完全离线且覆盖输入解析、数据映射、风险信号、隔离和队列。
- [ ] 文档与 changelog 已更新，安全敏感凭证未进入代码、日志或测试夹具。
