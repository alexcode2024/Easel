# 平台访问控制抽象

新增平台或扩展平台能力时，先阅读 [`新平台适配规范`](platform-adapter-guide.md)，其中定义了业务数据结构映射、接口契约、接入顺序与测试清单。

## 目标与边界

平台访问控制不再依赖小红书专属队列或 Web 分支。统一运行时在 `skills/shared/scripts/platform_access.py` 中；平台差异集中在 `platform_adapters.py` 的策略适配器中。调用方只提供资源身份、任务说明和结果，不自行重复实现并发、锁、节奏、预算或风控状态管理。

这里的 `account_id` 指当前使用的登录态/凭证身份，而不是正在读取的公开目标账号。当前 Easel 每个平台配置一个默认登录态，因此业务入口使用 `default`；所有读取同一平台默认登录态的操作共享一个访问资源。后续支持多个登录态时，需由账号管理层提供稳定的凭证身份 ID，并让 Web、CLI、状态查询都使用相同 ID。

队列、暂停和请求节奏用于减少并发访问、在命中限制后停止；它们不是绕过风控的方法，也不保证平台不会限制账号。外部浏览器、别的项目、其他机器以及平台自身的阈值不受 Easel 控制。

## 通用数据结构

- `AccessResource(platform, account_id)`：资源键为 `platform:account_id`，并提供稳定摘要用于文件名隔离。
- `AccessPolicy(platform, display_name, concurrency, request_delay_min, request_delay_max, risk_cooldown, request_budget)`：描述并发上限、随机请求目标间隔、熔断冷却与单轮预算。
- `AccessResult(exit_code, payload, text)`：把 CLI 或 HTTP 包装层的执行结果交给平台适配器统一判定。
- `PlatformAdapter`：访问运行时依赖的接口，包括策略、锁/状态路径、URL 匹配、响应检查、错误结果分类与熔断管理。
- `PlatformPolicyAdapter`：面向 HTTP/浏览器平台的可复用实现；平台只需配置域名、路径、风控标记、持久化文件和策略。

## 运行流程

1. Web 任务以 `AccessResource` 进入 `AsyncResourceQueue`，同一资源 FIFO 排队，默认并发为 1；不同平台、不同账号身份有独立队列。
2. 获准任务持有跨进程文件锁。CLI 直接运行时由 `run_cli()` 获取同一把锁；Web 启动的子进程通过资源键继承父任务锁，不会再次抢锁造成自锁。
3. HTTP 请求在 `session()` 内经 `request()` 检查预算、等待共享的持久化节奏时钟并检查响应；共享节奏文件另有文件锁，避免多个进程同时等完后并发发出请求。
4. Playwright 上下文通过 `install_browser_guard()` 让适配器匹配的 API 请求经过相同检查。页面导航仍由平台采集适配器自身处理。
5. 平台适配器识别风控信号后持久化熔断状态；排队任务在下一次检查时停止，不会自动重试。熔断文件按平台/账号身份区分。
6. `GET /api/platform-access/status` 返回各已注册适配器的当前任务、排队状态、外部进程占用与熔断状态；旧 `/api/xhs/access/status` 仅保留作兼容。对标账号页每 3 秒轮询通用接口。

## 当前接入范围

| 平台 | 访问策略 | 当前受保护业务 |
|---|---|---|
| 小红书 | 并发 1、8–15 秒目标间隔、1 小时熔断；沿用已有状态与节奏文件 | 对标抓取/详情/同步/下载/监控，自有账号校验与创作数据，扫码登录和发布 |
| 抖音 | 并发 1、8–15 秒目标间隔、1 小时熔断 | 对标抓取/详情，自有账号统计/校验，扫码登录与异步发布 |
| TikTok | 并发 1、8–15 秒目标间隔、1 小时熔断 | 对标抓取/详情和相应浏览器 API 拦截；目前没有接入 TikTok 自有账号登录态/创作数据流程 |

这些间隔与冷却时间是 Easel 当前配置，不是平台官方建议，也不是“安全阈值”。每个平台需要根据真实业务表现、平台政策和反馈谨慎调整。没有实现某项平台业务时，注册访问策略不应被理解为该业务已可用。

## 新平台接入步骤

1. 在 `platform_adapters.py` 注册 `AccessPolicy` 与 `PlatformPolicyAdapter` 配置：平台键、显示名、API 域名/路径、风控识别规则、状态文件和适当策略。
2. HTTP CLI 入口使用 `platform_access.run_cli(main, platform=...)`；浏览器流程在创建上下文后调用 `install_browser_guard(context, platform=...)`。直接 HTTP 调用使用 `session()` / `request()`，不要另起锁或熔断实现。
3. Web 入口通过 `_platform_access()` 和 `_run_platform_command()`；只有访问规则不能表达的平台特殊行为才放在该平台采集/发布适配器内部。
4. 用离线 mock 覆盖资源隔离、FIFO、策略并发、父子进程锁继承、节奏锁、风控判定和状态快照；不使用真实平台账号验证。
5. 确认 `GET /api/platform-access/status` 和页面状态显示平台名、当前任务、排队、熔断恢复时间；更新适配覆盖表和 changelog。

## 验证

`tests/test_platform_access.py` 覆盖平台/账号资源隔离、队列并发、状态持久化、响应信号、子进程锁继承和节奏状态；`tests/test_xhs_access.py` 保留小红书兼容路径与历史行为回归；`tests/test_account_whoami.py` 验证自有账号路径接入队列。测试仅使用本地模拟，不访问真实平台。
