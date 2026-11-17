"""Agent investigation records. Pure transitions; never execute or admit cases."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

STAGES = ("analysis", "authoring", "preparation", "verification")
OUTCOMES = ("progress", "blocked", "complete", "failed", "deferred")
VERIFICATION_NOTE = "Agent progress is not a test verdict; verify assertions in the original Scout task."


def text(value: Any, name: str, limit: int = 2000, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value.strip()


def facts(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("Invalid evidence")
    return sorted({text(item, "evidence") for item in value})


def summary(state: dict) -> dict:
    attempts = state["attempts"]
    stages = {stage: {status: 0 for status in ("pending", "in_progress", *OUTCOMES)} for stage in STAGES}
    rows, independent = [], []
    unknown = [{k: a[k] for k in ("unit", "attempt_id", "task_id")}
               for a in attempts if a["write_outcome"] == "unknown" and a["attempt_id"] not in state["resolutions"]]
    for unit in state["units"]:
        progress = {}
        for stage in STAGES:
            history = [a for a in attempts if a["unit"] == unit and a["stage"] == stage]
            latest = history[-1] if history else None
            status = (latest["outcome"] or "in_progress") if latest else "pending"
            progress[stage] = {"status": status, "attempts": len(history), "remaining": max(0, state["stage_budget"] - len(history)),
                               "task_id": latest["task_id"] if latest else "", "blocker": latest["blocker"] if latest else ""}
            stages[stage][status] += 1
        rows.append({"unit": unit, "stages": progress})
        if progress["verification"]["status"] != "complete" and not any(
                p["status"] in {"blocked", "failed", "deferred", "in_progress"} for p in progress.values()):
            independent.append(unit)
    return {"workflow_id": state["workflow_id"], "scope": state["scope"], "stages": stages, "units": rows,
            "attempts_used": len(attempts), "total_remaining": max(0, state["total_budget"] - len(attempts)),
            "unresolved_writes": unknown, "independent_units": [u for u in independent if u not in {a["unit"] for a in unknown}],
            "attempts": deepcopy(attempts), "resolutions": deepcopy(state["resolutions"]), "verification_note": VERIFICATION_NOTE}


def transition(old: dict | None, operation: str, workflow_id: str, args: dict, now: str) -> tuple[dict, dict]:
    workflow_id = text(workflow_id, "workflow_id", 128)
    state = deepcopy(old)
    if operation == "init":
        scope = text(args.get("scope"), "scope")
        units = args.get("units")
        if not isinstance(units, list) or not 1 <= len(units) <= 1000:
            raise ValueError("Invalid units")
        units = sorted({text(u, "unit", 128) for u in units})
        stage_budget, total_budget = args.get("stage_budget", 6), args.get("total_budget", 24)
        if type(stage_budget) is not int or type(total_budget) is not int or not 1 <= stage_budget <= total_budget <= 10000:
            raise ValueError("Invalid budgets")
        if state and (state["scope"], state["stage_budget"], state["total_budget"]) != (scope, stage_budget, total_budget):
            raise ValueError("Existing workflow scope/budgets differ")
        state = state or {"workflow_id": workflow_id, "scope": scope, "stage_budget": stage_budget, "total_budget": total_budget,
                          "units": [], "attempts": [], "resolutions": {}, "created_at": now}
        state["units"] = sorted(set(state["units"]) | set(units))
        return state, summary(state)
    if not state:
        raise ValueError("Unknown workflow")
    if operation == "get":
        return state, summary(state)
    attempt_id = text(args.get("attempt_id"), "attempt_id", 128)
    attempt = next((a for a in state["attempts"] if a["attempt_id"] == attempt_id), None)
    evidence = facts(args.get("evidence", []))
    if operation == "start":
        unit, stage = text(args.get("unit"), "unit", 128), args.get("stage")
        action, blocker = text(args.get("action"), "action", 500), text(args.get("blocker", ""), "blocker", required=False)
        writes = args.get("writes", False)
        if unit not in state["units"] or stage not in STAGES or type(writes) is not bool:
            raise ValueError("Invalid unit, stage or writes")
        if attempt:
            if (attempt["unit"], attempt["stage"], attempt["action"], attempt["writes"]) != (unit, stage, action, writes):
                raise ValueError("Attempt ID already belongs to a different investigation")
            return state, {"allowed": False, "reason": "attempt_exists", "attempt": attempt, "next": "read_existing_attempt"}
        active = next((a for a in state["attempts"] if a["unit"] == unit and not a["outcome"]), None)
        if active:
            return state, {"allowed": False, "reason": "attempt_in_progress", "attempt_id": active["attempt_id"], "next": "read_existing_attempt"}
        unknown = [a for a in summary(state)["unresolved_writes"] if a["unit"] == unit]
        if writes and unknown:
            return state, {"allowed": False, "reason": "write_outcome_unknown", "attempts": unknown, "next": "reconcile_existing_task"}
        used = len(state["attempts"])
        stage_used = sum(a["unit"] == unit and a["stage"] == stage for a in state["attempts"])
        if used >= state["total_budget"] or stage_used >= state["stage_budget"]:
            return state, {"allowed": False, "reason": "budget_exhausted", "used": used, "stage_used": stage_used,
                           "next": "deliver_workflow" if used >= state["total_budget"] else "deliver_phase_and_continue_independent_units"}
        for prior in reversed(state["attempts"]):
            if (prior["unit"] == unit and prior["stage"] == stage and prior["outcome"] in {"blocked", "failed"}
                    and prior["action"] == action and (not blocker or prior["blocker"] == blocker)
                    and not set(evidence) - set(prior["evidence"])):
                return state, {"allowed": False, "reason": "repeated_blocker_without_evidence", "attempt_id": prior["attempt_id"],
                               "next": "change_approach_or_deliver_phase"}
        state["attempts"].append({"attempt_id": attempt_id, "unit": unit, "stage": stage, "action": action, "writes": writes,
                                  "blocker": blocker, "evidence": evidence, "task_id": "", "outcome": "", "write_outcome": "known",
                                  "note": "", "started_at": now, "finished_at": ""})
        return state, {"allowed": True, "attempt_id": attempt_id, "stage_remaining": state["stage_budget"] - stage_used - 1,
                       "total_remaining": state["total_budget"] - used - 1, "next": "perform_declared_investigation"}
    if not attempt:
        raise ValueError("Unknown attempt")
    if operation == "finish":
        outcome, write_outcome = args.get("outcome"), args.get("write_outcome", "known")
        blocker = text(args.get("blocker", ""), "blocker", required=False)
        task_id = text(args.get("task_id", ""), "task_id", 128, required=False)
        note = text(args.get("note", ""), "note", required=False)
        if outcome not in OUTCOMES or write_outcome not in {"known", "unknown"}:
            raise ValueError("Unsupported outcome")
        if outcome in {"blocked", "failed"} and not blocker:
            raise ValueError("Blocked/failed investigation must name its concrete blocker")
        evidence = sorted(set(evidence) | set(attempt["evidence"]))
        if outcome == "complete" and (not evidence or write_outcome == "unknown"):
            raise ValueError("Phase completion requires evidence and a known outcome")
        if write_outcome == "unknown" and (not attempt["writes"] or not task_id):
            raise ValueError("Unknown write outcome requires a writing attempt and its original task ID")
        values = {"outcome": outcome, "write_outcome": write_outcome, "blocker": blocker, "task_id": task_id, "note": note, "evidence": evidence}
        if attempt["outcome"]:
            if any(attempt[k] != v for k, v in values.items()):
                raise ValueError("Finished attempt is immutable")
        else:
            attempt.update(values, finished_at=now)
        return state, deepcopy(attempt)
    if operation == "resolve":
        task_id = text(args.get("task_id"), "task_id", 128)
        if not evidence or attempt["write_outcome"] != "unknown" or task_id != attempt["task_id"]:
            raise ValueError("Reconcile the original task with concrete evidence")
        resolution = state["resolutions"].get(attempt_id)
        if resolution and resolution["evidence"] != evidence:
            raise ValueError("Existing reconciliation is immutable")
        resolution = resolution or {"attempt_id": attempt_id, "task_id": task_id, "evidence": evidence, "resolved_at": now}
        state["resolutions"][attempt_id] = resolution
        return state, deepcopy(resolution)
    raise ValueError("Unsupported operation")
