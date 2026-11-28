"""外部 agent 使用 Mino 的入口说明。真源是 MCP 工具 agent.guide，不依赖某个仓库里的 skill 文件。"""
from __future__ import annotations

from typing import Any

from mino_nexus.assistant.agent_contract import agent_contract


def _workbench() -> dict[str, str]:
    url = ""
    try:
        from mino_nexus.assistant.assistant_reports import configuration
        from mino_nexus.contract.ownership import current_user_id

        uid = current_user_id()
        if uid:
            url = str(configuration(uid).get("effective_url") or "")
    except Exception:
        url = ""
    return {
        "url": url,
        "configuration_status": "configured" if url else "unconfigured",
        "connection_status": "not_checked",
        "login_failure_message": "无法连接服务器",
    }


def agent_guide() -> dict[str, Any]:
    guide = {
        "channel": "Mino MCP。工具名里的点写成下划线，例如 agent.guide 调用为 agent_guide。",
        "first_call": "明确测试/QA意图时先 agent_guide；普通研发和只读诊断不强制测试启动",
        "trigger": "Agent按用户明确测试/QA意图判断。普通产品研发、代码审查、部署、域名/TLS运维或仅提到Mino，不启动测试工作流、不锁定用例、不要求业务看板。控制面查询仍走MCP；只读诊断可省略agent_task_id。",
        "agent_execution": {
            "tools": ["agent_task_start", "agent_task_event", "agent_task_heartbeat", "agent_task_question",
                      "agent_task_answer", "agent_task_acknowledge", "agent_task_link", "agent_task_target", "agent_task_get", "agent_task_events", "agent_task_list"],
            "contract": "仅用户明确要求的多步测试/QA任务开始先 agent_task_start，立即在可见对话展示 startup_message/board_url，通过 agent_task_event(startup_delivered=true,summary 含 board_url) 声明发送后再执行业务；该声明不证明用户已读。上报 phase/summary/next_action/scope/environment（明确应用、端与环境）。已有真实需求 ID 时用 agent_task_target 挂载应用/端/环境节点；同节点可挂多个任务，禁止猜测归属。空地址报告入口未配置。后续 Mino 工具调用带 agent_task_id；Nexus 自动记录真实调用起止、耗时与引用。客户端本地分析、写文件与其他工具不可见，必须显式 event 和实际心跳。测试和工作流入口缺少业务 task_id 则拒绝；只读独立调用归入技术诊断，不推断 Agent 运行。",
            "help": "question 立即登记问题；get 读取 answers 后 acknowledge，答案已读取不等于问题解决。Nexus 内置助手问题走原 request/revision 续接；外部 Agent 不可由服务端擅自唤醒。",
            "liveness": "执行中超过60秒未有心跳状态为unknown，保留reported_status；心跳不证明设备动作或测试通过。暂停只能由执行方确认，不用UI点击伪造停止。",
        },
        "visible_receipts": {
            "per_case": "每条回执立即完整输出 case_receipts[].text：身份、环境、预期与实际、证据可用性、原因和下一步，未知不补猜；按 receipt_id 去重，不按 case_id 合并渠道或重跑。按批输出，不等到最终报告；长时间静默违反契约。",
            "help": "缺 OTP、账号、环境、配置、数据或需求歧义，必须在对话流输出 HELP：已查来源、缺失项、需要用户做什么。不得静默等待。",
            "accounts": "禁止编造账号、邮箱、OTP、手机号或业务数据；账号只取 resource.account_*。新账号 0→1 与老账号历史数据分开取证。",
            "alerts": "飞书告警是观察，不是测试证据；没有来源证据不得归因或忽略。",
        },
        "agent_workflow": {
            "owner": "agent",
            "applies_when": "用户明确要求测试用例编写、执行或验证；不用于普通研发、运维或只读诊断",
            "tools": ["case_source_list", "case_source_read", "agent_workflow_init", "agent_workflow_start", "agent_workflow_finish", "agent_workflow_resolve", "agent_workflow_get", "agent_workflow_bootstrap", "agent_workflow_draft", "agent_workflow_list", "agent_workflow_answer", "agent_workflow_context", "agent_workflow_report", "agent_workflow_finding"],
            "storage": "Nexus 当前部署数据库，按登录用户与 workflow_id 隔离；使用 Python SQLite，不使用 Agent 本地数据库",
            "source_drafting": "来源确实为空时先 bootstrap 登记目标，读取需求、补写人工用例并发布回读，再 init 锁定。读取失败不能当空表。人工业务歧义立即求助，其余继续。",
            "delivery": "每次 finish 自动保存阶段报告；所有终止路径调用 report(final=true) 并输出报告。get 读取 help_requests.answer 和 contexts；人工答案需要查证后恢复，不等于问题已解决。",
            "stages": ["analysis", "authoring", "preparation", "verification"],
            "contract": "init 前必须先用 case_source_list 取得来源 generation，再用 case_source_read 读取并取得成功快照的 read_id；init 只传 source_read_id，Nexus 从成功 CaseSourceRead 回执绑定 source 并锁定 expected_units；显式自报的来源或全集不一致时拒绝。units 只是首批 delivery hint，默认预算至少为 expected_units × 4。coverage.missing 是未实现的非终态条目，verification_missing 是未有正式 Scout 证据的非终态条目，next_units 按 missing 后 verification_missing 取下一批。implementation_complete/verification_complete 表示当前交付阶段已无未处理工作项，但终态阻塞/失败/延期仍单独计入 blocked_units；只有 all_implemented=true 才能说脚本全集写齐，只有 all_verified=true 才能说正式全集通过。complete 仅为 implementation_complete 的兼容别名。每轮调查 start 占用预算、finish 保存事实，allowed=false 按原因改变方案或交付阶段。重复阻塞须有新事实或明确不同方案；写结果未知先核对原任务并 resolve。普通状态轮询不算新调查。进度不是测试判定。 正式 case_run 只要带 script_targets 就必须传 workflow_id，服务端会核对成功 source_read 回执、expected_units 与项目一致性，缺失或不一致直接拒绝（reason_code 见错误返回）。",
        },
        "boundaries": [
            "读取飞书需求正文、用例表格或 Wiki 节点必须调用 Mino MCP `cli_query` 且 `plugin=feishu`（wiki +node-get → sheets +sheet-list → sheets +cells-get）；禁止用平台 Computer Use、浏览器页面、截图识别或手工抄录代替正文读取。正文读取不完整时停止并说明缺口，不能猜全集。",
            "编写“所有用例”前必须通过 case_source_list → case_source_read 取得成功 read_id 并由 Nexus 回执锁定 expected_units；不得用 tests/ 当前文件反推全集，也不得把 Agent 扫描或手抄结果当作全集。coverage.missing 或 verification_missing 非空时只能报告 X/Y 和剩余 ID，不能报告需求已全部完成；存在 blocked_units 时必须逐条列出，不能用 implementation_complete=true 掩盖。只有 all_implemented=true 才表示脚本全集写齐，只有 all_verified=true 才表示正式全集通过；二者都必须结合原 Scout task_id/断言证据。",
            "case_run 的 script_targets[].case_ids=[] 只表示 tests/<需求ID>/ 下当前已有脚本全集，不表示原文 expected_units 全集；用户要求执行“所有用例”前必须先用 agent_workflow_get.coverage 对账。带 script_targets 的正式执行必须同时传 agent_workflow_get 返回的 workflow_id；Nexus 用成功 case_source_read 回执、锁定的 expected_units 和项目一致性做硬门禁。"
            "脚本不得硬编码账号、邮箱、手机号或密码；业务测试账号只能来自 Nexus 号池并经 case.nexus.account(...) 租用。账号、商品、模板、订单等业务对象缺失时按 Nexus 资产记忆 → 飞书 CLI → Scout 事实的阶梯查证；阶梯走完仍缺就停下向用户发结构化 help_request（question/missing/checked_sources/affected_units），不得编造，也不得用截图或手抄代替正文。",
            "执行中必须有可观察回执：每 60 秒（或拿到新证据时）读 task_get.observability.active_cases，并在对话里输出固定格式心跳 [i/N 执行单元] case_id | active_cases.status（真实状态） | 已耗时 | step: 当前步骤 | waiting: 等待对象 | evidence: 最近证据；没有新证据就明说没有。每条用例终态（PASS/FAIL/BLOCKED/SKIPPED/UNVERIFIED/OBSERVED/CANCELLED）完整输出 case_receipts[].text，包含 receipt_id、被测构建和 disposition；证据不足用 UNVERIFIED。script_repair 先改脚本并完成 authoring，再只正式跑 coverage.script_rerun。用例表的预期结果、状态列和备注里的「需求为…」都不是需求正文。空格、大小写或连写（Best Sellers 与 Bestsellers）只能记 accepted_difference 并改用例，不能记 FAIL、bug 或 product_mismatch；服务端会拒绝这种缺陷草稿。只有需求或设计稿正文写出了不同的词，或界面位置被正文要求、或功能错误，才记 bug。对话收口只写：通过、可接受差异、缺陷草稿（编辑中）、仍未测。不要用 PASS/FAIL 数量当结论，也不要问用户接着补测、配数据还是封存。precondition_historical 与 build_mismatch 仍停止并保留证据。需求收口看 testing_closed 和 disposition_counts，all_verified 只表示全部正式通过。case_run 在需求指定构建时传 expected_build。正式脚本先 app.version()。界面外的预期用 case.nexus.fact，不用 on_page 代替。",
            "新账号 0→1 与老账号历史基线验证必须分开：新账号只验证注册/首登/初始态，老账号历史数据必须先有明确的历史订单/商品/模板输入或可查来源；缺少基线就 blocked 并在 help_request 里写清需要哪个对象、已查哪些来源，不得假设历史数据存在，也不得为了推进而新建替代数据。",
            "缺数据后的查找、构造、替代和重试决策只属于 Agent；脚本仓不内置 get_or_create、缺数据自动补数或自动换号。flows 只执行明确业务操作、识别阶段、等待、定位指定对象和复用验证方法。",
            "账号业务标签与资产记忆是选号线索，缺省、未知或未命中不得单独阻塞执行。申请账号时用 case.nexus.account(data_need=描述) 留存所需数据；resource_account_data_list/save/delete 管理独立对象引用记忆，不创建被测业务数据、不进行全量盘点。",
            "业务测试账号的唯一事实源是 Nexus 号池；账号元数据通过 resource_account_* 维护，执行时只由租约把本次所需字段交给 Scout。不要从 macOS Keychain、本地明文、环境变量或其他钥匙串读取业务测试账号。飞书 CLI、SMB、AI Provider、Scout Gmail 和插件密钥等本地专属或系统配置继续按各自现有边界保存，不进入号池。",
            "对象生产后绑定实际 ID 或区分特征，弹窗重绘、跨页和跨应用使用该变量重新定位并验证关联。业务阶段放 flows，原文映射和整例编排留用例。相同缺口无新观察时停止重复申请与整例重跑，Agent 选择有依据的准备路径；写结果未知先查同一任务。探针与准备用途排除正式统计；task_get.statistics 按执行次数、原文预期和辅助断言分列，completed 是已结束。通过率无判定为 null；blocked/declined/skipped/cancelled 不算 failed。报告绑定同一 task/session/attempt/revision/source_hash 和带时区记录时间；准备及续跑独立，不把历史局部通过拼成本次结论。缺完整证据时展示保存的摘要并说明证据暂不可用。",
            "编写工作区由本机 agent 编辑；debug_case 直接在该目录调试，正式 case_run 使用 Git 固定版本与独立任务工作区。MCP 不编辑工作区文件。各仓库分工与 Scout 安装见 agent 技能 overview。",
            "改了 config.yaml 的 apps 后必须 push 并调用 repo_apps_sync（只传 platform 与包名/URL，不要传 otp；可不传 app_id，Nexus 按 key 对齐或自动创建应用）；选应用前可先调 repo_apps_get。Nexus 不保存 Git 令牌，也不拉取脚本仓库。",
            "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。登录方式、验证码策略、Gmail 收件箱和自动新增序号由 Nexus 项目环境配置下发；只有 login.mode=auto 才会在缺号时自动补号，pool 只使用号池已有账号，hitl 转人工。应用级 login_policies 的 selected 优先于 all。脚本仓 config.yaml 只保留应用 key、platform、package/bundle_id/url；otp.code 仅兼容旧仓库，不应作为新项目配置。模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
        ],
        "quality_contract": {
            "objective": "verify_original_expectations",
            "formal_completion": {"requires_scout_task_id": True, "requires_sealed_original_assertions": True,
                                  "requires_full_source_coverage": True, "requires_matching_context": True,
                                  "requires_same_project": True, "all_matching_devices_must_pass": True},
            "investigation": {"stage_time_budget_sec": 1800, "total_time_budget_sec": "max(14400,expected_total*600)",
                              "policy": "advisory", "decision_owner": "agent",
                              "single_execution_default_min_sec": 1800,
                              "scope": "summed_attempt_elapsed_including_waits", "cancels_device_tasks": False},
            "help_request_fields": ["question", "missing", "checked_sources", "affected_units"],
            "receipts": {"path": "task_get.observability.case_receipts", "deduplicate_by": "receipt_id",
                         "display_owner": "calling_agent_or_client", "tool_return_is_delivery_ack": False},
        },
        "workbench": _workbench(),
        "flows": [
            {
                "name": "安装或卸载本机 Scout",
                "tools": ["scout_release_meta", "scout_install_token", "scout_install_status", "scout_list", "scout_uninstall"],
                "then": "安装或检查更新前可先调 scout_release_meta 查看当前 OS/arch 的最新稳定版本、build_id 和 release_tag；本机当前版本用 mino-scout status。安装先 scout_install_token 获取命令，再在本机执行匹配系统的命令，遵循客户端的本机执行权限，不要把 token 写进对话。用户要求卸载时调用 scout_uninstall 删除绑定，再执行返回的本机卸载命令。Nexus 不追加确认。",
            },
            {
                "name": "确认能操作哪些项目",
                "tools": ["project_list"],
            },
            {
                "name": "新建项目或删除误建应用",
                "tools": ["project_list", "project_create", "project_delete", "app_delete"],
                "then": "参数齐全且用户已要求该操作时直接调用工具。Nexus 不追加确认；参数缺失时补齐参数，删除须对应用户明确要求的对象。",
            },
            {
                "name": "配置项目脚本仓库",
                "tools": ["project_list", "project_script_repo_set", "repo_apps_sync"],
                "then": "先用 project_script_repo_set 保存项目脚本仓库 URL 和分支；仓库地址不要带令牌，Nexus 不拉仓库也不保存 Git 凭据。修改 config.yaml 的 apps 并 push 后，调用 repo_apps_sync 同步新增、修改或删除的应用 key，revision 使用 push 后的 commit。",
            },
            {
                "name": "编写或修改用例脚本",
                "tools": ["project_list", "device_list", "debug_case", "debug_case_execute", "debug_case_status", "debug_case_cancel", "debug_case_reset", "debug_case_checkpoint", "debug_case_close", "session_events", "session_screenshot", "case_run"],
                "then": "先用本机 mino-scout status 和 device_list 核对节点/设备。debug_case 绑定本机编写工作区，不拉仓、不要求 commit；debug_case_execute 提交 code 或 script_path，保留设备和应用现场，刷新 pages/flows。先观察 nodes、验证定位及公共路径，再扩展完整用例。debug_case_status 查询请求，session_events/screenshot/artifact 读取同一证据；取消片段、重置和结束使用对应 debug_case 工具。写结果未知只读核验，不盲目重试。用 debug_case_checkpoint 记录草稿、完整实现、路径验证，后者绑定同版本执行证据。结束并确认停止后，固定完整 commit，case_run 带 debug_origin 引用检查点，从独立初始状态正式验证；远端节点只用于正式执行。",
            },
            {
                "name": "登记或维护项目需求",
                "tools": ["project_list", "requirement_list", "requirement_save", "requirement_update"],
                "then": "新建需求使用 requirement_save，至少传 project_id、title 和应用：单应用传 app_id，关联多个应用时传 app_ids。requirement_id 可选，未传时自动生成；source_url 是需求文档，cases_url 是用例文档。相同 requirement_id 重复保存会更新原记录。需求列表为空时不要调用 requirement_update；维护已有需求时先用 requirement_list 核对，再调用 requirement_update。写入结果未知时先重新读取 requirement_list 核对，不盲目重试。",
            },
            {
                "name": "按需求链接编写并执行脚本",
                "tools": ["requirement_list", "cli_query", "device_list", "debug_case", "debug_case_execute", "debug_case_status", "debug_case_close", "case_run", "task_get"],
                "then": "requirement_list 返回需求 id、名称和渠道链接。飞书正文必须调用 cli_query(plugin=feishu)，依次用 wiki +node-get、sheets +sheet-list、sheets +cells-get 机器读取完整用例 ID；禁止用 Computer Use、浏览器截图或手工抄录代替。先锁定 agent workflow 的 expected_units；在本机 debug_case 中先观察和验证 pages/公共路径，再完成 tests/<需求ID>/<用例ID>.py。静态检查通过、结束调试并确认停止后，push 固定版本。case_run 传 script_targets：[{requirement_id, case_ids}]，revision 传已 push 的 commit，从独立初始状态正式验证。case_ids 为空只执行该需求目录下当前已有脚本，不等于原文全集；每批从 coverage.next_units 继续，脚本全集写齐看 all_implemented=true，正式全集通过看 all_verified=true。implementation_complete/verification_complete 只能表示当前阶段无未处理项，blocked_units 仍须单列，不能用其掩盖缺口。",
            },
            {
                "name": "让 Scout 跑整份脚本",
                "tools": ["case_list", "device_list", "case_run", "task_get"],
                "then": "case_run 的 revision 传已 push 的 commit。执行与汇报使用 agent 技能 run-test-cases。",
            },
            {
                "name": "让节点安装渠道程序",
                "tools": ["node_channel_program"],
                "then": "只转发给该节点下载中心已有的安装请求。Nexus 不下载安装包，也不接受临时地址。",
            },
            {
                "name": "查看一次执行",
                "tools": ["task_list", "task_get", "session_list", "session_events", "session_screenshot"],
            },
            {
                "name": "复用测试文件并主动准备缺口",
                "tools": ["resource_asset_list", "resource_asset_prepare", "resource_asset_prepared", "debug_case", "debug_case_execute", "case_run", "task_get"],
                "then": "文件分三种状态，不能混用。执行任务时生成：准备脚本把图片、模型、纹理、3MF 写到 case.output_dir（execution/<task_id>/attempts/<attempt>/output），再 case.assets.publish；publish 只接受该目录中的文件，复制到 ~/.mino/scout/assets/<账号>/library/<sha256>，返回 role=generated。本次任务继续用自己写出的路径。使用之前的：先 resource_asset_list 或准备任务的 reuse，只拿 {asset_id,version}，不打开旧任务 output，不重新生成，并派到 source.node_id。用例执行时：正式用例从 case_inputs 取引用，case.assets.fetch 校验库文件后复制到本次 output_dir/.assets/<asset_id>/v<version>/；用例只改这份副本。换节点得到 asset_not_on_this_scout。Git 和 Nexus 不放文件正文。缺资源先查目录、账号对象记忆、原需求和准备配方，在已配置预算内 resource_asset_prepare 去重领取，claimed=true 才准备；wait 继续独立用例，原结果未知不重复生成。准备登记后再恢复原用例正式验证。内部 plugin/session/参数错位由 Agent 查 schema 和已知映射修正，不转用户问题。",
            },
            {
                "name": "记录账号业务数据并准备输入",
                "tools": ["resource_account_select", "resource_account_list", "resource_account_create", "resource_account_update", "resource_account_import", "resource_account_data_list", "resource_account_data_save", "resource_account_data_delete", "case_run", "task_get"],
                "then": "Agent 先用 resource_account_select(data_requirements=[{kind, attributes}], env, app_id) 取得按资产记忆排序的空闲候选；明确需要补号时用 resource_account_create 或 resource_account_import，修改已有账号用 resource_account_update，批量导入先传 preview=true 核对再 commit。用 case_inputs 按用例 ID 或脚本路径传入 account_id 和业务对象。脚本 case.data.require 返回 data_missing 时，Agent 读取 task_get 的 cases[].data_missing 和本次证据，在已有授权范围内选择明确的准备脚本，通过 case_run(source_context={parent_task_id, purpose:\"prepare\", data_need}, case_inputs) 单独执行。准备脚本显式操作并 case.data.bind 输出对象和 account_id；Agent 核对 cases[].data_outputs、写入 resource_account_data_save，再用新的 client_request_id 和 purpose:\"resume\" 创建原用例的新任务，传回相同 account_id 与对象。Nexus 只保存关系和调度，脚本仓不含自动造数、换号或重试策略；写入结果未知先查询同一次 attempt，不能重复创建。",
            },
            {
                "name": "看资源或配置账号自动补池",
                "tools": ["resource_overview", "resource_device_list", "resource_account_list", "resource_account_create", "resource_account_update", "resource_account_delete", "resource_account_import", "resource_env_get", "resource_env_set"],
                "then": "登录方式、验证码策略、Gmail 收件箱、应用级 login_policies、手机号/Gmail 别名序号和自动补池策略由 Nexus 项目环境配置决定。selected 策略对指定应用优先于 all；只有 login.mode=auto 才会在缺号时自动补号，pool 只使用号池已有账号，hitl 转人工。不要把这些字段写进脚本仓 config.yaml。账号新增/修改/批量导入后回读 resource_account_list 核对；删除只用于用户明确要求删除的空闲账号。",
            },
            {
                "name": "恢复已结束 Web 任务的隔离资源",
                "tools": ["task_get", "device_list", "task_resource_reconcile", "task_write_safety_baseline"],
                "then": "正常任务结束后自动结算资源。用户要求恢复历史隔离时调用 task_resource_reconcile(task_id)，由原 Scout 核验进程及浏览器停止，再原子释放槽位和账号；历史写结果未知时，不重复尝试 reconcile 或换节点绕过。用户明确要求复核并恢复该资源时调用 task_write_safety_baseline：Web 必须由原 Scout 证明旧代际已退役，或提供同代际 terminated_attempt 的准确执行进程树停止证明；均须无活动 worker、浏览器已关闭，并完整审计封存记录；复核只释放执行资源，保留历史未知结论，不核验业务复位、不自动重放原写动作。停止或身份证据不足继续隔离，租约到期不代表自动恢复。无需登录 Workbench。",
            },
        ],
    }
    guide.update(agent_contract())
    return guide
