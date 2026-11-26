# Nexus CHANGELOG

Nexus 发布变更日志真源；版本真源为 `mino_nexus/VERSION`。只记录源码与实际验证，部署及真实业务验收另行确认。

## 0.1.143 — 2026-11-26

### 修复

- Scout WebSocket 入站回调使用独立、有界任务处理，收帧循环继续接收 RESULT，避免账号/资源回调反向 RPC 时相互等待。队列满返回 `callback_overloaded`，不冒充已持久化。迟到旧连接不能移除新注册会话。
- `record.sync` 校验与持久化完成后返回持久版本 ACK；投影异常记录节点、session、attempt、版本与异常类型，由后台及执行等待者重试，不因投影故障要求 Scout 重做执行。
- 终态投影不再以资源释放资格作为前置条件。投影在任务锁内重读最新持久摘要，避免迟到快照回退。后台按游标分页补齐运行中任务的终态，即使资源仍隔离也能同步结果；终结任务不直接释放资源，原封存、停止证明、未知写及租约代次守卫继续生效。
- 账户回调核验任务时绑定持久执行 owner；机器可读拒绝码不再被通用 ValueError/PermissionError 覆盖。诊断日志携带回调、节点、run、session，不记录回调参数、账号凭据或 OTP。
- MCP initialize 公共 instructions、工具 schema/agent_guide 描述与结构化契约改为由 Agent 判断用户明确测试/QA意图。普通产品研发、代码审查、部署、域名/TLS运维或仅提到 Mino 不触发测试工作流。控制面查询仍走 MCP；无业务绑定的只读工具调用不自动创建看板。明确测试入口保留业务绑定与原文锁定门禁。

- heartbeat 采用独立、有界 dispatch：单个在途线程加最新待处理心跳，等待 callback 事务锁不占用收帧循环。接收与反向 RPC 读取节点快照不获取 registry 锁；回调事务锁内不反向获取 registry 锁，断线的 run 查询移到锁外。
- 回调按连接/进程代次、执行 owner 和截止时间核验，获取 admission/节点事件锁后再次核验；事务提交前复核，超时回滚并报告 `callback_outcome_unknown`。关闭取消协程并有界等待，未结束线程仍跟踪、占用容量，不宣称停止或释放未知写。
- 已终态任务可刷新同一 case/session/attempt 的更高摘要版本及证据可用性；补偿扫描覆盖这类陈旧索引，不重开任务、不改变结论或任务结束时间。

### 验证清单

以下检查实际运行并通过，解释器为仓库 `.venv/bin/python`，使用 `-B`，不运行 pytest 或 `tests/test_*.py`：

- [x] `tools/check_nexus_record_sync.py`：隔离 SQLite + 模拟 socket；running 748 → sealed fail 833、旧摘要/重复 ACK、跨身份/终态冲突拒绝、投影故障仍 ACK、锁内最新投影、未知写保持隔离、补偿扫描分页、已终态 834 摘要/证据刷新及扫描收敛、嵌套 RPC、队列上限、旧连接失效/关闭、新旧 owner 上下文、无凭据诊断、initialize/tools/list/guide 契约、只读不建看板、正式测试门禁。
- [x] `tools/check_nexus_callback_lifecycle.py`：真实临时 SQLite 慢回调 + registry/DB 竞争写 + 20 次 heartbeat + 嵌套 RESULT 持续收帧，有界合并、无锁环；超时结果未知、关闭有界等待、过期事务回滚、无迟到凭据；连接/owner/进程代次及 close 后 admission 拒绝，节点事件等待锁后拒绝旧代次，未结束线程仍占队列容量。
- [x] `tools/check_execution_contracts.py`：渠道/目标冻结、账户作用域、多 slot、租约与 OTP。
- [x] `tools/check_agent_task_board.py`：绑定/公告/来源门禁、业务投影、问题状态与只读诊断无自动看板。
- [x] `tools/check_debug_resource_recovery.py`：封存停止证明、未知写隔离、代次冲突回滚、跨 owner 拒绝、恢复幂等及原结论不变。
- [x] `tools/check_agent_quality_contracts.py`：完整证据判定、回执去重、上下文/多设备/用途边界与真实心跳状态。
- [x] `tools/arch_report.py --check`：分层违规 0 条。
- [x] `tools/check_lazy_imports.py`：函数内导入可解析。
- [x] `tools/check_globals.py`：无未定义全局名。
- [x] `tools/check_calls.py`：关键字参数与形参一致。
- [x] `tools/check_returns.py`：无意外函数末尾返回。
- [x] `git diff --check`：无空白错误。

### 交付边界

- CR261126100415821 / EMB002 的 Scout sealed fail833、云 running748 与旧批节点断线是本次提供的排查线索；748→833 在隔离夹具复现并验证，不代表已读取或修复线上任务。
- 未操作真实数据库、真实节点/UI、未重启服务、未 commit/push/deploy。既有用户部署工作流/脚本修改保留；仅修改 MinoNexus 仓库。未修改 Scout 协议或其他仓库。
- 源码版本更新不证明服务已加载；主线程审阅整合后另行提交、发布及核对真实 task/session/attempt 的终态与回执。
