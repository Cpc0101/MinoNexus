"""Pure execution selection for case-library tag filters."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from mino_nexus.casework.case_store import normalize_case_tags


def select_cases_by_tags(
    cases: Iterable[dict[str, Any]],
    *,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    start_index: int = 0,
    source_revision: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply include-any/exclude-any filters, then freeze the resulting plan."""
    candidates = [case for case in cases if isinstance(case, dict)]
    includes = normalize_case_tags(include_tags)
    excludes = normalize_case_tags(exclude_tags)
    include_keys = {tag.casefold() for tag in includes}
    exclude_keys = {tag.casefold() for tag in excludes}
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for case in candidates:
        case_id = str(case.get("case_id") or "").strip()
        if case_id and case_id in seen_ids:
            continue
        if case_id:
            seen_ids.add(case_id)
        tags = {tag.casefold() for tag in normalize_case_tags(case.get("tags"))}
        reason = ""
        if exclude_keys and tags & exclude_keys:
            reason = "tag_excluded"
        elif include_keys and not tags & include_keys:
            reason = "tag_not_included"
        if reason:
            skipped.append({"case_id": case_id, "reason": reason})
            continue
        item = deepcopy(case)
        if case_id:
            item["case_id"] = case_id
        selected.append(item)

    matched_count = len(selected)
    offset = max(0, int(start_index or 0))
    final_cases = selected[offset:]
    selected_case_ids = [str(case.get("case_id") or "") for case in final_cases if case.get("case_id")]
    selection = {
        "filters": {
            "include_tags": includes,
            "exclude_tags": excludes,
        },
        "matched_count": matched_count,
        "selected_case_ids": selected_case_ids,
        "skipped": skipped,
        "source_revision": str(source_revision or "").strip(),
        "selected_case_revisions": {
            str(case.get("case_id")): str(case.get("source_revision_id") or "")
            for case in final_cases
            if case.get("case_id") and case.get("source_revision_id")
        },
    }
    return final_cases, selection


def tags_requested(include_tags: list[str] | None, exclude_tags: list[str] | None) -> bool:
    return bool(normalize_case_tags(include_tags) or normalize_case_tags(exclude_tags))
