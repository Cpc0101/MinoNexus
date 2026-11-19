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
        "first_call": "agent_guide",
        "agent_workflow": {
            "owner": "agent",
            "tools": ["agent_workflow_init", "agent_workflow_start", "agent_workflow_finish", "agent_workflow_resolve", "agent_workflow_get"],
            "storage": "Nexus 当前部署数据库，按登录用户与 workflow_id 隔离；使用 Python SQLite，不使用 Agent 本地数据库",
            "stages": ["analysis", "authoring", "preparation", "verification"],
            "contract": "每轮调查 start 占用预算、finish 保存事实，allowed=false 按原因改变方案或交付阶段。重复阻塞须有新事实或明确不同方案；写结果未知先核对原任务并 resolve。普通状态轮询不算新调查。进度不是测试判定。",
        },
        "boundaries": [
            "缺数据后的查找、构造、替代和重试决策只属于 Agent；脚本仓不内置 get_or_create、缺数据自动补数或自动换号。flows 只执行明确业务操作、识别阶段、等待、定位指定对象和复用验证方法。",
            "账号业务标签与资产记忆是选号线索，缺省、未知或未命中不得单独阻塞执行。申请账号时用 case.nexus.account(data_need=描述) 留存所需数据；resource_account_data_list/save/delete 管理独立对象引用记忆，不创建被测业务数据、不进行全量盘点。",
            "对象生产后绑定实际 ID 或区分特征，弹窗重绘、跨页和跨应用使用该变量重新定位并验证关联。业务阶段放 flows，原文映射和整例编排留用例。相同缺口无新观察时停止重复申请与整例重跑，Agent 选择有依据的准备路径；写结果未知先查同一任务。探针与准备用途排除正式统计；task_get.statistics 按执行次数、原文预期和辅助断言分列，completed 是已结束。通过率无判定为 null；blocked/declined/skipped/cancelled 不算 failed。报告绑定同一 task/session/attempt/revision/source_hash 和带时区记录时间；准备及续跑独立，不把历史局部通过拼成本次结论。缺完整证据时展示保存的摘要并说明证据暂不可用。",
            "编写工作区由本机 agent 编辑；debug_case 直接在该目录调试，正式 case_run 使用 Git 固定版本与独立任务工作区。MCP 不编辑工作区文件。各仓库分工与 Scout 安装见 agent 技能 overview。",
            "改了 config.yaml 的 apps 后必须 push 并调用 repo_apps_sync（只传 platform 与包名/URL，不要传 otp；可不传 app_id，Nexus 按 key 对齐或自动创建应用）；选应用前可先调 repo_apps_get。Nexus 不保存 Git 令牌，也不拉取脚本仓库。",
            "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。登录方式、验证码策略、Gmail 收件箱和自动新增序号由 Nexus 项目环境配置下发；号池为空或没有匹配账号时由 Nexus 自动补号。脚本仓 config.yaml 只保留应用 key、platform、package/bundle_id/url；otp.code 仅兼容旧仓库，不应作为新项目配置。模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
        ],
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
                "tools": ["requirement_list", "device_list", "debug_case", "debug_case_execute", "debug_case_status", "debug_case_close", "case_run", "task_get"],
                "then": "requirement_list 返回需求 id、名称和渠道链接。从链接读原文，在本机 debug_case 中先观察和验证 pages/公共路径，再完成 tests/<需求ID>/<用例ID>.py。静态检查通过、结束调试并确认停止后，push 固定版本。case_run 传 script_targets：[{requirement_id, case_ids}]，revision 传已 push 的 commit，从独立初始状态正式验证。case_ids 为空时执行该需求目录下的全部脚本。",
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
                "name": "记录账号业务数据并准备输入",
                "tools": ["resource_account_select", "resource_account_list", "resource_account_create", "resource_account_update", "resource_account_import", "resource_account_data_list", "resource_account_data_save", "resource_account_data_delete", "case_run", "task_get"],
                "then": "Agent 先用 resource_account_select(data_requirements=[{kind, attributes}], env, app_id) 取得按资产记忆排序的空闲候选；明确需要补号时用 resource_account_create 或 resource_account_import，修改已有账号用 resource_account_update，批量导入先传 preview=true 核对再 commit。用 case_inputs 按用例 ID 或脚本路径传入 account_id 和业务对象。脚本 case.data.require 返回 data_missing 时，Agent 读取 task_get 的 cases[].data_missing 和本次证据，在已有授权范围内选择明确的准备脚本，通过 case_run(source_context={parent_task_id, purpose:\"prepare\", data_need}, case_inputs) 单独执行。准备脚本显式操作并 case.data.bind 输出对象和 account_id；Agent 核对 cases[].data_outputs、写入 resource_account_data_save，再用新的 client_request_id 和 purpose:\"resume\" 创建原用例的新任务，传回相同 account_id 与对象。Nexus 只保存关系和调度，脚本仓不含自动造数、换号或重试策略；写入结果未知先查询同一次 attempt，不能重复创建。",
            },
            {
                "name": "看资源或配置账号自动补池",
                "tools": ["resource_overview", "resource_device_list", "resource_account_list", "resource_account_create", "resource_account_update", "resource_account_delete", "resource_account_import", "resource_env_get", "resource_env_set"],
                "then": "登录方式、验证码策略、Gmail 收件箱、手机号/Gmail 别名序号和自动补池策略由 Nexus 项目环境配置决定。号池为空或没有匹配账号时，租号会按该策略自动新增；不要把这些字段写进脚本仓 config.yaml。账号新增/修改/批量导入后回读 resource_account_list 核对；删除只用于用户明确要求删除的空闲账号。",
            },
            {
                "name": "恢复已结束 Web 任务的隔离资源",
                "tools": ["task_get", "device_list", "task_resource_reconcile"],
                "then": "正常任务结束后自动结算资源。用户要求恢复历史隔离时调用 task_resource_reconcile(task_id)，由原 Scout 核验进程及浏览器停止，再原子释放槽位和账号；证据不足保持隔离，原测试结论不变。无需登录 Workbench。",
            },
        ],
    }
    guide.update(agent_contract())
    return guide
