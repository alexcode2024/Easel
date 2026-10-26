# Easel 本机部署记录

> Linux 部署、模型配置与小红书 Cookie 登录修复见 [Linux 部署与修复记录](docs/linux-deployment.md)。以下保留原 Windows 部署记录。

部署目标：`https://github.com/ZJU-REAL/Easel`（main 分支，v0.3.0）
部署位置：`C:\Projects\Easel`
部署主机：Windows 11（简体中文，OEM 代码页 936）

## 运行状态

| 组件 | 地址 / 位置 | 状态 |
| --- | --- | --- |
| Web 工作台 | http://localhost:7860 | 运行中 |
| OpenClaw Gateway（profile `easel`） | http://127.0.0.1:37289 | 运行中 |
| 对话模式 | OpenClaw（完整工具 / 技能 / 发布能力） | 已启用 |
| 模型 | `openai/deepseek-flash` @ `https://api.deepseek.com/v1` | 连通 |
| 内置技能 | 114 个 | 已同步 |

自检结果：`easel doctor` → 环境就绪；`easel ping` → 全部通过。

## 目录与关键文件

- `.venv/` — Python 3.13.2 虚拟环境（Easel 0.3.0 及全部运行依赖）
- `.env` — 模型配置（已在 `.gitignore` 中，不会入库）
- `web/frontend/dist/` — 已构建的 React 工作台产物
- `~/.openclaw-easel/openclaw.json` — OpenClaw profile 配置（provider / 主模型 / gateway）
- `~/.openclaw-easel/workspace/` — agent 工作区（`outputs/`、`profiles/` 以 Junction 指向项目目录）
- `.deploy/` — 本次部署使用的辅助脚本（非上游内容）
- `easel.ps1` — 本机启动器，见下节

## 日常使用

推荐用附带的启动器启动，它会顺带修好本机 `COMSPEC` 缺陷并设置 UTF-8（原因见下节）：

```powershell
cd C:\Projects\Easel
.\easel.ps1 web                    # 启动 Web 工作台，默认 7860
.\easel.ps1 doctor                 # 环境自检
.\easel.ps1 ping                   # 端到端连通性测试
.\easel.ps1 gateway status         # 查看 gateway
.\easel.ps1 gateway restart        # 重启 gateway
```

浏览器打开 http://localhost:7860 即可使用。

### 上游原生命令（等价，但需自行处理下节的两个环境变量）

```powershell
.\.venv\Scripts\easel.exe web            # 启动 Web
.\.venv\Scripts\easel.exe gateway start  # 启动 gateway
powershell -File scripts\gateway.ps1 status
```

注意：gateway 首次启动需要约 **38 秒** 才会 `/healthz` 就绪，而 `scripts/gateway.ps1 start`
只轮询 20 秒，因此它可能报「启动失败」但 gateway 随后其实起来了 —— 用
`gateway status` 或 `curl http://127.0.0.1:37289/healthz` 复核。

## 本机环境缺陷（与 Easel 无关，但会影响它）

### 1. 机器级 `COMSPEC` 被写坏 —— 需要管理员修复

```
COMSPEC = C:\WINDOWS\system32\cmd.exe;<某 ffmpeg 目录>\bin     ← 当前值（错误）
COMSPEC = C:\WINDOWS\system32\cmd.exe                          ← 正确值
```

Windows 用这个变量原样定位命令解释器，多出来的 `;路径` 会让所有经 `cmd.exe` 派生子进程的
程序失败，报 `ENOENT ... errno -4058`。实测影响：**任何 `npm` 脚本**（`npm run build`、
`npm install -g` 的 install script 全部失败）、部分 Node CLI。

这是本次部署**之前就存在**的问题，不是 Easel 或本次安装引入的；机器级写入需要管理员权限，
本次会话无法修改，因此 `easel.ps1` 在每个进程树内临时修正它。

彻底修复（需以管理员身份打开 PowerShell 或「系统属性 → 环境变量」）：

```powershell
[Environment]::SetEnvironmentVariable('COMSPEC','C:\WINDOWS\system32\cmd.exe','Machine')
```

修复后 `easel.ps1` 里的相关处理即成为冗余（无害）。

### 2. 中文 Windows 的代码页 936 会让 `easel web` 直接崩溃

`web/app.py` 启动横幅含 `✦`，Python 在代码页 936 下按 GBK 编码 stdout，抛
`UnicodeEncodeError: 'gbk' codec can't encode character '\u2726'` 并退出。
上游 `setup.ps1` 自己设了 `PYTHONUTF8=1` 规避，但 `easel web` 从普通终端启动时不会带上。
`easel.ps1` 已设置该变量；若直接调用 `.venv\Scripts\easel.exe web` 遇到此错，先执行
`$env:PYTHONUTF8='1'`。

## 本次部署完成的工作

1. 下载并解压上游 main 分支（源码归档，本机原先没有 git）
2. 安装前置依赖：Git 2.55、FFmpeg 9.0.2（Node 24.19 已存在）
3. 创建 `.venv` 并安装 `pyproject.toml` 全部运行依赖（含 `faster-whisper`、`playwright`、
   `opencv`、`rembg`、`biliup`、`librosa` 等）
4. 构建 React 工作台（`web/frontend/dist`）
5. 安装 Playwright Chromium（浏览器登录 / 发布用）
6. `npm install -g openclaw@latest` → OpenClaw 2026.10.3
7. 配置 `.env` 走 DeepSeek 的 OpenAI 兼容端点
8. 运行上游 `setup.ps1 -OpenClaw -NonInteractive`：初始化 profile、同步技能、启动 gateway
9. 核对 `doctor` / `ping` / Web HTTP 全绿

### 关于 `OPENAI_*` 变量名

`setup.ps1` 第 7 步使用 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 写入 OpenClaw 的
`openai` provider。该 provider 就是通用 OpenAI 兼容通道，与厂商是否为 OpenAI 无关，
因此 DeepSeek 直接复用这三个键，无需改脚本：

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-flash
```

`deepseek-flash` 已实测支持 function calling（`finish_reason=tool_calls`），满足 OpenClaw
工具运行时要求。`ANTHROPIC_API_KEY` 保持不可用的占位值，以免 `setup.ps1` 另写一个残缺的
anthropic provider 并抢占主模型。

## Web 搜索 provider（Tavily）

竞品分析等发现层技能依赖 `web_search`。原先没有配置 provider，小红书被风控拦截后降级链断裂
（`skill-competitor-analysis/references/data-collection.md` 指定的兜底正是
`web_search site:xiaohongshu.com`）。现已接入 Tavily：

| 项 | 值 |
| --- | --- |
| 插件 | `@openclaw/tavily-plugin` 2026.10.3（`openclaw plugins install` 安装） |
| provider | `tools.web.search.provider = tavily`（写在 `~/.openclaw-easel/openclaw.json`） |
| 凭证 | `TAVILY_API_KEY` 写在项目 `.env`（`.env` 已 gitignore） |
| 附加工具 | `tavily_search`（可按 depth / topic / 时间范围过滤）、`tavily_extract`（可抓 JS 渲染页） |

### ⚠️ key 必须由启动器注入，否则静默失效

Tavily 的 key 解析顺序是「配置里的 `apiKey`」→「**环境变量 `TAVILY_API_KEY`**」。而 Easel
本身**不会**把 `.env` 导出给子进程 —— `web/app.py` 的 `_read_env()` 只用于设置界面回显和日志
脱敏，`setup.ps1` 也只把选定的几个 key 写进 `openclaw.json`。

gateway 的环境取决于「谁启动了它」：

- `easel.ps1 gateway start` → 启动器已解析 `.env` 并注入本进程，✅ 正常
- Web 的 `/api/gateway/restart` → 走 `_proxy_env()`（`os.environ.copy()`），即**继承 Web 进程
  的环境** —— 所以 Web 也必须由启动器启动

因此 `easel.ps1` 增加了 `.env` 注入逻辑（不覆盖调用方已显式设置的值）。已实测：
从 Web 触发重启 gateway 后 `web_search` 仍然可用。

**症状识别**：`web_search` 返回

```
provider_error: Search failed. Check this search provider's configuration and connection, or choose another provider.
```

看到这条基本就是 key 没进环境，不必怀疑网络。**结论：一律用 `easel.ps1` 启动 Web 和 gateway。**

不想依赖环境变量时，也可把 key 直接写进 `~/.openclaw-easel/openclaw.json` 的
`plugins.entries.tavily.config.webSearch.apiKey`（代价是明文落在用户目录）。

### 实测效果

- 通用查询返回真实结果 ✅
- `site:xiaohongshu.com <关键词>` 能返回小红书站内结果，降级路径可用 ✅
- 但**相关度取决于索引质量**：实测搜「大厂升升」返回的多为不相关笔记，该账号在
  Tavily 索引里覆盖很弱。所以 `web_search` 定位是**兜底**而非替代 ——
  能直取主页 SSR 数据时仍应优先直取。

## 小红书采集：`skills/shared/scripts/xhs_fetch.py`

2026-10-16 新增。起因：竞品分析报告标注「未登录态采集」，但用户已完成登录。
排查后确认了两个独立问题，脚本按此设计。

### 实测结论（同一账号同一时刻）

| 访问方式 | 结果 |
| --- | --- |
| `ctx.request.get()` + 登录态持久化 profile | ✅ 200 / 185 KB / 昵称 + 12 篇作品 |
| `ctx.request.get()` 匿名 | ❌ 10 KB 空壳 / 0 篇 |
| 浏览器 `page.goto()`（**即使带登录态**） | ❌ 跳 `/login` 或 `/website-login/error` |

**必须用带登录态的 request 请求；页面导航一定被拦。** 会话来自 Web 登录流程写入的
`~/.easel-browser-profiles/XiaohongshuProfile`（cookie 库实测 `web_session` 等均 LIVE）。

### 决策：接受 12 篇，不做翻页（2026-10-16，用户决定）

翻页需要签名头 `x-s` / `x-t`。仓库内 `skills/builtin/skill-xhs-analyzer/src/lib/signing.ts`
有 585 行纯 TypeScript 的 x3 签名实现（只手写哈希 + Node crypto，无 wasm / 无浏览器依赖），
**技术上可解**（Node 子进程调用或移植到 Python）。

**用户明确选择不做**，理由是接受 12 篇的覆盖面。因此：

- 12 篇是**刻意接受的边界，不是待修缺陷**。不要再为此提议启用翻页。
- 报告必须写清实际覆盖数（如「SSR 首页 12 篇」），**不得**把 `--limit 50` 说成分析了 50 篇。
- 若日后要翻页，从此处接手即可；那条路已验证可行但未实施。

### 用法

```powershell
.\.venv\Scripts\python.exe skills\shared\scripts\xhs_fetch.py `
  --user-id 55753cf4f5a2633511d5a652 --limit 50 `
  --json outputs\_scratch\xhs_<账号>.json
```

- 顺序：**登录态优先 → 失败回退匿名**（可 `--no-fallback` 关闭），符合用户要求
- 节流：串行、一次一篇、随机 jitter。**默认 10–30s（Steady 档 = 20s ± 50%）**，
  即 `research-loop.md` 的安全默认；`--min-delay/--max-delay` 可调，
  `--max-delay` 低于 5s 时会打印 Fast 档风险警告（stderr，不改变退出码）
- 熔断：命中 `300012` / `NeedVerify` / 验证码 / 跳登录即停，**绝不重试**，exit code 2
- 实测：默认档一次抓取约 33s（2 个请求 + 一次抖动）

**⚠️ 翻页不可用 —— `--limit 50` 是上限而非保证。** 实测只能拿到 SSR 内嵌的第一页
**12 篇**。更深翻页走 `/api/sns/web/v1/user_posted`，该接口要求签名头 `x-s` / `x-t`，
**Easel 没有该实现，一律 HTTP 406**（已试 xsec_token 参数、`X-S-Common` 头、匿名搜索取
新鲜 token、feed 入口取 token，全部失败；`noteDetailMap` 与 `noteCard.noteId` 均为空）。

已知未采用的解法：`skills/builtin/skill-xhs-analyzer/src/lib/signing.ts` 有 585 行**纯
TypeScript** 的 x3 签名实现（只手写哈希 + Node crypto，无 wasm / 无浏览器依赖），理论上
可通过 Node 子进程或移植到 Python 补齐翻页。属独立工作项。

### 速率档位（安全约束）

`skill-xhs-analyzer/references/research-loop.md` 是仓库自带的风控安全文档，其默认档为
**Steady = 20 s ± 50%（即 10–30 s）**，并把 **<5 s 归为需用户显式 opt-in 的 Fast 档**。

**本脚本默认即 Steady 档（10–30 s）** —— 按该文档的安全默认执行。曾一度设为 3–10 s
（用户最初要求），后经确认改为 Steady。要提速须显式传
`--min-delay/--max-delay`，且 `--max-delay < 5` 会打印 Fast 档风险警告。

文档特别指出**匀速低方差本身就是机器签名**（人的间隔高方差、重尾），因此 jitter 不可省；
读操作触发风控会令账号降级数小时，熔断器必须硬停而非重试。

## 已知限制

- **Playwright CDN 在本机不可达**：`storage.googleapis.com` / `cdn.playwright.dev` 直连失败，
  安装时该步骤被上游脚本降级为警告（不影响其余部分）。若后续要启用浏览器登录 / 发布：

  ```powershell
  $env:PLAYWRIGHT_DOWNLOAD_HOST='https://cdn.npmmirror.com/binaries/playwright'
  .\.venv\Scripts\python.exe -m playwright install chromium
  ```

- gateway 的远程模型目录刷新被本机 DNS 拦截（`catalog.openclaw.ai` 解析到内网地址），
  日志中会出现 `blocked URL fetch (remote-model-catalog)`；不影响已配置的本地 provider。
- 图片 / 视频 / 音乐 / TTS 等媒体能力需要各自的 provider key（见 `.env.example` 后半段），
  当前未配置。
- 账号登录态与发布涉及个人凭证，需在 Web 界面自行完成。
