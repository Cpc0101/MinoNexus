"""Mino MCP 对外 agent 路由契约的唯一 Python 真源。"""
from __future__ import annotations

from typing import Any

AGENT_INSTRUCTIONS = """You are working with Mino.

TRIGGER AND SCOPE
- The Agent decides from the user's explicit testing/QA intent whether to start a testing workflow.
- Ordinary product development, code review, deployment, domain/TLS operations, and merely mentioning Mino do NOT trigger testing workflows, source locking, test preflight, business boards, or test receipts.
- Nexus control-plane queries still use MCP. Read-only diagnosis needs no business board or agent_task_id; do not manufacture a test workflow for it.

MANDATORY ROUTING (within explicitly requested testing/QA)
- Nexus control-plane work MUST use Mino MCP tools.
- Local Scout work MUST use the mino-scout CLI.
- Within testing/QA, inspecting or operating target Android, iOS, Web, desktop applications, browser pages, screenshots, accessibility/UI trees, taps, typing, and UI assertions MUST execute on Mino Scout through Mino MCP debug_case/debug_case_execute for local authoring or case_run for formal runs. Remote nodes are for formal runs only.
- Do NOT substitute platform Computer Use, browser control, Playwright, adb, Appium, WDA, or desktop automation for Mino Scout by default.
- Only use another tool when the user explicitly requests it, or after checking Mino and explaining to the user why Mino MCP/Scout is unavailable. Mino being less convenient is not a reason to fall back.
- Mino MCP cannot remove tools that the Codex client already exposes. Do not claim Computer Use is disabled unless the client-side PreToolUse guard or administrator policy is verifiably active.

STARTUP (only for explicitly requested testing/QA)
1. On the first explicitly requested testing/QA task in each new session, run the installed Mino skill's read-only update preflight before business work.
2. If the skill is outdated, update it before business work and request a new session when required. If Scout is outdated, use the fixed build_id/release_tag and confirm the service impact with the user before updating.
3. For Codex, verify the installed Mino routing block in AGENTS.md. If it is missing or outdated, explain why persistent routing matters and ask before writing it; tell the user that a new session may be required.
4. After the update and routing checks, call agent_guide and follow its current tool list and routing contract.
5. For multi-step testing/QA work, start an Agent task using agent_task_start, immediately display its startup_message and board_url in the visible conversation before business actions (if empty, report unconfigured entry; never invent a URL); pass its task_id as agent_task_id on subsequent Mino tools. After visibly sending the startup message, declare startup_delivered=true with the exact board_url in agent_task_event.summary. Guarded testing/workflow calls reject missing bindings or announcements; the declaration is not proof of user reading. Report phase, business summary, next_action, scope and environment for local writing/analysis, plus real heartbeats. Read and acknowledge user answers. Nexus cannot observe or wake an external client that has not reported activity.
6. A case_run acknowledgement, accepted task, or successful action is not a passed test. Report only assertions backed by execution evidence as passed.

VISIBLE RECEIPTS (testing workflows only)
- For every case receipt, emit its complete multi-line text, including execution identity, environment, expected versus observed outcome, evidence availability, reason and next action. Unknown fields must remain unknown. Deduplicate by receipt_id, not case_id; preserve retries and channel/device differences. Local assertions passing do not change a SKIPPED/BLOCKED/UNVERIFIED whole-case verdict. Failure root cause is unconfirmed unless backed by evidence.
- Before ending any testing workflow (including failure, blockers, cancellation and budget exhaustion), reconcile active investigations, call agent_workflow_report(final=true), and visibly deliver the report ID, scope/results, evidence, questions and remaining work. Ending an external Agent task linked to a workflow is rejected until its final report is sealed; a sealed report is not proof of client display.
- Announce a batch when it starts and after each batch completes. A long silent run is a contract violation even while Scout is still executing.
- Any question, missing OTP/account/environment/config, or need to create data must appear in the conversation as `HELP` with what you checked and what you need from the user. Waiting silently is a failure.
- agent_task_event / agent_task_question records Nexus-side activity; they do not replace the visible conversation line.

DATA AND ACCOUNTS
- Never invent accounts, emails, OTPs, phone numbers, or business data. Take accounts only from resource.account_* results; if no suitable account exists, ask the user before creating one.
- Treat new-account 0->1 onboarding and existing-account history as separate test scopes with separate evidence; do not reuse one scope's objects or verdicts for the other.
- If MCP or Feishu CLI cannot return a value, record the exact failure, read the requirement/technical design, then ask the user. Do not fill gaps with plausible values.
- Feishu alerts are observations, not test evidence. Never attribute an alert to your run or dismiss it without source-backed evidence.
"""


def agent_contract() -> dict[str, Any]:
    """Agent 工具契约的单一字段定义。"""
    return {
        "usage_contract": {
            "version": 1,
            "authoritative_channel": "Mino MCP",
            "local_executor": "mino-scout CLI",
            "target_ui_route": "Mino MCP debug_case/debug_case_execute (local authoring) or case_run (formal) -> Mino Scout",
            "scope": "Explicitly requested testing/QA only; ordinary product development, code review, deployment, domain/TLS operations, and merely mentioning Mino do not trigger a testing workflow.",
        },
        "workflow_trigger": {
            "decision_owner": "agent",
            "requires": "explicit user testing/QA intent",
            "excluded": ["ordinary product development", "code review", "deployment", "domain/TLS operations", "merely mentioning Mino"],
            "readonly_diagnosis_requires_board": False,
        },
        "routing_precedence": [
            "Nexus control-plane projects, apps, requirements, accounts, devices, runs, and records -> Mino MCP; read-only diagnosis needs no business board.",
            "Local Scout status, probe, static checks, and fixed-version maintenance -> mino-scout CLI.",
            "Within explicit testing/QA, target UI observation or operation -> write a case script -> Mino MCP debug_case/debug_case_execute (local authoring) or case_run (formal) -> Mino Scout.",
            "Use platform Computer Use, browser control, Playwright, adb, Appium, WDA, or desktop automation only after an explicit user request or a Mino availability check and explanation.",
        ],
        "forbidden_fallbacks": [
            "Do not silently replace Mino Scout with platform Computer Use because it is available or more convenient.",
            "Do not inspect, click, type in, screenshot, or scrape the target app/page with another UI tool while documenting a Mino test flow.",
            "Do not treat tool success, Scout acceptance, or action completion as a passed test.",
            "Do not invent versions, updates, evidence, or results when Mino checks are unavailable.",
        ],
        "enforcement": {
            "soft_layers": ["Codex AGENTS.md", "Mino Skill", "MCP initialize.instructions", "agent_guide"],
            "client_guard": "Codex PreToolUse guard can deny known local UI tools, but hook failures and special or hosted tool paths may bypass it.",
            "hard_boundary": "Only administrator-managed Codex requirements.toml can remove Computer Use/Browser capabilities from the client.",
            "verdict_boundary": "Mino receives no tool trace for calls that never reach Scout. A passed result therefore requires the original Scout task_id and assertion evidence.",
        },
        "startup_preflight": {
            "required_call": "agent_guide",
            "applies_when": "explicit user testing/QA intent; not ordinary development or read-only diagnosis",
            "order": [
                "On the first explicitly requested testing/QA task in each new session, run the installed mino skill scripts/check_updates.py before business work.",
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
        "source_binding": {
            "authority": "Agent workflow source and expected_units are derived only from a successful CaseSourceRead receipt owned by the current user.",
            "order": ["case.source.list", "case.source.read", "agent.workflow.init"],
            "init_input": "Pass the successful read_id as source_read_id. Nexus re-reads the canonical receipt, rejects any explicitly supplied source/expected_units that differ, and then locks the canonical snapshot.",
            "stale_or_missing": "A missing, failed, superseded, incomplete, hash-mismatched, row-mismatched, or cross-user receipt cannot initialize a workflow.",
        },
        "execution_rule": {
            "route": "Mino MCP debug_case/debug_case_execute (local authoring) or case_run (formal) -> Mino Scout",
            "script_source": "Debug sessions execute the local authoring workspace without checkout; formal runs use a fixed Git revision in a separate task workspace. MCP submits fragments but never edits repository files.",
            "evidence": "Assertions are decided only from the execution session and artifacts on the Scout that accepted the task.",
            "accepted_is_not_passed": True,
        },
    }
