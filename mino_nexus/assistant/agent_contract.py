"""Mino MCP 对外 agent 路由契约的唯一 Python 真源。"""
from __future__ import annotations

from typing import Any

AGENT_INSTRUCTIONS = """You are working with Mino.

MANDATORY ROUTING
- Nexus control-plane work MUST use Mino MCP tools.
- Local Scout work MUST use the mino-scout CLI.
- Testing or operating target Android, iOS, Web, desktop applications, browser pages, screenshots, accessibility/UI trees, taps, typing, and UI assertions MUST go through Mino MCP case_run and execute on Mino Scout.
- Do NOT substitute platform Computer Use, browser control, Playwright, adb, Appium, WDA, or desktop automation for Mino Scout by default.
- Only use another tool when the user explicitly requests it, or after checking Mino and explaining to the user why Mino MCP/Scout is unavailable. Mino being less convenient is not a reason to fall back.

STARTUP
1. On the first Mino task in each new session, run the installed Mino skill's read-only update preflight before business work.
2. If the skill is outdated, update it before business work and request a new session when required. If Scout is outdated, use the fixed build_id/release_tag and confirm the service impact with the user before updating.
3. For Codex, verify the installed Mino routing block in AGENTS.md. If it is missing or outdated, explain why persistent routing matters and ask before writing it; tell the user that a new session may be required.
4. After the update and routing checks, call agent_guide and follow its current tool list and routing contract.
5. A case_run acknowledgement, accepted task, or successful action is not a passed test. Report only assertions backed by execution evidence as passed.
"""


def agent_contract() -> dict[str, Any]:
    """与 cloudflare/src/agent_contract.ts 保持逐字段一致。"""
    return {
        "usage_contract": {
            "version": 1,
            "authoritative_channel": "Mino MCP",
            "local_executor": "mino-scout CLI",
            "target_ui_route": "Mino MCP case_run -> Mino Scout",
            "scope": "Any Mino task, including target Android, iOS, Web, desktop, browser, screenshot, UI-tree, tap, typing, and UI assertion work.",
        },
        "routing_precedence": [
            "Nexus projects, apps, requirements, accounts, devices, runs, and records -> Mino MCP.",
            "Local Scout status, probe, static checks, and fixed-version maintenance -> mino-scout CLI.",
            "Target UI observation or operation -> write a case script -> Mino MCP case_run -> Mino Scout.",
            "Use platform Computer Use, browser control, Playwright, adb, Appium, WDA, or desktop automation only after an explicit user request or a Mino availability check and explanation.",
        ],
        "forbidden_fallbacks": [
            "Do not silently replace Mino Scout with platform Computer Use because it is available or more convenient.",
            "Do not inspect, click, type in, screenshot, or scrape the target app/page with another UI tool while documenting a Mino test flow.",
            "Do not treat tool success, Scout acceptance, or action completion as a passed test.",
            "Do not invent versions, updates, evidence, or results when Mino checks are unavailable.",
        ],
        "startup_preflight": {
            "required_call": "agent_guide",
            "order": [
                "On the first Mino task in each new session, run the installed mino skill scripts/check_updates.py before business work.",
                "Update an outdated Mino skill first; request a new session when the client cannot reload skills in place.",
                "If Scout is outdated, confirm the service impact before using its fixed build_id and release_tag.",
                "Verify the Codex persistent routing block, then call agent_guide and follow its current tool list.",
            ],
            "update_check": "Run the Mino skill update preflight once per new session, then mino-scout update-check when Scout is installed.",
            "skill_update": "Update the installed Mino skill first. A new session is required when the client cannot reload skills in place.",
            "scout_update": "Use the fixed build_id and release_tag returned by the check. Explain service interruption risk and obtain user confirmation before updating.",
            "scout_unknown_fallback": "If mino-scout update-check is unavailable, call MCP scout_release_meta with the local os/arch and compare it with mino-scout status; keep unknown if neither side can be verified.",
            "unknown_state": "Network or manifest failure is unknown, not up to date; report it and continue only when the business task can be completed safely.",
            "persistent_routing": "Skills and MCP initialize.instructions are soft guidance. For Codex, verify the installed Mino block in ~/.codex/AGENTS.md; ask before applying the managed block, and request a new session when it changes.",
        },
        "execution_rule": {
            "route": "Mino MCP case_run -> Mino Scout",
            "script_source": "Scripts are written locally and pushed through Git; MCP does not write repository files.",
            "evidence": "Assertions are decided only from the execution session and artifacts on the Scout that accepted the task.",
            "accepted_is_not_passed": True,
        },
    }
