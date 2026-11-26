# Web 写隔离恢复

## 三种状态不能混用

- 任务终态、节点在线和租约过期不等于资源已释放。
- `open_actions` 只计任务级未结算动作，不计独立执行资源的隔离状态。
- 原 Scout 的停止证明只证明旧执行不会继续，不证明历史业务写入成功或业务状态已复位。

## 两个恢复入口

1. `task_resource_reconcile(task_id)`：自动结果审计路径。原节点的封存事件必须完整，所有写结果明确，停止证明绑定同一任务、会话、attempt、账号、节点、SN、版本及进程代际。
2. `task_write_safety_baseline(task_id)`：用户明确要求恢复资源时的复核路径。Web 必须有 `retired_generation` 停止证明、`worker_active=false`、`browser_closed=true`，并完整读取原 Scout 封存事件。历史未知写结果可以保留，但不能自动重放。

第二个入口不要求 `open_actions>0`；它按持久 Scout 记录及执行资源恢复。非 Web 仍要求停止证据和实际截图基线；截图回执与待复核动作快照在提交事务内再次核对。

## 提交与回执

恢复在一个 SQLite 写事务里再次核验记录、归属及资源代次，提升 epoch，释放对应资源和关联租约，并保存 `resource_resolution` / `isolation_exit`。记录变化或代次不匹配时，释放事务回滚。获取停止证明过程中保存的证据不等于已完成释放。

显式 Web 复核返回 `verification_kind=retired_web_lane_review`、`historical_outcome_preserved=true`、`automatic_replay_allowed=false`。`resource_reset_verified=false`：没有核验远端业务复位。原 Scout 摘要、失败原因和测试结论不改写。重复请求返回已提交的同一恢复结果。

## 拒绝的含义

- `recovery_write_outcome_unknown`：普通 reconcile 不能自动解决；有用户恢复授权时走显式复核。
- `stop_evidence_required`：身份或停止证据不足，显式复核也拒绝。
- `recovery_write_evidence_*`：封存事件缺失、变化或格式不完整，不能跳过审计。
- `resource_resolution_conflict`：记录、资源归属或 epoch 已变化，重新核验，不强删记录。

不删业务库记录，不新增或改名 Scout 节点绕过隔离，不把恢复资源当作测试通过，不自动重跑原任务。
