"""Immutable workflow delivery snapshots; no inferred execution verdicts."""
from copy import deepcopy
from hashlib import sha256
import json


def seal_report(state, now, *, final=False):
    from mino_nexus.contract.agent_workflow import summary
    view = summary(state)
    snapshot = {key: deepcopy(view[key]) for key in (
        "scope", "source", "source_read_id", "expected_units", "coverage", "units",
        "attempts", "help_requests", "contexts", "unresolved_writes", "task_contexts", "draft_actions", "decisions", "findings")}
    digest = sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    reports = state.setdefault("reports", [])
    if not any(r["content_hash"] == digest and r["final"] == final for r in reports):
        reports.append({"report_id": digest[:24] + ("-final" if final else "-stage"),
                        "template_version": 1, "created_at": now, "content_hash": digest,
                        "final": final, "snapshot": snapshot})
    if not final:
        # Only the newest stage snapshot is kept; sealed final deliveries remain immutable.
        state["reports"] = [r for r in reports if r["final"] or r is reports[-1]]
    if final:
        closed = view["coverage"].get("testing_closed") and not view["unresolved_writes"]
        if view["coverage"]["all_verified"] and not view["unresolved_writes"]:
            state["delivery_status"] = "delivered"
        elif closed:
            state["delivery_status"] = "delivered_with_findings"
        else:
            state["delivery_status"] = "delivered_with_gaps"


def update_delivery(state, operation, args, now):
    from mino_nexus.contract.agent_workflow import text, facts, unit_list
    if operation == "draft":
        if state.get("source_read_id"):
            raise ValueError("Source already locked; use scoped investigation stages")
        attempt_id = text(args.get("attempt_id"), "attempt_id", 128)
        action = {"attempt_id": attempt_id, "action": text(args.get("action"), "action", 500),
                  "note": text(args.get("note", ""), "note", required=False)}
        question = args.get("help_request")
        if question is not None:
            # Source questions have no case IDs until the successful source read.
            if not isinstance(question, dict) or question.get("affected_units") != []:
                raise ValueError("Draft help request affected_units must be empty before source lock")
            if set(question) != {"question", "missing", "checked_sources", "affected_units"}:
                raise ValueError("Invalid draft help request")
            action["help_request"] = {"question": text(question["question"], "question"),
                                      "missing": text(question["missing"], "missing"),
                                      "checked_sources": facts(question["checked_sources"]), "affected_units": []}
        actions = state.setdefault("draft_actions", [])
        prior = next((a for a in actions if a["attempt_id"] == attempt_id), None)
        if prior and any(prior.get(k) != v for k, v in action.items()):
            raise ValueError("Draft action is immutable")
        if not prior:
            actions.append({**action, "created_at": now})
        state["delivery_status"] = "in_progress"
    elif operation == "answer":
        attempt_id = text(args.get("attempt_id"), "attempt_id", 128)
        attempt = next((a for a in [*state["attempts"], *state.get("draft_actions", [])] if a["attempt_id"] == attempt_id and a.get("help_request")), None)
        if not attempt:
            raise ValueError("Unknown help request")
        answer = {"answer": text(args.get("answer"), "answer"), "answered_at": now}
        answers = state.setdefault("answers", {})
        if attempt_id in answers:
            if answers[attempt_id]["answer"] != answer["answer"]:
                raise ValueError("Answer is immutable; append a context correction")
        else:
            answers[attempt_id] = answer
            state["delivery_status"] = "in_progress"
    elif operation == "context":
        context_id = text(args.get("context_id"), "context_id", 128)
        category = args.get("category")
        if category not in {"requirement", "configuration", "environment", "data", "hardcoding", "product", "decision"}:
            raise ValueError("Invalid context category")
        affected = unit_list(args.get("affected_units", []), "affected_units", required=False)
        if not set(affected) <= set(state.get("expected_units", [])):
            raise ValueError("Context units outside locked scope")
        context = {"context_id": context_id, "category": category,
                   "content": text(args.get("content"), "content"),
                   "source": text(args.get("reference"), "reference"),
                   "affected_units": affected, "evidence": facts(args.get("evidence", []))}
        contexts = state.setdefault("contexts", [])
        prior = next((c for c in contexts if c["context_id"] == context_id), None)
        if prior and any(prior[k] != v for k, v in context.items()):
            raise ValueError("Context is immutable")
        if not prior:
            state["delivery_status"] = "in_progress"
            contexts.append({**context, "created_at": now, "confirmation": "provided_not_verified"})
    else:
        final = args.get("final", False)
        if type(final) is not bool:
            raise ValueError("Invalid final")
        if final and any(not a["outcome"] for a in state["attempts"]):
            raise ValueError("Reconcile active investigations before final delivery")
        seal_report(state, now, final=final)
