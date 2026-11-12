"""外部 agent 使用 Mino 的入口说明。真源是 MCP 工具 agent.guide，不依赖某个仓库里的 skill 文件。"""
from __future__ import annotations

from typing import Any


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
        "login_failure": "无法连接服务器",
    }


def agent_guide() -> dict[str, Any]:
    return {
        "channel": "Mino MCP。工具名里的点写成下划线，例如 agent.guide 调用为 agent_guide。",
        "first_call": "agent_guide",
        "boundaries": [
            "脚本文本只由 agent 写在本机脚本仓并通过 Git 推送；MCP 只做查询、同步元数据与触发执行，不写代码。不要把脚本 POST/PUT 给 Nexus。各仓库分工与 Scout 安装见 agent 技能 overview。",
            "改了 config.yaml 的 apps 后必须 push 并调用 repo_apps_sync（只传 platform 与包名/URL，不要传 otp；可不传 app_id，Nexus 按 key 对齐或自动创建应用）；选应用前可先调 repo_apps_get。Nexus 不保存 Git 令牌，也不拉取脚本仓库。",
            "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。验证码优先读脚本仓 otp.code，否则要人工提供；模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
        ],
        "workbench": _workbench(),
        "flows": [
            {
                "name": "确认能操作哪些项目",
                "tools": ["project_list"],
            },
            {
                "name": "编写或修改用例脚本",
                "tools": ["project_list", "case_list", "device_list", "case_run", "task_get", "session_events", "session_screenshot"],
                "then": "使用 write-test-cases。先选一台 executable 设备。页面定位、点击、截图和界面结构都写进用例脚本，用 app.hierarchy()、app.screenshot()、app.tap() 等 Scout 运行时方法，经 case_run 触发 Scout 的 script.run。从这次运行的 tool/result（capability_id=hierarchy 的 nodes 或 hierarchy_text）和截图写 pages。一步不是单个控件能完成时写成 flows/（连续动作）或 scripts/（不碰设备的逻辑），随同一轮 case_run 调试。写成覆盖原文的可执行脚本后，push，再用同一台设备 case_run 一次。不要用浏览器、adb 或其他工具另采页面参数。",
            },
            {
                "name": "按需求链接编写并执行脚本",
                "tools": ["requirement_list", "device_list", "case_run", "task_get"],
                "then": "requirement_list 返回需求 id、名称和渠道链接。从链接读取用例，写成 tests/<需求ID>/<用例ID>.py 并 push。case_run 传 script_targets：[{requirement_id, case_ids}]。case_ids 为空时执行该需求目录下的全部脚本。revision 传已 push 的 commit。",
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
                "name": "看资源够不够",
                "tools": ["resource_overview", "resource_device_list", "resource_account_list"],
            },
        ],
    }
