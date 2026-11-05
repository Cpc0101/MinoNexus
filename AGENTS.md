# AGENTS.md — MinoNexus


> **前端架构硬约束（2026-10-14）**：Mino 项目只有 **MinoWorkbench** 一个前端。MinoConsole 与 MinoStudio 已取消，不再开发、构建、交付或作为配置/验收入口。管理、用例、环境、控件配置和证据查看全部在 Workbench；Nexus 与 Scout 保持后端和执行节点职责。历史文档中的 Console/Studio 入口均以 Workbench 为准。“三端”仅指 Web、移动、PC 被测平台。
本文件是给 Codex / 任何在本仓库工作的人的**硬约束清单**。写代码前先读这里。

> **执行架构（2026-10-30 V9）**：`mino_nexus/loop/agent_loop.py` 已删除，逐步决策循环不再存在。开跑只下发脚本（`script_dispatch`），Scout 执行并回调（`script_callback`）；本文涉及 agent_loop / `EXECUTE` 循环的描述以本节和 [docs/架构/V9/Nexus.md](docs/架构/V9/Nexus.md) 为准。

---

## 0. 本仓库的定位

MinoNexus 是服务端中枢：持有数据、准入、会话与协议，把用例脚本下发给 [MinoScout](../MinoScout) 执行。

上游来源是 [MiniOrangeServer](../MiniOrangeServer)（**已停止维护，只读参考，禁止改动**）。搬迁映射见 [docs/MIGRATION.md](docs/基础框架/MIGRATION.md)。

---

## 1. 硬约束

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **不碰设备** —— 不 import `adbutils` / `uiautomator2` / `playwright` / `facebook-wda` / `Appium` / `pywinauto`，不 `subprocess` 调 `adb` | 设备操作全部经协议委托给 Scout。Nexus 一旦直连设备，就没法上云、没法多节点 |
| 2 | **不含图像算法依赖** —— 不 import `torch` / `open_clip` / `paddleocr` / `ultralytics` / `cv2` | 拆分时已确认批次路径运行时零调用（见 `docs/MIGRATION.md` §0）。引回来等于把 45% 的旧代码拖回来 |
| 3 | **不 import Scout** | 依赖必须单向 |
| 4 | **协议契约一致** | 两仓 `protocol.py` 必须与同步的协议文档一致；本地 fixture 可用于 round-trip 校验 |
| 5 | **测试只留本地** —— `tests/`、单元测试与 mock 文件不得提交；禁止为「验证修复」跑 pytest | 跑批/登录等问题以 **`session_events` + `session_meta`** 为准（[docs/排查手册/01-Session-Log与数据库.md](docs/排查手册/01-Session-Log与数据库.md)）；缺字段就补 session 写入，不靠测试脚本 |

> 约束 2 的例外只有 `pillow`：缩略图压缩（`make_thumb`）需要它，且只做重采样，不做识别。

---

## 2. 三条铁律

### 2.1 能力目录是唯一真源，且只在本仓

`catalog_entries` 声明"能做什么、用什么实现"，**不写"什么时候做、按什么顺序做"** —— 后者是 LLM 的事。`kind` 为 `prep` / `do` / `check` / `generic` / `recovery`，与执行阶段和 `/packs?kind=` 同一套。业务能力由 Workbench 维护。程序直接派发的系统能力由程序在启动时自动注册与升级，不要求用户在 Workbench 点升级；代码声明维护其平台、caller、phases 和版本合同，不能仅改常量而遗留现库旧平台。

Scout 不读 `catalog_entries`。能力目录仍由 Workbench 维护，但脚本执行不再现场拼 `executor_order` / `selected_impl`；页面配置由 Scout 从脚本仓库读取，Nexus 不打包。

### 2.2 执行在 Scout，Nexus 只派发与回调

V9（2026-10-30）起，Nexus 不再逐步决策，也没有 `agent_loop`。开跑由 `loop/script_dispatch.py` 下发 `script.run` 载荷（`repo_url` / `branch` / `revision` / `script_path` / `sn` / `platform` 等）；Scout 自行拉取仓库并在设备上执行脚本，Nexus 不读、不打包脚本仓库。

Scout 需要租约、账号或回执时，通过 `loop/script_callback.py` 回调 Nexus（`script.lease` / `case.account` / `case.step` / `case.note` / `case.fail`）；验证码与模型调用在 Scout 本机完成。**不要在 Nexus 里重新引入逐步决策循环，也不要在回调里补做决策。**

### 2.3 设备动作与脚本进度都不落本仓

设备动作、页面定位、脚本运行都在 Scout；Nexus 只保存会话、准入、租约与回调结果。脚本步骤事件仍写入同一份 `session_events`。

`loop/local_executors.py` 等旧本地执行器仅为兼容引用保留，不是执行入口；不要再把它们接成新的循环。

---

## 3. 目录约定

根上只准 `app.py` / `cli.py`。**`services/` 已经拆完删除**（V8 第 0–9 步，2026-10-22）——
新业务按层落位：用例与评审进 `casework/`，资源与设备进 `resources/`，
身份 / 凭证 / 集成插件进 `platform/`，对话与协作进 `assistant/`，
新表进 `models/`，基础设施进 `core/`。

```
mino_nexus/
├── app.py / cli.py       装配根（只允许 import api）
│
├── core/                 0 层：database · paths · log · protocol · schemas · client_gate · mdns
├── contract/             0 层：纯数据契约（ui_nodes · execution · case_preview …）
├── models/               0 层：ORM，一张表一个文件
├── records/              1 层：eventlog（原子写 / writer / 绑定）
├── platform/             2 层：identity · settings · settings_store · node_* · 迁移与插件
├── resources/            3 层：设备 / 账号 / 项目环境 / 资源池 / 文档库 / 设计稿 / 节点
├── catalog/              4 层：能力目录 loader / registry（读 catalog_entries）
├── knowledge/            5 层：政策 / 文档 / 意图面 / 检查口径
├── runtime/              6 层：run_context · menu · session_gate · cursor_state
├── navigation/           7 层：atlas（图谱）· geometry（几何与控件定位）· login（登录链）
├── action_space/         8 层：scheme · assemble · 坐标与参数
├── ai/                   8 层：planner · prompt_render · job_store · llm_client
├── casework/             9 层：用例 / 评审 / QA / 工作区协作 / 平台迁移
│   └── qa/               qa_cover · qa_process_assist · qa_process_jobs
├── assistant/           10 层：对话 / 通知 / 媒体 / 网关
│   ├── workspace/        协作工作区（collab_* · workspace_*）
│   └── connectors/       飞书 · 禅道 · Figma · IM（plugins_store · wechat_ilink · im_hold）
├── loop/                11 层：case_runner · script_dispatch · script_callback · router_proxy
├── api/                 12 层：HTTP 与实时通道
│   ├── r*.py             路由，沿用上游 r* 命名 + deps.py
│   ├── ws/observers.py   UI /ws
│   └── ws/node.py        Scout /node
└── prompts/              数据（启动时解析，不是代码包）
```

层号只允许向下依赖（`python3 tools/arch_report.py --check` 强制）。
命名沿用上游：router 用 `r*`，websocket handler 用 `w*`。

**业务状态进 sqlite（`mino.db`）。** 禁止再给项目 / 应用 / 设备 / 跑批 / 账号 / 设置 / 能力目录加 JSON 真源。

### 与上游 MiniOrangeServer 的结构差异（以及为什么）

| 上游有 | 本仓 | 原因 |
|---|---|---|
| `core/database.py` + `models/`（SQLAlchemy） | **同样有。** 库文件叫 `mino.db`，不是 `autobots.db` | 服务端数据必须在库里 |
| `core/database.APP_DATA_DIR` | `core/paths.py` | 上游把数据目录和 engine 塞在同一模块，任何要拼路径的模块都被迫 import ORM |
| `system_settings_service.py` | `platform/settings.py` + `platform/settings_store.py` | 见 §8 |
| `services/`（80 文件大平层） | **已删除**，按层拆进 `platform` / `resources` / `casework` / `assistant` / `knowledge` / `navigation` / `ai` | 平层里 80 个文件互相 import，改一处要通读；拆开后依赖方向可校验 |
| `runtime/run_context.py`（跑探测） | `runtime/run_context.py`（**重写**） | 连通性来自 Scout 上报，Nexus 不探测（§6） |

---

## 4. 改动前必读

| 你要做什么 | 先读 |
|---|---|
| 加一条能力 / 改能力的实现路径 | [docs/CAPABILITY_CATALOG.md](docs/基础框架/CAPABILITY_CATALOG.md)。多数情况只改 YAML，**Scout 侧零改动** |
| 改 prompt / 决策逻辑 | [docs/PROMPTS.md](docs/基础框架/PROMPTS.md) — **真源是 `mino_nexus/prompts/`，改文件后重启即可（启动时解析，格式错会拒绝启动）** |
| 改与 Scout 的通信 | [docs/PROTOCOL.md](docs/基础框架/PROTOCOL.md) + §5。**协议改动必须两仓同步** |
| 派单 / 设备归属 / 多节点 | [docs/NODE_REGISTRY.md](docs/基础框架/NODE_REGISTRY.md) |
| 动数据模型 | [docs/DATA_MODEL.md](docs/基础框架/DATA_MODEL.md) |
| 打通 Workbench | [docs/HTTP.md](docs/基础框架/HTTP.md) |
| 动导航图 / 守卫 / hierarchy | [docs/NAVIGATION_ATLAS.md](docs/基础框架/NAVIGATION_ATLAS.md)。配置在 `nav_fsm*` 表，**代码里不得硬编码被测 App 的文案或包名** |
| 动三渠道统一检索 / context-pack | [docs/9月12日_AppIntel信息基座.md](docs/9月12日_AppIntel信息基座.md)（规划真源） |
| 从上游搬代码 | [docs/MIGRATION.md](docs/基础框架/MIGRATION.md) |
| 本地调试 / Agent 协作 | [docs/开发手册.md](docs/开发手册.md) — **禁止**调试时直接对 `mino.db` 增删改 |
| 排查跑批/登录/租号问题 | [docs/排查手册/01-Session-Log与数据库.md](docs/排查手册/01-Session-Log与数据库.md) — **只查 session 轨迹**；不要写 pytest |

---

## 5. 协议同步规则

协议在两个仓库各有一份 `protocol.py`（本仓 `mino_nexus/protocol.py`，Scout `mino_scout/protocol.py`）。**没有共享包** —— 只有两个仓库，不引入第三个。

契约真源是两仓同步的 `docs/PROTOCOL.md` 和对应 `protocol.py` 实现；`tests/fixtures/protocol/*.json` 仅供本地调试，不提交 Git。

改协议的流程，**同一个 PR 周期内完成**：

1. 改 `docs/PROTOCOL.md`（两仓同一份文本）
2. 两仓各自改 `protocol.py`，使其与文档一致
3. 若本地有 fixture，两仓各自确认 round-trip 并保持本地样例一致，不提交样例

`docs/PROTOCOL.md` §8 的哈希仅供有本地 fixture 时核对；两仓协议文档或实现不一致 = 协议漂移。

---

## 6. 不要做的事

- 不要为了"快一点"在 Nexus 里直接 `subprocess adb ...` —— 那是拆分失败的第一步
- 不要把设备连通性当成本地可查的状态 —— 它只来自 Scout 的 `REGISTER` / `HEARTBEAT`
- 不要在 Nexus 里重新引入逐步决策循环；脚本执行只在 Scout（见 §2.2）
- 不要把 CLIP / OCR / 组件检测搬回来 —— 拆分时已确认批次路径零调用
- 不要改 `../MiniOrangeServer` 的任何文件
- 不要假设至少有一个 Scout 在线 —— 无节点时必须能启动并在 UI 上说清楚
- 不要让 Workbench 去发现 Nexus —— origin 编译进前端，登录失败只提示「无法连接服务器」
- 不要在 `script.run` 载荷里省字段或塞本机绝对路径 / 模型密钥 —— 路径、大小与凭据校验在 Nexus 完成
- **不要**为排查或自证修复去新增 `tests/test_*.py` 或跑 pytest（§1 约束 5）；查 `cr-…::case-…::sn` 的 `session_events`

---

## 7. FastAPI 装配的两个坑（都踩过，都能让服务静默失效）

### 7.1 请求模型 / WebSocket 注解必须在**模块级**

本仓的 `app.py` 与 `api/ws/node.py` 都有 `from __future__ import annotations`，
于是函数注解是**字符串**，FastAPI 拿模块 globals 去 `eval` 它。定义在函数内部的
`BaseModel` 子类或 `WebSocket` import 拿不到 → `NameError` → **整个 app 起不来**。

```python
# ✗ 会炸
def create_app():
    class ObserveBody(BaseModel): ...
    @app.post("/x")
    async def x(body: ObserveBody): ...

# ✓
class ObserveBody(BaseModel): ...
def create_app(): ...
```

这个坑修完被一次重写带回来过，坏了一整天。请求模型必须留在模块级。

### 7.2 fastapi 版本上界是必须的

**fastapi 0.141.1 起，`include_router(APIRouter(prefix=...))` 会把路由路径解析成空串。**
后果：12 个 router 全部静默失效（没有 `/auth/status`、没有 `/case-runner/run`），
而 `app` 照常启动、`/health` 照常 200。最小复现：

```python
r = APIRouter(prefix="/x")
@r.get("/ping")
def ping(): ...
app.include_router(r)
# 0.124.0 → "/x/ping"      0.141.1 → ""
```

所以 `pyproject.toml` 钉了 `fastapi>=0.124,<0.130`。基础路由全在不代表服务是好的，带 prefix 的 router 也必须在。

---

## 8. 两套 settings 的分工

| 模块 | 角色 | 落盘 |
|---|---|---|
| `platform/settings_store.py` | **唯一真源。** UI（`rSettings`）读写；落在 `settings` 表 | `mino.db` |
| `platform/settings.py` | **只按执行账号转发。自己不存任何配置，不回退到共享或环境 Key。** | 无 |

`llm_client` 与 `runtime/run_context` **只 import `mino_nexus.platform.settings`** —— "key 从哪来"收在一处。

### 铁律

**provider 字段的形状由 `settings_store.get_ai_provider_credentials` 定义，
`settings.py` 不得自行拼装。** 执行账号来自认证会话、已验证绑定或持久任务 owner。

踩过的坑（两层）：

1. `settings.py` 曾自己读 `ai_providers.json`，而 UI 写另一份配置 —— 在设置页填了 key，
   `llm_client` 读不到，所有 LLM 调用报"未配置 provider"。
2. 更隐蔽：`llm_client` 会检查 `provider["case_execution_use"]`（"允许用于跑用例"），
   而当时 `settings.py` **根本不返回这个 key** → `.get()` 得到 `None` → 即使读对文件也被拒。

`settings_store.get_ai_provider_credentials` 定义 provider 字段形状；`settings.py` 只做账号校验与转发。
`summary()` 不得泄明文 key。

### 执行账号与密钥隔离

线上用户、助手与后台任务必须携带明确的执行账号。模型配置按 `settings.id=ai-user:<uid>` 持久化；只共享不含密钥的服务商预设。后台线程创建时捕获身份，任务恢复时使用固化的 owner；缺归属则拒绝模型调用。请求体 `user_id` 不能决定归属。

`MINO_AI_API_KEY` 等历史环境覆盖不再参与用户执行。无 Key 的账号明确报未配置，不使用管理员、其他用户、全局或共享环境 Key。独立联调使用临时 `MINO_NEXUS_DATA_DIR`，通过该测试账号配置模型服务；不要写真实业务数据库。

---

## 9. Workbench 界面

改 [MinoWorkbench](../MinoWorkbench) 时同时遵守下面两条。细则与现有页面约束见该仓 `docs/开发手册.md`。

1. **删掉多余的标语、说明文字和次要标签。** 标题、控件、数据和状态本身要能说明界面。如果删完就看不懂，问题在布局，直接重新设计，不要把说明写回去。
2. **每次修改都要同时让用户体验、开发体验和 AI Agent 的使用体验变好。** 三边都要变好，而且不能破坏现有功能。
