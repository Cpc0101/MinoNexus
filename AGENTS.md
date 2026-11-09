# AGENTS.md — MinoNexus

给在本仓工作的任何人和 agent。**写代码前先读这里。** 这里没有版本号、没有历史方案，只有现状约束。

> 全部文档只有 `docs/` 下这几个目录：`架构/`（现状）、`接口/`、`排查/`、`开发/`、`专题/`。历史方案已删除，需要时查 git 历史。

---

## 0. 本仓库是什么

Mino 的服务端中枢：持有数据、准入、会话与协议，把用例脚本下发给 [MinoScout](../MinoScout) 执行。唯一前端是 [MinoWorkbench](../MinoWorkbench)，编译进 wheel 内置，同源 `:50104` 访问。

上游 MiniOrangeServer **已停止维护、只读、禁止改动**，仅作历史参考。

---

## 1. 硬约束

| # | 约束 | 为什么 | 守门 |
|---|---|---|---|
| 1 | **不碰设备** —— 不 import `adbutils` / `uiautomator2` / `playwright` / `facebook-wda` / `appium` / `pywinauto`，不 `subprocess` 调 `adb` | 设备操作全部经协议委托给 Scout。直连设备就没法上云、没法多节点 | code review + 依赖清单 |
| 2 | **不含图像算法依赖** —— 不 import `torch` / `open_clip` / `paddleocr` / `ultralytics` / `cv2` | 拆分时已确认批次路径零调用，引回来等于把 45% 旧代码拖回来 | `pyproject.toml` 依赖审查 |
| 3 | **不 import Scout** | 依赖必须单向 | `tools/arch_report.py --check` |
| 4 | **协议两仓一致** | `docs/接口/协议.md` 两份必须逐字相同，两份 `protocol.py` 必须能往返同一份载荷 | 见 §4 |
| 5 | **不写单元测试** —— 禁止新增 / 修改 `tests/test_*.py`，禁止为「验证修复」跑 pytest | 跑批 / 登录问题以 `session_events` + `session_meta` 为准；缺字段就补 session 写入 | 见 [排查](docs/排查/Session日志与数据库.md) |
| 6 | **不碰业务库** —— 调试时禁止对 `mino.db` 增删改 | 本机库常等同团队测试真源，不是沙箱 | — |

约束 2 的唯一例外是 `pillow`：缩略图压缩 `make_thumb` 需要它，且只做重采样，不做识别。

---

## 2. 三条铁律

### 2.1 执行在 Scout，Nexus 只派发与回调

`case_run` 准入后下发 `script.run`（`repo_url` / `branch` / `revision` / `script_path` / `sn` / `platform`），Scout 自己拉仓库、读 `config.yaml` 与 `pages/`、执行 `run(case)`，账号 / 步骤 / 失败 / 证据通过 `script.callback` 回来。

**Nexus 不读、不打包脚本仓库，不决定下一步点哪里。** 不要在 Nexus 里重新引入逐步决策循环，也不要在回调里补做决策。详见 [执行链](docs/架构/执行链.md)。

### 2.2 模型与验证码在 Scout 本机

`case.llm` / `case.vlm` 用该节点自己保存的地址、模型名和密钥直连；`case.scout.otp(account)` 按账号登录策略取码（账号自带码 → 固定码 → 本机 Gmail）。**密钥和截图不回 Nexus。**

### 2.3 设备动作与脚本进度都不落本仓

设备动作、页面定位、脚本运行都在 Scout；Nexus 只保存会话、准入、租约与回调结果。脚本步骤事件写入同一份 `session_events`，Workbench 不另建轨迹格式。

---

## 3. 目录与分层

根上只准 `app.py` / `cli.py`。18 个包，**只允许 import 层号更小的包**：

```
core/ contract/ models/   0   地基、纯契约、ORM
records/                  1   事件账本
platform/                 2   身份 / 凭证 / 节点 / 设置 / 插件
resources/                3   设备 / 账号 / 环境 / 资源池 / 文档库
catalog/                  4   能力目录
knowledge/                5   政策 / 文档检索 / 意图面
runtime/                  6   run_context / 菜单 / 会话闸门
navigation/               7   图谱 / 几何定位 / 登录链
action_space/ ai/         8   动作载荷 / 智能
casework/                 9   用例 / 评审 / QA / 工作区
assistant/               10   对话 / 通知 / 媒体 / IM
loop/                    11   case_runner / script_dispatch / script_callback / router_proxy
api/                     12   HTTP `r*.py` · WS `ws/observers.py`（UI）· `ws/node.py`（Scout）
<root>                   13   装配根，只 import api
```

`python3 tools/arch_report.py --check` 强制（**会扫函数体内 import**，只扫顶层会漏 80% 依赖）。`prompts/` 是数据目录，不是代码包。

业务状态进 sqlite。**禁止给项目 / 应用 / 设备 / 跑批 / 账号 / 设置 / 能力目录加 JSON 真源。**

---

## 4. 协议同步

两份 `docs/接口/协议.md`（本仓一份、`../MinoScout` 一份）**必须逐字相同**，两份 `protocol.py` 必须一致。

改协议在**同一个 PR 周期内**完成：

1. 改 `docs/接口/协议.md`（两仓同一份文本）
2. 两仓各自改 `protocol.py`
3. 本地 fixture 只在本地核对，**不提交**

协议**没有版本号字段**。加字段时 dumps 省略空值。

---

## 5. 改动前必读

| 你要做什么 | 先读 |
|---|---|
| 理解整体分工与分层 | [系统架构](docs/架构/系统架构.md) |
| 改开跑 / 下发 / 回调 | [执行链](docs/架构/执行链.md) |
| 改协议 | [协议](docs/接口/协议.md) + §4 |
| 加能力 / 换实现路径 | [能力目录](docs/架构/能力目录.md) |
| 改用例脚本仓库 / 页面 YAML | [脚本仓库](docs/架构/脚本仓库.md) |
| 派单 / 设备归属 / 多节点 | [节点与设备](docs/架构/节点与设备.md) |
| 动数据模型 | [数据模型](docs/架构/数据模型.md) |
| 动导航图 / 定位链 | [导航图谱](docs/专题/导航图谱.md) |
| 改 prompt | [Prompt 规范](docs/开发/Prompt规范.md)，真源是 `mino_nexus/prompts/`（启动时解析，格式错拒绝启动） |
| 打通 Workbench | [HTTP](docs/接口/HTTP.md) |
| 排查跑批 / 登录 | [Session 日志与数据库](docs/排查/Session日志与数据库.md)，**不要写 pytest** |
| 本地调试 / Agent 协作 | [开发手册](docs/开发/开发手册.md) |

---

## 6. 不要做的事

完整清单见 [不要恢复的东西](docs/架构/不要恢复.md)。最常见的：

- 不要在 Nexus 里 `subprocess adb ...`
- 不要把设备连通性当本地可查状态 —— 只来自 Scout 的 `REGISTER` / `HEARTBEAT`
- 不要重新引入逐步决策循环
- 不要把 CLIP / OCR / 组件检测搬回来
- 不要改 `../MiniOrangeServer` 的任何文件
- 不要假设至少有一个 Scout 在线 —— 无节点必须能启动并在 UI 说清楚
- 不要让 Workbench 去发现 Nexus —— origin 编译进前端，登录失败只提示「无法连接服务器」
- 不要在 `script.run` 里省字段或塞本机绝对路径 / 模型密钥
- 不要新增 `tests/test_*.py` 或跑 pytest
- 不要把 `pages/` 写回 `nav_fsm*` 或控件表

---

## 7. FastAPI 装配的两个坑

都会让服务**静默失效**。

### 7.1 请求模型 / WebSocket 注解必须在模块级

`app.py` 与 `api/ws/node.py` 有 `from __future__ import annotations`，注解是字符串，FastAPI 拿模块 globals 去 `eval`。函数体内的 `BaseModel` 子类或 `WebSocket` import 拿不到 → `NameError` → **整个 app 起不来**。

### 7.2 fastapi 版本上界是必须的

fastapi 0.141.1 起 `include_router(APIRouter(prefix=...))` 会把路由路径解析成空串，12 个 router 全部静默失效，而 `app` 照常启动、`/health` 照常 200。`pyproject.toml` 钉 `fastapi>=0.124,<0.130`。

---

## 8. 模型配置的归属

| 模块 | 角色 | 落盘 |
|---|---|---|
| `platform/settings_store.py` | **唯一真源**。UI（`rSettings`）读写，落 `settings` 表 | `mino.db` |
| `platform/settings.py` | **只按执行账号转发**，自己不存任何配置，**不回退共享或环境 Key** | 无 |

`llm_client` 与 `runtime/run_context` 只 import `mino_nexus.platform.settings`——"key 从哪来"收在一处。

**provider 字段的形状由 `settings_store.get_ai_provider_credentials` 定义，`settings.py` 不得自行拼装。**

线上用户、助手与后台任务必须携带明确的执行账号：模型配置按 `settings.id = ai-user:<uid>` 持久化，只共享不含密钥的服务商预设；后台线程创建时捕获身份，任务恢复时用固化的 owner，缺归属则拒绝模型调用。**请求体 `user_id` 不能决定归属。**

无 Key 的账号明确报未配置，**不使用**管理员、其他用户、全局或共享环境 Key。`MINO_AI_API_KEY` 等环境变量覆盖**不再参与用户执行**。独立联调用临时 `MINO_NEXUS_DATA_DIR`，通过该测试账号配置模型服务，不写真实业务数据库。

---

## 9. Workbench

改 [MinoWorkbench](../MinoWorkbench) 时遵守两条：

1. **删掉多余的标语、说明文字和次要标签。** 标题、控件、数据和状态本身要能说明界面；删完看不懂就重新设计布局，不要把说明写回去。
2. **每次修改都要同时让用户体验、开发体验、AI Agent 的使用体验变好，且不破坏现有功能。**
