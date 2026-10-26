# Linux 部署与修复记录

记录日期：2026-10-26。项目来源为 `alexcode2024/Easel`，本次部署基于
`main` 分支的 `6a3b84b`（Easel 0.3.0）。本文不包含真实模型密钥、Cookie、
账号资料、私有网络地址或运行日志。

## 部署范围

| 组件 | 本次配置 |
| --- | --- |
| Python | uv 管理的 Python 3.12.15，项目内 `.venv` |
| Node.js | 24.22.1，独立安装，不替换系统 Node.js |
| OpenClaw | 2026.10.7，使用独立的 `easel` profile |
| FFmpeg / FFprobe | 7.0.2 静态发行版，独立工具目录 |
| Web | 前端生产构建，由 FastAPI 提供，端口 7860 |
| Gateway | 仅监听回环地址，profile 端口 37289 |
| 浏览器 | Playwright Chromium，已通过实际启动检查 |
| 技能 | 114 个内置技能已同步到 agent 工作区 |

安装过程保留系统 Python、Node.js 和其他应用的配置。运行依赖通过项目虚拟环境
安装，前端使用 `npm ci` 和 `npm run build` 构建。浏览器通过
`python -m playwright install chromium` 安装。

### 本机辅助文件

部署辅助文件位于 Git 忽略的 `.deploy/`，它们包含本机路径和局域网配置，不上传。
其职责与设置记录如下，便于迁移时重建：

| 文件 | 职责 |
| --- | --- |
| `env.sh` | 设置项目根目录、虚拟环境和独立工具的 PATH、项目级 npm prefix、UTF-8 与端口 |
| `easel` | 加载环境、进入项目根目录，再运行虚拟环境中的 Easel CLI |
| `gateway` | 加载同一环境，再调用上游 Gateway 管理脚本 |
| `easel-web.service` | systemd 用户服务，运行 Web，异常退出后 5 秒重启 |
| `easel-gateway.service` | systemd 用户服务，使用上游脚本启动和停止 Gateway |

环境中设置 `EASEL_PORT=7860`、`PYTHONUTF8=1` 和
`EASEL_GATEWAY_SYSTEMD=1`。局域网访问仅允许明确配置的
`EASEL_EXTRA_HOSTS` 和 `EASEL_EXTRA_ORIGINS`，不使用通配符。

Web unit 使用 `Type=simple`、`Restart=on-failure` 和 `RestartSec=5`，
并通过 `Wants` / `After` 依赖 `easel-gateway.service`。
Gateway unit 使用 `Type=oneshot`、`RemainAfterExit=yes`，启动与停止分别调用
管理脚本的 `start`、`stop`；启动超时 180 秒，停止超时 30 秒。
两个 unit 均使用 `UMask=0077`、项目根目录作为工作目录，
并安装到用户的 `default.target`。本机用户已启用 linger，无需保持终端登录。

Gateway 的 oneshot unit 负责开机启动和显式停止，不代表进程崩溃后会自动恢复；
可用下面的重启命令恢复服务。Web 的异常退出则由 systemd 自动重启。

### 日常操作

```bash
systemctl --user status easel-web easel-gateway
systemctl --user restart easel-web
systemctl --user restart easel-gateway
journalctl --user -u easel-web -n 100 --no-pager
```

在已加载项目独立工具 PATH 的终端执行：

```bash
easel doctor
easel ping
```

本服务没有登录认证，不应直接暴露到公网或不可信网络。备份时保护模型配置、
账号会话和产物的访问权限，不把它们作为部署模板上传。

## 修复一：模型配置已保存但不可用

### 原因

1. 自定义供应商的 API key 保存在 OpenClaw 配置中，原认证检查仅认可
   `.env`，导致页面误报未配置。
2. 保存供应商与主模型后，没有重启常驻网关。实际请求仍走网关加载的旧默认模型，
   报出旧供应商没有可用认证的错误。
3. systemd 托管时不能在 Web 进程下另起替代网关，否则后续 Web 重启可能连带
   结束该网关，破坏原有服务管理关系。

### 改动

- `easel/commands/doctor.py`：识别主模型对应 provider 中的非空、非占位 API key；
  配置损坏时仍检查环境文件，不回显密钥。
- `web/frontend/src/lib/api.ts`：封装固定、无参数的 Gateway 重启接口。
- `web/frontend/src/components/SettingsPanel.tsx`：聊天配置保存成功后单独重启网关；
  重启失败明确提示“配置已保存”，不会重新提交配置。直连和媒体配置不重启网关。
- `web/app.py`：Linux 且设置 `EASEL_GATEWAY_SYSTEMD=1` 时，通过固定的 systemd
  用户服务重启，不接受外部输入构造 unit 名称或 shell 命令。

实际验收：模型接口、Gateway 和 Web SSE 对话均返回 `PONG`；
配置检测返回 `modelConfigured=true`，不再自动弹出“未配置模型”提示。

## 修复二：小红书 Cookie 校验缺少依赖

原运行依赖清单未声明小红书 HTTP 登录与抓取需要的依赖，Cookie 校验可能报告
`No module named 'curl_cffi'`，补一个包后还可能继续缺少签名库。

在 `pyproject.toml` 统一补充：

| 依赖 | 用途 |
| --- | --- |
| `curl-cffi>=0.11,<1` | 模拟浏览器 HTTP / TLS 指纹 |
| `xhshow>=0.2,<1` | 生成小红书接口签名 |
| `lxml>=5,<7` | 提取页面内的状态脚本 |
| `PyYAML>=6,<7` | 解析页面状态中的兼容语法 |

安装这些依赖后，对已保存 Cookie 执行强制身份校验，返回
`loggedIn=true`、主站会话有效。此验收仅验证身份，不执行发布、评论或其他账号操作，
也不在记录中保存 Cookie 或账户身份。

## 回归测试与验收

新增测试覆盖：自定义主供应商认证、空值/占位 key、缺失主供应商、损坏配置回退、
受托管网关重启、模型保存后的重启顺序、直连/媒体不重启、重启失败不重复保存，
以及模拟小红书签名请求后的 Cookie 身份识别。

问题修复时的分项验证：模型相关 Python 测试 146 个通过，小红书及账号登录相关
Python 测试 163 个通过，前端 Node 测试 74 个通过。真实凭证仅用于上述人工验收；
回归测试使用临时配置和模拟 HTTP 响应，不调用真实模型或平台。

提交前在隔离的 OpenClaw 配置目录执行完整检查，避免读取本机账号配置：

```bash
test_state_dir=$(mktemp -d)
EASEL_OPENCLAW_STATE_DIR="$test_state_dir" python -m pytest -q tests/
node --test tests/frontend/*.test.mjs
python scripts/validate_skills.py
python scripts/validate_skill_commands.py
python scripts/validate_document_paths.py
```

前端在其目录执行 `npm run build` 和 `npm run lint`。
构建存在既有的较大 bundle 提示，lint 存在既有的正则转义警告；本次不修改无关代码。

### 本次提交前检查结果

- 完整 Python 测试：1342 个通过、6 个跳过、19 个失败。
- 已将这 19 个失败用例放到原始 `6a3b84b` 源码快照，在相同环境下逐一复跑，
  19 个均同样失败，确认不是本次改动引入。失败涉及本地 agent 默认引擎假设、
  npm prefix 环境影响以及部分旧模型设置测试的直连模式假设；本次不扩展修复范围。
- 本次修改覆盖的模型与账号登录测试合并复跑，309 个全部通过；前端 74 个测试全部通过。
- 技能校验：114 个技能通过；文档命令校验：310 条 Python 命令通过；
  仓库文档路径校验通过。
- 前端生产构建和 lint 均以状态码 0 完成，保留上述既有警告。

## 提交边界

提交业务修复、运行依赖清单、回归测试、本记录及 Changelog。
不提交 `.env`、任何真实 Cookie、OpenClaw 私有配置、账号画像、运行产物、
截图、日志、浏览器缓存、虚拟环境、下载归档或本机专用启动文件。
