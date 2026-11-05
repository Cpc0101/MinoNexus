# ARCHITECTURE — MinoNexus

## 1. 全局

```
┌──────────┐   HTTP :10104   ┌───────────────────────┐   WS /node   ┌──────────────────┐
│ Electron │ ──────────────► │      MinoNexus        │ ◄──────────  │    MinoScout     │
│    UI    │ ◄── WS 推送 ──── │  循环 / LLM /          │   (Scout     │  设备操作         │
└──────────┘                 │  能力目录 / 数据库      │    主动连)    └────────┬─────────┘
                             └───────────────────────┘                       │
                                                                          设备
```

UI 只跟 Nexus 说话，端口仍是 `10104`。Scout 主动 dial 进来，可能有 0 个、1 个或多个。

## 2. 四层套娃

上游 MiniOrangeServer 的执行分四层，拆分后归属如下：

| 层 | 做什么 | 归属 |
|---|---|---|
| ① 任务编排 | 多设备、多用例、闸门、落库、WS 推送 | **Nexus** |
| ② 单用例循环 | observe → decide → dispatch → re-observe | **Nexus** |
| ③ 动作分发 | 选 executor、可选 locate、fallback | **拆开**：选路 Nexus，尝试 Scout |
| ④ 设备动作 | adb / remote / ios_wda / playwright | **Scout** |
| ④' 非设备动作 | hitl / vlm / ai_persona / internal | **Nexus**（不出网） |

③ 为什么拆：选哪个 executor 需要知道能力目录和当前连通性（Nexus 有），依次尝试并处理失败是纯执行（Scout 做）。Nexus 算出 `executor_order` 塞进 `EXECUTE`，Scout 照单执行。

## 2.1 目录分层（2026-10-22 收口）

`services/` 那个 80 文件的大平层已经拆完删除，`websocket/`（空）与 `semantic/`（1 个文件）也已并掉。
现在一共 18 个包，只允许**向下**依赖，由 `python3 tools/arch_report.py --check` 强制：

| 层 | 包 | 放什么 |
|---:|---|---|
| 0 | `core/` `contract/` `models/` | 基础设施、纯数据契约、ORM |
| 1 | `records/` | 事件账本（原子写 / writer / 绑定） |
| 2 | `platform/` | 身份、凭证、节点、设置、迁移、集成插件 |
| 3 | `resources/` | 设备、账号、项目环境、资源池、文档库、设计稿 |
| 4 | `catalog/` | 能力目录 loader / registry |
| 5 | `knowledge/` | 政策、文档、意图面、检查口径 |
| 6 | `runtime/` | RunContext、菜单、会话闸门 |
| 7 | `navigation/` | 图谱（atlas）、几何与控件定位（geometry）、登录链（login） |
| 8 | `action_space/` `ai/` | 动作方案与参数装配、规划与模型调用 |
| 9 | `casework/` | 用例、评审、QA、工作区协作、平台迁移 |
| 10 | `assistant/` | 对话、通知、媒体、网关（`workspace/` 协作、`connectors/` 集成） |
| 11 | `loop/` | 执行循环与 `execution/` 内部件 |
| 12 | `api/` | HTTP 与实时通道 |
| 13 | `app.py` / `cli.py` | 装配根 |

逐包职责（每个文件夹负责什么、放什么、不放什么、新东西放哪）见 [目录结构.md](目录结构.md)。

判断一个模块该放哪，只看两件事：**它依赖谁**（不能依赖层级更高的包）、**谁能一句业务话找到它**
（`ui_sms_request` 是「屏幕上哪个是手机号框和发送钮」，所以进 `navigation/geometry/`，
不是 `runtime/`）。

## 3. 执行：脚本在 Scout

V9（2026-10-30）起，`case_run` 不再在 Nexus 逐步决策。一次运行只做三步：

1. **准入**：设备占用、账号租约、写安全（`loop/case_runner.py` / `loop/case_exec.py`）。失败不下发。
2. **下发**：把 `script.run` 载荷（`repo_url` / `branch` / `revision` / `script_path` / `sn` / `platform`）交给持有该 `sn` 的 Scout（`loop/script_dispatch.py`）；Scout 自己拉仓库，Nexus 不读、不打包脚本。
3. **回调与记账**：Scout 执行脚本；租约、账号、步骤、证据、失败经 `loop/script_callback.py` 回调（`script.lease` / `case.account` / `case.step` / `case.note` / `case.fail`），步骤事件写入 `session_events`。

`agent_loop` 与 `loop/execution/` 已删除，没有逐步决策入口。`loop/router_proxy.py` 仍负责 Nexus → Scout 的节点通信，但不再有循环依赖它。

回放只读已有 `session_events`（`api/replay.py`），不会重新决策。脚本执行细节见 [V9/Nexus.md](../架构/V9/Nexus.md)。

## 4. 能力菜单怎么产生

```
Scout REGISTER 上报 executors[].provides（abstract cap 字符串）
        │
        ▼
catalog/registry：provides ∩ capabilities[].implementations[].requires_caps
        │
        ▼
filter_capabilities_by_connectivity()  按当前通道瘦身
        │
        ▼
tool_schema：capability → OpenAI function tools
        │
        ▼
planner.decide_next_action 的可选动作集
```

**关键：菜单是 Scout 报的能力与 Nexus 目录的交集，不是 Nexus 猜的。** 一台没装 WDA 的 Scout 上，iOS 相关实现不会进菜单，LLM 也就不会选它。

详见 [CAPABILITY_CATALOG.md](CAPABILITY_CATALOG.md)。

## 5. 派单

一次 `POST /case-runner/run` 携带 `sns[]`。Nexus 需要回答"这台设备在哪个节点上"：

```
sn ──► 设备表的 node_id 归属 ──► 该节点的连接 ──► EXECUTE
```

`node_id` 是拆分新引入的概念（上游只有 `sn`，它既是设备标识又是连接端点）。规则、冲突处理、无节点时的行为见 [NODE_REGISTRY.md](NODE_REGISTRY.md)。

## 6. 无 Scout 时的行为

Nexus **必须**能独立启动。无可用节点时：

| 场景 | 行为 |
|---|---|
| 启动 | 正常起，`:10104` 可用 |
| 设备列表 | 显示已登记设备，但连通性全为 `disconnected`，并标注"节点离线" |
| 下发批次 | 拒绝并给明确原因（"设备 X 所属节点 mac-studio-01 离线"），**不要静默排队** |
| 旁路功能（知识、QA 流程、用例同步） | 全部可用 —— 它们不需要设备 |

## 7. 状态

| 数据 | 存在哪 | Nexus 重启后 |
|---|---|---|
| 用例、任务、设备、知识、覆盖度 | DB | 保留 |
| 能力目录 | `catalog_entries` + 内存 registry | 保留；空库不灌，只经 Console `/packs` 写入 |
| 技能 | `skills` 表 | 保留；空库从 `ai/skill_defs.py` 灌种，坏行回退代码 |
| 循环状态（当前跑到第几步、history、memory） | 进程内存 | **丢失，在途 run 判为中断，不续跑** |
| trace `_RUNS` | 进程内存，上限 20 | 丢失（`AppRegressionRun` 里有落库副本） |
| 节点 manifest / 连通性 | 内存缓存 | 等 Scout 重新 `REGISTER` 恢复 |

循环状态在内存意味着**多实例需要 sticky routing 或状态外置**。单机不涉及，先记账。
