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
            "脚本文本只写在本机的脚本仓库工作区，不要把脚本 POST/PUT 给 Nexus。各仓库分工与 Scout 安装见 agent 技能 overview。",
            "Git 提交和推送仍在本机。Nexus 不保存 Git 令牌，也不拉取脚本仓库；设备所在电脑上的 Scout 用本机 git 凭据自己拉取。改了脚本仓库 config.yaml 的 apps 后，必须 push 并调用 repo_apps_sync 告知 Nexus；选应用前可先调 repo_apps_get。",
            "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。验证码和模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
        ],
        "workbench": _workbench(),
        "flows": [
            {
                "name": "确认能操作哪些项目",
                "tools": ["project_list"],
            },
            {
                "name": "编写或修改用例脚本",
                "tools": ["project_list", "case_list"],
                "then": "使用已安装的 agent 技能 write-test-cases 编写脚本（先读 repo-config 了解仓库构成，project_list 返回 repo_url / repo_branch），写完用 check-test-cases 自检，再 commit 并 push。",
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
                "name": "自己操作设备并记到任务里",
                "tools": ["case_external_begin", "case_external_report", "case_external_screenshot", "case_external_finish"],
            },
            {
                "name": "看资源够不够",
                "tools": ["resource_overview", "resource_device_list", "resource_account_list"],
            },
        ],
    }
