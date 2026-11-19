## 正式部署

Nexus 与 Workbench 独立构建、独立部署。Nexus 提供 HTTP API 与 WebSocket；正式发布只产出腾讯云使用的 Linux AMD64 Docker 镜像，macOS 如需本地运行可从源码构建 wheel + launchd；Workbench 在自己的仓库构建静态文件并部署到静态站点或 CDN。Nexus wheel 和容器不包含、不构建、不托管 Workbench。旧原生安装器与在线自更新链路已移除，Scout 继续独立安装。

GitHub tag 触发 CNB 构建 `linux/amd64` 镜像并推送到腾讯云 TCR；腾讯云只拉取“版本号-full commit SHA”不可变标签，不使用 `latest`。后续 tag 不再生成 wheel、sdist、ARM64 镜像或离线镜像包。安装、Compose、升级、回滚及本地 wheel 构建步骤见 [部署手册](docs/github-updates.md)。

```sh
pipx install ./mino_nexus-X.Y.Z-py3-none-any.whl
mino-nexus service install
mino-nexus service status
```

Workbench 构建时通过 `VITE_NEXUS_URL` 指向 Nexus；跨域部署时在 Nexus 设置明确的 `MINO_WORKBENCH_ORIGINS`。升级前先停写并备份，旧安装复制到新目录验收后保留原目录。

# MinoNexus

**服务端中枢。** 持有数据、准入、会话与协议，把用例脚本下发给 [MinoScout](../MinoScout) 执行。唯一前端是 [MinoWorkbench](../MinoWorkbench)，在自己的发布链路构建并独立部署，不进入 Nexus wheel。

MinoNexus 是 [MiniOrangeServer](../MiniOrangeServer) 拆分出的服务端那一半，与 MinoScout（设备执行节点）配对使用。

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 唯一服务端：HTTP `:50104` + WS 推送（UI `/ws`、Scout `/node`） | **不碰设备** —— 没有 adb / WDA / Playwright 连接 |
| 数据归属方：项目、用例、设备、账号、任务、会话事件、能力目录 | 不实现设备动作 —— 那是 Scout 的事 |
| 开跑准入、下发脚本仓库地址、保存回调结果 | 不读、不打包脚本仓库；不做逐步决策 |
| 模型配置的唯一真源（按执行账号隔离） | 不含图像算法 —— CLIP / OCR / 组件检测已废弃 |

**一句话边界：Nexus 决定「能不能跑、跑哪条、跑在哪台设备上」，然后把仓库地址交给 Scout。**

```
浏览器 ──HTTP/WS──► MinoWorkbench ──HTTP/WS──► MinoNexus ──WS /node──► MinoScout ──► 设备
                         独立静态部署              准入·会话·协议·DB        拉仓库·跑脚本
```

## 快速开始

```bash
uv sync                  # 或 pip install -e .
mino-nexus --dev         # localhost:50101，隔离开发数据
```

文档库上传 PDF 依赖 `pypdf`（已写在 `pyproject.toml`）。若报「PDF 解析需要 pypdf」，在当前环境重新 `pip install -e .` 后重启。

## 文档

| 文档 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | **硬约束**（唯一真源）、目录与分层、改动前必读 |
| [系统架构](docs/架构/系统架构.md) | 三端分工、18 个包的分层与依赖方向 |
| [执行链](docs/架构/执行链.md) | `case_run` → `script.run` → Scout → 回调 → `session_events` |
| [脚本仓库](docs/架构/脚本仓库.md) | `config.yaml` · `tests/` · `scripts/` · `pages/<APP_KEY>/` |
| [协议](docs/接口/协议.md) | 五条消息的字段级定义（与 Scout 同一份，逐字相同） |
| [HTTP](docs/接口/HTTP.md) | Workbench 与外部 agent 用的接口 |
| [能力目录](docs/架构/能力目录.md) | `kind` 取值、实现路径、启动时注册 |
| [节点与设备](docs/架构/节点与设备.md) | `node_id` / `sn` / 归属与派单 |
| [数据模型](docs/架构/数据模型.md) | 88 张表按域分组 |
| [不要恢复的东西](docs/架构/不要恢复.md) | **已删除且不要加回来**的清单 |
| [排查](docs/排查/README.md) | 跑批失败、登录、定位、节点问题 |
| [开发手册](docs/开发/开发手册.md) | 本地调试纪律与数据目录 |
| [部署手册](docs/github-updates.md) | 安装、升级、回滚 |

## 守门

```bash
python3 tools/arch_report.py --check     # 分层违规 / 包级环（CI 中执行）
```

硬约束见 [AGENTS.md](AGENTS.md) §1：不碰设备、不含图像算法、不 import Scout、协议两仓一致、不写单测。
