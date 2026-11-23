"""Agent investigation records. Pure transitions; never execute or admit cases."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

STAGES = ("analysis", "authoring", "preparation", "verification")
OUTCOMES = ("progress", "blocked", "complete", "failed", "deferred")
VERIFICATION_NOTE = "Agent progress is not a test verdict; verify assertions in the original Scout task."
DONE_STATUSES = {"implemented", "path_validated", "formal_verified"}
TERMINAL_STATUSES = {"blocked", "failed", "deferred"}
SOURCE_FIELDS = {"source_id", "kind", "url", "reference", "spreadsheet_token", "sheet_id", "range", "revision", "content_hash"}


def text(value: Any, name: str, limit: int = 2000, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value.strip()


def facts(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("Invalid evidence")
    return sorted({text(item, "evidence") for item in value})


def unit_list(value: Any, name: str, required: bool = True) -> list[str]:
    if not isinstance(value, list) or len(value) > 1000 or (required and not value):
        raise ValueError(f"Invalid {name}")
    units = [text(item, name, 128) for item in value]
    if len(units) != len(set(units)):
        raise ValueError(f"Duplicate {name}")
    return sorted(units)


def workflow_source(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value or set(value) - SOURCE_FIELDS:
        raise ValueError("Invalid source")
    source = {}
    for key, raw in value.items():
        if key == "revision" and type(raw) is int:
            source[key] = str(raw)
        elif isinstance(raw, str):
            source[key] = text(raw, f"source.{key}", 2000)
        else:
            raise ValueError("Invalid source")
    if not source.get("kind") or not (source.get("url") or source.get("reference")):
        raise ValueError("Source requires kind and url/reference")
    return source


def _latest(attempts: list[dict], unit: str, stage: str) -> dict | None:
    history = [a for a in attempts if a["unit"] == unit and a["stage"] == stage]
    return history[-1] if history else None


def _unit_state(attempts: list[dict], unit: str) -> dict:
    unit_attempts = [(index, attempt) for index, attempt in enumerate(attempts) if attempt["unit"] == unit]
    if not unit_attempts:
        return {"status": "pending", "implemented": False, "path_validated": False, "formal_verified": False, "terminal": False}
    _, latest = unit_attempts[-1]
    authoring_done = max((index for index, attempt in unit_attempts
                          if attempt["stage"] == "authoring" and attempt["outcome"] == "complete"), default=-1)
    implemented = authoring_done >= 0
    verification = next((attempt for index, attempt in reversed(unit_attempts)
                         if attempt["stage"] == "verification"), None)
    verification_index = next((index for index, attempt in reversed(unit_attempts)
                               if attempt["stage"] == "verification"), -1)
    formal_verified = bool(
        implemented
        and verification
        and verification_index > authoring_done
        and verification["outcome"] == "complete"
        and verification["task_id"]
    )
    preparation_index = next((index for index, attempt in reversed(unit_attempts)
                              if attempt["stage"] == "preparation" and attempt["outcome"] == "complete"), -1)
    validation_index = max(
        preparation_index,
        verification_index if verification and verification["outcome"] == "complete" else -1,
    )
    path_validated = bool(implemented and validation_index > authoring_done)
    terminal = latest["outcome"] in TERMINAL_STATUSES
    if terminal:
        status = latest["outcome"]
    elif formal_verified:
        status = "formal_verified"
    elif path_validated:
        status = "path_validated"
    elif implemented:
        status = "implemented"
    else:
        status = "in_progress"
    return {"status": status, "implemented": implemented, "path_validated": path_validated,
            "formal_verified": formal_verified, "terminal": terminal}


def _unit_status(attempts: list[dict], unit: str) -> str:
    return _unit_state(attempts, unit)["status"]


def coverage(state: dict) -> dict:
    expected = state["expected_units"] if "expected_units" in state else state["units"]
    declared = set(state["units"])
    states = {unit: _unit_state(state["attempts"], unit) for unit in expected}
    missing = [unit for unit in expected if not states[unit]["implemented"] and not states[unit]["terminal"]]
    verification_missing = [unit for unit in expected
                            if not states[unit]["formal_verified"] and not states[unit]["terminal"]]
    blocked = [unit for unit in expected if states[unit]["terminal"]]
    implementation_complete = not missing
    verification_complete = not verification_missing
    next_units = missing[:10] if missing else verification_missing[:10]
    implemented = sum(states[unit]["implemented"] for unit in expected)
    formal_verified = sum(states[unit]["formal_verified"] for unit in expected)
    return {
        "expected": len(expected),
        "declared": sum(unit in declared for unit in expected),
        "implemented": implemented,
        "path_validated": sum(states[unit]["path_validated"] for unit in expected),
        "formal_verified": formal_verified,
        "blocked": len(blocked),
        "missing": missing,
        "verification_missing": verification_missing,
        "blocked_units": blocked,
        "implementation_complete": implementation_complete,
        "verification_complete": verification_complete,
        "all_implemented": implemented == len(expected),
        "all_verified": formal_verified == len(expected) and not blocked,
        "complete": implementation_complete,
        "accounted": not missing,
        "next_units": next_units,
    }


def summary(state: dict) -> dict:
    attempts = state["attempts"]
    stages = {stage: {status: 0 for status in ("pending", "in_progress", *OUTCOMES)} for stage in STAGES}
    rows, independent = [], []
    expected = state["expected_units"] if "expected_units" in state else state["units"]
    declared = set(state["units"])
    unknown = [{k: a[k] for k in ("unit", "attempt_id", "task_id")}
               for a in attempts if a["write_outcome"] == "unknown" and a["attempt_id"] not in state["resolutions"]]
    for unit in expected:
        progress = {}
        for stage in STAGES:
            history = [a for a in attempts if a["unit"] == unit and a["stage"] == stage]
            latest = history[-1] if history else None
            status = (latest["outcome"] or "in_progress") if latest else "pending"
            progress[stage] = {"status": status, "attempts": len(history), "remaining": max(0, state["stage_budget"] - len(history)),
                               "task_id": latest["task_id"] if latest else "", "blocker": latest["blocker"] if latest else ""}
            stages[stage][status] += 1
        rows.append({"unit": unit, "declared": unit in declared, "status": _unit_status(attempts, unit), "stages": progress})
        if progress["verification"]["status"] != "complete" and not any(
                p["status"] in {"blocked", "failed", "deferred", "in_progress"} for p in progress.values()):
            independent.append(unit)
    return {"workflow_id": state["workflow_id"], "scope": state["scope"], "source": state.get("source"),
            "source_read_id": state.get("source_read_id", ""),
            "expected_units": expected, "coverage": coverage(state), "stages": stages, "units": rows,
            "attempts_used": len(attempts), "total_remaining": max(0, state["total_budget"] - len(attempts)),
            "unresolved_writes": unknown, "independent_units": [u for u in independent if u not in {a["unit"] for a in unknown}],
            "attempts": deepcopy(attempts), "resolutions": deepcopy(state["resolutions"]), "verification_note": VERIFICATION_NOTE}


def transition(old: dict | None, operation: str, workflow_id: str, args: dict, now: str) -> tuple[dict, dict]:
    workflow_id = text(workflow_id, "workflow_id", 128)
    state = deepcopy(old)
    if operation == "init":
        scope = text(args.get("scope"), "scope")
        expected_units = unit_list(
            args.get("expected_units"), "expected_units", required=False
        )
        # expected_units is the locked source-of-truth scope. Unless the caller
        # explicitly narrows this first batch, every expected unit is declared
        # visible immediately instead of disappearing behind a small files list.
        units = (
            expected_units
            if args.get("units") is None
            else unit_list(args.get("units"), "units", required=False)
        )
        if not set(units) <= set(expected_units):
            raise ValueError("units must be a subset of expected_units")
        source = workflow_source(args.get("source"))
        if source is None:
            raise ValueError("Source is required")
        source_read_id = text(args.get("source_read_id", ""), "source_read_id", 128, required=False)
        if not source_read_id and not state:
            raise ValueError("source_read_id is required")
        minimum_total = len(expected_units) * len(STAGES)
        if state:
            if state["scope"] != scope:
                raise ValueError("Existing workflow scope differs")
            if "expected_units" in state:
                if state["expected_units"] != expected_units:
                    raise ValueError("Existing workflow expected_units differ")
            elif not set(state["units"]) <= set(expected_units):
                raise ValueError("expected_units must contain existing units")
            if state.get("source") and source and state["source"] != source:
                raise ValueError("Existing workflow source differs")
            existing_read_id = str(state.get("source_read_id") or "")
            if existing_read_id and source_read_id and existing_read_id != source_read_id:
                raise ValueError("Existing workflow source_read_id differs")
            if "stage_budget" in args and args["stage_budget"] != state["stage_budget"]:
                raise ValueError("Existing workflow stage_budget differs")
            if "total_budget" in args and args["total_budget"] != state["total_budget"]:
                raise ValueError("Existing workflow total_budget differs")
            if state["total_budget"] < minimum_total:
                raise ValueError("Existing workflow total_budget is below expected_units minimum")
            state["expected_units"] = expected_units
            state["source"] = state.get("source") or source
            state["source_read_id"] = existing_read_id or source_read_id
        else:
            stage_budget = args.get("stage_budget", 6)
            total_budget = args.get("total_budget", max(24, minimum_total))
            if (type(stage_budget) is not int or type(total_budget) is not int
                    or not 1 <= stage_budget <= total_budget <= 10000 or total_budget < minimum_total):
                raise ValueError("Invalid budgets")
            state = {"workflow_id": workflow_id, "scope": scope, "source_read_id": source_read_id,
                     "expected_units": expected_units, "source": source,
                     "stage_budget": stage_budget, "total_budget": total_budget, "units": [], "attempts": [],
                     "resolutions": {}, "created_at": now}
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
        expected = state["expected_units"] if "expected_units" in state else state["units"]
        if unit not in expected or stage not in STAGES or type(writes) is not bool:
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
                           "coverage": coverage(state),
                           "next": "report_coverage_gap_not_complete" if used >= state["total_budget"] else "deliver_phase_and_continue_independent_units"}
        for prior in reversed(state["attempts"]):
            if (prior["unit"] == unit and prior["stage"] == stage and prior["outcome"] in {"blocked", "failed"}
                    and prior["action"] == action and (not blocker or prior["blocker"] == blocker)
                    and not set(evidence) - set(prior["evidence"])):
                return state, {"allowed": False, "reason": "repeated_blocker_without_evidence", "attempt_id": prior["attempt_id"],
                               "next": "change_approach_or_deliver_phase"}
        # A small first batch is only a delivery hint. The locked expected_units
        # remains authoritative, so later batches can start without a second init.
        state["units"] = sorted(set(state["units"]) | {unit})
        state["attempts"].append({"attempt_id": attempt_id, "unit": unit, "stage": stage, "action": action, "writes": writes,
                                  "blocker": blocker, "evidence": evidence, "task_id": "", "outcome": "", "write_outcome": "known",
                                  "note": "", "started_at": now, "finished_at": ""})
        return state, {"allowed": True, "attempt_id": attempt_id, "stage_remaining": state["stage_budget"] - stage_used - 1,
                       "total_remaining": state["total_budget"] - used - 1, "coverage": coverage(state),
                       "next": "perform_declared_investigation"}
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
        return state, {**deepcopy(attempt), "coverage": coverage(state)}
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
