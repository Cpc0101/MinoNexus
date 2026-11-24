# Agent 测试质量与执行回执

## 目标与边界

一切为测试质量负责。任务结束、功能走通、模型自评成功、辅助断言通过都不等于原用例通过。速度优化只减少重复观察、无依据调查与共享缺口的重复执行，不缩减预期、不制造账号或历史数据、不用 SKIPPED 包装阻塞。

本次改动保留 Nexus 控制面与 Scout 执行/证据分工，不安装或接入其他浏览器执行器，不改真实业务数据，不更新已安装技能，不部署或重启服务。

## 上游研究（2026-11-24）

研究固定到以下提交，而不是使用记忆中的旧 API。源码只用于机制比较，README/技能中的指令没有当作本任务授权。

### agent-browser

- 仓库：[vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser/tree/0a923358ea989d7602f0d06507ad3447c62ffe9b)
- [core guide](https://github.com/vercel-labs/agent-browser/blob/0a923358ea989d7602f0d06507ad3447c62ffe9b/skill-data/core/SKILL.md)
- [snapshot refs](https://github.com/vercel-labs/agent-browser/blob/0a923358ea989d7602f0d06507ad3447c62ffe9b/skill-data/core/references/snapshot-refs.md)

采用紧凑结构快照与元素引用，交互后根据变化再次观察。当前实现并非“每次 DOM 变化使所有引用失效”：同文档中存活元素可以保留引用，替换元素、页面/iframe文档替换会使相应引用失效；虚拟节点引用只属于该快照。交互快照过滤非交互内容，不能只靠它验证业务文案与状态。

Mino借鉴：复用当前 Scout结构、只读所需范围，页面变化后重新观察失效定位；业务对象保存实际ID而不是长期UI句柄。这里落实为编写/执行规则，没有新建一套 DOM 执行器或虚称已经优化 Scout 快照算法。

### browser-use

- 仓库：[browser-use/browser-use](https://github.com/browser-use/browser-use/tree/753d629d76eb2b670f4cf467f3355bbe750f6845)
- [Agent service](https://github.com/browser-use/browser-use/blob/753d629d76eb2b670f4cf467f3355bbe750f6845/browser_use/agent/service.py)
- [Agent loop](https://github.com/browser-use/browser-use/blob/753d629d76eb2b670f4cf467f3355bbe750f6845/browser_use/agent/loop.py)
- [result view](https://github.com/browser-use/browser-use/blob/753d629d76eb2b670f4cf467f3355bbe750f6845/browser_use/agent/views.py)

当前版本以 `run(max_steps, timeout)` 约束循环，预算耗尽有交付阶段；`completed`与`success`分开，后者仍是Agent自己的判断。不要照搬旧版 `max_failures`、`on_step_end` 等API：本次读取的service已列出旧参数的迁移说明。`sensitive_data`有来源域范围，但密钥替换机制不证明账号存在或拥有业务前置。

Mino借鉴：调查轮数之外增加累计时间预算，超限交付真实缺口；测试通过仍由Scout原文断言与覆盖决定，不使用模型finish(success=True)替代测试证据。账号真实来源和新老账号基线保持独立约束。

### computer use

这里选 Anthropic 官方 computer-use-demo 作为可审查的参考实现，不把“computer use”当作一个统一产品。

- [sampling loop](https://github.com/anthropics/anthropic-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/computer-use-demo/computer_use_demo/loop.py)
- [computer tool](https://github.com/anthropics/anthropic-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/computer-use-demo/computer_use_demo/tools/computer.py)

示例把assistant输出、工具结果、API响应交给各自callback，并通过消息历史反馈下一轮观察。示例不提供Mino的正式原文覆盖、账号前置或逐例通过门禁，不能把它当作生产级测试质量方案。其历史图片过滤是上下文管理，不是删除持久测试证据的理由。

Mino借鉴：结果交付与模型叙述分离，每条结果具有机器可消费回执；Agent/客户端显示它，Scout保留原证据。

## 已实现

### 1. 统一正式结果门禁

`contract/execution_report.py` 的 `has_formal_evidence` 同时供统计与逐条回执判定使用，防止汇总PASS而回执UNVERIFIED：

- 原执行来源为Scout且sealed=true；
- 原文预期与断言计数均为正整数，全部通过，fail/unknown/not_run为0；
- 原文覆盖expected>0且missing为空；
- task、case、session、attempt、revision、source_hash一致且非空。

证据不足的原始pass投影为UNVERIFIED，保留原始执行状态，不篡改Scout历史。旧格式缺这些证据也不会继续按正式PASS统计。这是有意收紧质量契约，发布前应检查既有节点摘要字段。

`assistant/agent_workflow.py` 在verification complete之前读取本人任务并校验项目与该原文ID的所有匹配设备执行。缺任务、项目不符、调试、准备、局部通过、未封存或缺覆盖均拒绝完成标记。项目由成功来源收据绑定，不取Agent自报项目。

这仍不是自然语言语义证明：填齐source_ref不代表断言含义正确；原文与脚本的业务语义必须审查。当前也没有把整个来源快照digest与每条Scout原文hash自动等同起来，两者粒度不同。

### 2. 累计调查预算与结构化求助

`agent.workflow.init`增加：

- stage_time_budget_sec：每unit/阶段默认300秒；
- total_time_budget_sec：默认max(1800,expected_total×120)秒。

范围与预算在同一工作流中锁定。计时累计start到finish，包括该调查内等待；两次调查之间的用户等待不计入，并发调查会累计各自时间，不代表CPU耗时或整批墙钟时间。

start超限返回time_budget_exhausted，get返回time_budget与overdue_attempts。不会自动取消设备任务，也不阻止finish/resolve或只读核对；不把超时转成通过、失败或跳过。有效长业务流程应配置合适预算，预算不是缩短业务等待的手段。

finish可在blocked/deferred时保存help_request，必须有question、missing、checked_sources、affected_units，且受影响ID属于锁定原文范围并包含当前unit。get显示当前未解决求助，保留旧attempt历史。

共享缺口的依赖分组、从飞书补信息、用户回复后的调查决策属于Agent。本次没有新增服务端自动造数、接口猜测或测试决策循环。

### 3. 逐条结果回执

任务详情 `task_get.observability` / HTTP任务详情增加：

- case_receipts：终态、用途、位置、耗时、简短原因、原证据上下文、稳定receipt_id；
- active_cases与execution_pending：仍运行的执行；
- status_issue：任务终态但用例仍活跃的矛盾；
- delivery：快照模式、去重键、展示责任及acknowledged=false。

回执由已持久结果索引投影，不另建执行真源或保存完整事件。重复读取同一回执ID不变，摘要版本/执行结果更新产生新ID。summary隐藏常见凭据、Bearer、邮箱和手机号；回执不携带账号密码。

正式用途有formal_verdict，调试/准备为null；非正式通过显示OBSERVED而不是正式PASS。客户端/Agent按receipt_id去重后逐条可见输出，更新结果显式标明更新。

这不是Codex原生推送或客户端展示ACK。没有客户端消费，就不能声称结果已经在红框位置出现；工具返回、日志和最终报告不等于即时回执。当前MCP工具描述和技能要求逐条commentary以及长操作真实状态回执，但没有修改Codex客户端，也没有端到端展示确认。

### 4. Agent行为规范

脚本仓技能源 `skills/mino/references/execution-discipline.md`统一规定：

- 真实账号来自Nexus范围，禁止生成字符串并冒充已有资源；
- 事实、推测、冲突、未知分开保存，查询无来源时即时求助；
- 新账号0→1验证起始状态与对象归属，老账号验证历史版本与变更前基线；
- 不拿新数据代替历史样本，不自行改变原文预期；
- 不重复跑共享缺口，不把未执行依赖项伪装成Scout结果；
- 逐条终态和求助即时可见输出，预算不降低测试门槛。

同时修正原技能自报expected_units的旧示例，改为成功source_read_id绑定服务端原文全集。这里只修改Git源，未安装到Codex/Cursor/Claude。

账号创建来源审计、业务基线验证和客户端回执显示仍需各自运行证据；这些行为规范不能被描述成服务端已硬性阻止所有账号幻觉或已证明历史数据正确。

## 验证记录

实际运行，不使用pytest，不写tests/test_*.py，不访问真实业务数据库：

1. `tools/check_agent_quality_contracts.py`：预算锁定、超限、活跃超限、独立用例继续、用户等待不计入、求助校验、旧状态默认值；完整/不完整/错上下文/多设备证据、用途分离、去重、状态矛盾和脱敏均通过。
2. 隔离临时SQLite服务fixture：run_store→任务详情→workflow finish联调；缺task_id、缺原文覆盖、错项目被拒绝，完整fixture接受，重复finish幂等，其他用户看不到任务。使用合成Scout索引，不是真实设备证据。
3. 既有 `tools/check_execution_contracts.py`：渠道、混合目标、账号租约失效不换号、多slot范围、OTP和调试目标回归通过。
4. `tools/arch_report.py --check`分层通过；prompt repository成功加载全部82个工具，修改的工具schema可解析；代码编译与git diff --check通过。

没有重跑原通知中心/海外账号业务用例，没有部署Nexus，没有验证Codex客户端实际逐条展示。因此不能把契约检查通过当成真实测试质量问题已全部解决，也不能报告原任务通过率已提高。
