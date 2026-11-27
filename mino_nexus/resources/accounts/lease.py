"""测试账号租用：Requirement DSL 选号、run 级租约、TTL、幂等释放。"""
from __future__ import annotations

import copy
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.resources import project_store as ps
from mino_nexus.resources.project_env import (
    account_ident,
    account_label,
    list_test_accounts,
    pick_test_accounts,
    resolve_surface_id,
    save_test_accounts,
)
from mino_nexus.resources.resource_pool import (
    DEFAULT_LEASE_TTL_SEC,
    account_facets,
    compile_requirements_from_case_need,
    format_facets_brief,
    lease_expired,
    new_lease_record,
)

TAG = "AccountLease"
DEFAULT_ACCOUNT_SLOT = "default"


def normalize_account_slot(value: Any) -> str:
    """Lenient internal normalization; public API validation is strict."""
    slot = str(value or "").strip()
    return slot[:80] if slot else DEFAULT_ACCOUNT_SLOT


def validate_account_slot(value: Any) -> str:
    """Validate the idempotency key used to address one leased identity."""
    if value is None:
        return DEFAULT_ACCOUNT_SLOT
    if type(value) is not str:
        raise ValueError("account_slot_invalid")
    slot = value.strip()
    if not slot:
        return DEFAULT_ACCOUNT_SLOT
    if len(slot) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in slot):
        raise ValueError("account_slot_invalid")
    return slot


def _account_id(row: dict[str, Any] | None) -> str:
    value = row if isinstance(row, dict) else {}
    return str(value.get("id") or value.get("account_id") or "").strip()


def leased_account_entries(ctx: Any) -> list[dict[str, Any]]:
    """Return every leased account in this execution unit, without mutating ctx."""
    rows = getattr(ctx, "leased_accounts", None)
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def leased_account_for_slot(ctx: Any, slot: Any = "") -> dict[str, Any] | None:
    wanted = normalize_account_slot(slot)
    for row in leased_account_entries(ctx):
        if normalize_account_slot(row.get("slot")) == wanted:
            return row
    return None


def _excluded_accounts_for_slot(ctx: Any, slot: Any) -> set[str]:
    wanted = normalize_account_slot(slot)
    return {
        account_id
        for row in leased_account_entries(ctx)
        if (account_id := _account_id(row))
        and normalize_account_slot(row.get("slot")) != wanted
    }


def _seed_legacy_account_views(ctx: Any) -> None:
    """Adopt the old single-account fields before adding a second slot."""
    accounts = getattr(ctx, "leased_accounts", None)
    leases = getattr(ctx, "leased_account_leases", None)
    if (isinstance(accounts, list) and accounts) or (isinstance(leases, list) and leases):
        return
    picked = dict(getattr(ctx, "picked_account", None) or {})
    lease = dict(getattr(ctx, "resource_lease", None) or {})
    account_id = _account_id(picked) or _account_id(lease)
    if not account_id:
        return
    picked.setdefault("id", account_id)
    picked.setdefault("account_id", account_id)
    picked["slot"] = DEFAULT_ACCOUNT_SLOT
    picked.setdefault("app_id", str(lease.get("app_id") or getattr(ctx, "app_id", "") or ""))
    picked.setdefault("platform", str(lease.get("platform") or getattr(ctx, "platform", "") or ""))
    picked.setdefault("target_package", str(lease.get("target_package") or ""))
    picked.setdefault("env_surface", str(lease.get("env_surface") or ""))
    lease.setdefault("account_id", account_id)
    lease.setdefault("project_id", str(getattr(ctx, "project_id", "") or ""))
    lease.setdefault("run_id", str(getattr(ctx, "run_id", "") or ""))
    lease.setdefault("case_id", str(getattr(ctx, "case_id", "") or ""))
    lease.setdefault("sn", str(getattr(ctx, "sn", "") or ""))
    lease["slot"] = DEFAULT_ACCOUNT_SLOT
    lease.setdefault("app_id", str(picked.get("app_id") or getattr(ctx, "app_id", "") or ""))
    lease.setdefault("platform", str(picked.get("platform") or getattr(ctx, "platform", "") or ""))
    ctx.leased_accounts = [picked]
    ctx.leased_account_leases = [lease]
    _rebuild_account_views(ctx)


def _rebuild_account_views(ctx: Any) -> None:
    """Keep one compatibility view and a slot-addressed aggregate in sync."""
    account_rows: list[dict[str, Any]] = []
    lease_rows: list[dict[str, Any]] = []
    for row in getattr(ctx, "leased_accounts", None) or []:
        if isinstance(row, dict) and _account_id(row):
            account_rows.append(dict(row))
    for row in getattr(ctx, "leased_account_leases", None) or []:
        if isinstance(row, dict) and _account_id(row):
            lease_rows.append(dict(row))

    # Same slot is an idempotency key; later data replaces earlier data. Account
    # ids remain unique across slots because allocation excludes other slots.
    by_slot: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        by_slot[normalize_account_slot(row.get("slot"))] = row
    lease_by_slot: dict[str, dict[str, Any]] = {}
    for row in lease_rows:
        lease_by_slot[normalize_account_slot(row.get("slot"))] = row

    account_rows = list(by_slot.values())
    lease_rows = list(lease_by_slot.values())
    ctx.leased_accounts = account_rows
    ctx.leased_account_leases = lease_rows
    ctx.leased_account_by_slot = {
        slot: _account_id(row) for slot, row in by_slot.items() if _account_id(row)
    }

    selected = by_slot.get(DEFAULT_ACCOUNT_SLOT) or (account_rows[-1] if account_rows else {})
    selected_lease = lease_by_slot.get(DEFAULT_ACCOUNT_SLOT) or (lease_rows[-1] if lease_rows else {})
    ctx.picked_account = dict(selected or {})
    ctx.resource_lease = dict(selected_lease or {})
    briefs = [format_accounts_brief(row) for row in account_rows]
    ctx.accounts_brief = "；".join(dict.fromkeys(text for text in briefs if text))
    resource_env = dict(getattr(ctx, "resource_env", None) or {})
    resource_env["accounts"] = account_rows
    if selected:
        resource_env["project_id"] = str(selected_lease.get("project_id") or resource_env.get("project_id") or "")
        resource_env["account_id"] = _account_id(selected)
    ctx.resource_env = resource_env



def _lease_ttl_sec() -> int:
    raw = str(os.environ.get("MINO_RESOURCE_LEASE_TTL_SEC") or "").strip()
    if raw.isdigit():
        return max(60, int(raw))
    return DEFAULT_LEASE_TTL_SEC


def _requirements_from_ctx(
    ctx: Any,
    params: dict[str, Any],
    *,
    need_facets: dict[str, Any] | None,
    ai_reasoning: str = "",
    env_doc: dict | None = None,
) -> dict[str, Any]:
    from mino_nexus.resources.accounts.requirement_compile import augment_requirements_from_precondition
    from mino_nexus.resources.accounts.pool_templates import (
        augment_requirements_with_template,
        merge_need_requirements,
    )

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    param_tid = str(
        params.get("template_id") or params.get("account_template_id") or ""
    ).strip()
    need = dict(need_facets) if isinstance(need_facets, dict) else {}
    if need.get("skip_prose"):
        req = dict(need.get("requirements") or {})
        if param_tid:
            req = augment_requirements_with_template(req, param_tid, env_doc)
        return req
    if param_tid:
        need["template_id"] = param_tid
    if isinstance(params.get("requirements"), dict):
        need["requirements"] = merge_need_requirements(
            need.get("requirements") if isinstance(need.get("requirements"), dict) else {},
            params.get("requirements"),
        )

    if need.get("need_account") or need.get("template_id") or need.get("prompt") or need.get("session"):
        req = compile_requirements_from_case_need(need, env=env_profile, env_doc=env_doc)
        if param_tid:
            req = augment_requirements_with_template(req, param_tid, env_doc)
        from mino_nexus.resources.accounts.requirement_compile import augment_requirements_from_precondition

        return augment_requirements_from_precondition(
            req, str(need.get("prompt") or ""), env_doc
        )

    pre = str(params.get("precondition") or ai_reasoning or "").strip()
    from mino_nexus.resources.resource_pool import compile_requirements_from_text

    req = compile_requirements_from_text(pre, env=env_profile)
    tid = param_tid or str(need.get("template_id") or "").strip()
    if tid:
        req = augment_requirements_with_template(req, tid, env_doc)
    from mino_nexus.resources.accounts.requirement_compile import augment_requirements_from_precondition

    return augment_requirements_from_precondition(req, pre, env_doc)


def format_accounts_brief(row: dict[str, Any]) -> str:
    if not row:
        return ""
    ident = account_ident(row) or "未填号码"
    facets = account_facets(row)
    facet_text = format_facets_brief(facets)
    env = str(row.get("env") or "").strip() or "-"
    reason = str(row.get("reason") or "").strip()
    note = str(row.get("note") or "").strip()
    bits = [f"已租测试账号 {ident}", f"环境 {env}", f"状态 {facet_text}"]
    if note:
        bits.append(f"备注 {note[:80]}")
    if reason:
        bits.append(reason)
    return "；".join(bits)


def _effective_secrets_for_ctx(
    ctx: Any,
    env_doc: dict[str, Any],
    *,
    prompt: str = "",
) -> dict[str, Any]:
    from mino_nexus.resources.otp_resolve import resolve_effective_secrets

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip() or "test"
    surface = str(getattr(ctx, "env_surface", "") or "").strip()
    if not surface:
        surface = resolve_surface_id(
            env_doc,
            platform=str(getattr(ctx, "platform", "") or "android"),
            target_id=str(getattr(ctx, "target_package", "") or ""),
            prompt=str(prompt or "").strip(),
            env_profile=env_profile,
        )
    return resolve_effective_secrets(
        env_doc,
        env_profile=env_profile,
        env_surface=surface,
        app_id=str(getattr(ctx, "app_id", "") or ""),
    )


def _login_kind_for_ctx(ctx: Any, env_doc: dict[str, Any], *, prompt: str = "") -> str:
    from mino_nexus.resources.otp_resolve import login_kind_from_secrets

    return login_kind_from_secrets(_effective_secrets_for_ctx(ctx, env_doc, prompt=prompt))


def _login_mode_for_ctx(ctx: Any, env_doc: dict[str, Any], *, prompt: str = "") -> str:
    from mino_nexus.resources.otp_resolve import login_mode_from_secrets

    return login_mode_from_secrets(_effective_secrets_for_ctx(ctx, env_doc, prompt=prompt))


def _row_matches_ctx_login_kind(
    row: dict[str, Any] | None,
    ctx: Any,
    env_doc: dict[str, Any],
    *,
    prompt: str = "",
) -> bool:
    from mino_nexus.resources.otp_resolve import account_row_matches_login_kind

    from mino_nexus.resources.accounts.app_scope import account_matches_app
    if not account_matches_app(row, str(getattr(ctx, "app_id", "") or "")):
        return False
    kind = _login_kind_for_ctx(ctx, env_doc, prompt=prompt)
    return account_row_matches_login_kind(row, kind)


def _lease_failure_base(env_doc: dict[str, Any], ctx: Any | None, *, prompt: str = "") -> str:
    if ctx is not None:
        mode = _login_mode_for_ctx(ctx, env_doc, prompt=prompt)
        if mode == "pool":
            return "当前应用登录策略为 pool：不会自动补号；号池中没有满足 Requirement 的可用账号，请人工添加或释放账号"
        if mode == "hitl":
            return "当前应用登录策略为 hitl：不会自动补号；请人工提供或处理账号后重试"
    return "没有满足 Requirement 的可用账号（可能都被占用或状态不符）"


def _lease_unavailable_hint(
    env_doc: dict[str, Any],
    *,
    project_id: str,
    requirements: dict[str, Any],
    run_id: str,
    ctx: Any | None = None,
    prompt: str = "",
) -> str:
    """租号失败时附一句可操作的池内统计（不改变选号逻辑）。"""
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.resources.resource_pool import account_facet_values_for_match, match_requirements

    defs = {
        str(d.get("key") or ""): d
        for d in merged_pool_field_defs(env_doc)
        if str(d.get("key") or "")
    }
    rid = str(run_id or "").strip()
    match_n = 0
    busy = 0
    locked_n = 0
    for row in list_test_accounts(env_doc, project_id=project_id):
        facets = account_facet_values_for_match(row)
        ok, _, _ = match_requirements(facets, requirements, field_defs=defs)
        if not ok:
            continue
        match_n += 1
        if bool(row.get("locked")):
            locked_n += 1
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        other = str(lease.get("run_id") or "").strip()
        if other and other != rid:
            busy += 1
    if match_n == 0:
        return "；号池内 0 个账号满足当前 facet 硬约束（检查形象/环境标注或补号）"
    free = match_n - busy - locked_n
    if free <= 0:
        return f"；满足约束 {match_n} 个，可用 {free}（占用 {busy}，锁定 {locked_n}）"
    if ctx is not None:
        from mino_nexus.resources.otp_resolve import account_row_matches_login_kind

        kind = _login_kind_for_ctx(ctx, env_doc, prompt=prompt)
        if kind == "email":
            email_ok = 0
            email_free = 0
            rid = str(run_id or "").strip()
            for row in list_test_accounts(env_doc, project_id=project_id):
                facets = account_facet_values_for_match(row)
                ok, _, _ = match_requirements(facets, requirements, field_defs=defs)
                if not ok or not account_row_matches_login_kind(row, "email"):
                    continue
                email_ok += 1
                lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
                other = str(lease.get("run_id") or "").strip()
                if bool(row.get("locked")) or (other and other != rid):
                    continue
                email_free += 1
            if email_ok == 0:
                return "；环境 login.kind=email，但号池内无填写 email 的账号（勿用手机号账号顶替）"
            if email_free == 0:
                return f"；有 {email_ok} 个邮箱账号满足 facet，但均被占用或锁定"
    return ""


def _purge_expired_leases(project_id: str) -> None:
    """TTL expiry never proves stopped effects. Run finalization/recovery releases leases."""
    return


def _pick_row(
    env_doc: dict[str, Any],
    *,
    project_id: str = "",
    app_id: str = "",
    requirements: dict[str, Any],
    env_profile: str,
    platform: str,
    target_id: str,
    run_id: str,
    surface: str = "",
    holder_sn: str = "",
    prompt: str = "",
    observed_by_account: dict[str, dict[str, str]] | None = None,
    excluded_account_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    surface = resolve_surface_id(
        env_doc,
        surface=surface,
        platform=platform,
        target_id=target_id,
        prompt=prompt,
        env_profile=env_profile,
        app_id=app_id,
    )
    ranked = pick_test_accounts(
        list_test_accounts(env_doc, project_id=project_id),
        app_id=app_id,
        prompt=prompt,
        env=env_profile,
        surface=surface,
        channels=env_doc.get("channels") or [],
        platform=platform,
        target_id=target_id,
        env_doc=env_doc,
        requirements=requirements,
        run_id=run_id,
        holder_sn=holder_sn,
        observed_by_account=observed_by_account,
        excluded_account_ids=excluded_account_ids,
    )
    from mino_nexus.resources.otp_resolve import (
        filter_accounts_for_login_kind,
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    secrets = resolve_effective_secrets(
        env_doc,
        env_profile=env_profile,
        env_surface=surface,
        app_id=app_id,
    )
    kind = login_kind_from_secrets(secrets)
    matched_kind = filter_accounts_for_login_kind(ranked, kind=kind)
    # 登录身份是硬约束：没有匹配账号时进入自动补池，不能回退租另一种身份。
    ranked = matched_kind
    from mino_nexus.resources.account_data_match import rank_account_data
    ranked = rank_account_data(project_id, ranked, requirements.get("data_requirements"), app_id=app_id)
    for row in ranked:
        if str(row.get("id") or "").strip() in (excluded_account_ids or set()):
            continue
        if bool(row.get("locked")):
            continue
        aid = str(row.get("id") or "")
        observed = observed_by_account.get(aid) if isinstance(observed_by_account, dict) else None
        if not row_satisfies_requirements(row, requirements, env_doc, observed=observed):
            continue
        return dict(row)
    return None


def _mark_leased(
    project_id: str,
    account_id: str,
    run_id: str,
    *,
    case_id: str = "",
    sn: str = "",
    app_id: str = "",
    env: str = "",
    requirements: dict[str, Any] | None = None,
    ttl_sec: int | None = None,
    slot: str = "",
    platform: str = "",
    target_package: str = "",
    env_surface: str = "",
    session_id: str = "",
) -> bool:
    from mino_nexus.resources.pool_account_store import try_claim_account_lease
    if app_id:
        from mino_nexus.resources.accounts.app_scope import validate_project_apps
        validate_project_apps(project_id, [app_id])

    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next((r for r in rows if str(r.get("id") or "") == account_id), None)
    existing = row.get("lease") if isinstance(row, dict) and isinstance(row.get("lease"), dict) else {}
    prev_run = str(existing.get("run_id") or "").strip()
    prev_case = str(existing.get("case_id") or "").strip()
    cid = str(case_id or "").strip()

    rec = new_lease_record(
        run_id,
        ttl_sec=max(60, int(ttl_sec)) if ttl_sec else _lease_ttl_sec(),
        case_id=cid,
        sn=str(sn or "")[:64],
        slot=normalize_account_slot(slot),
        app_id=app_id,
        platform=platform,
        target_package=target_package,
        env_surface=env_surface,
    )
    if session_id:
        rec["session_id"] = session_id
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    defs = {str(d["key"]): d for d in merged_pool_field_defs(doc) if d.get("key")}
    ok = try_claim_account_lease(project_id, account_id, rec,
                                 requirements=requirements, field_defs=defs, app_id=app_id)
    if not ok:
        SLog.w(
            TAG,
            f"claim lease failed project={project_id[:8]} account={account_id[:8]} "
            f"run={run_id[:12]} sn={str(sn or '')[:12]}",
        )
        return False

    from mino_nexus.resources.resource_allocation_log import append_allocation_log
    from mino_nexus.resources.project_env import account_ident as _ident_fn

    try:
        ident = _ident_fn(row) if row else account_id[:12]
    except Exception:
        ident = account_id[:12]

    same_run = prev_run == str(run_id or "").strip() and prev_run
    should_log = not same_run or (cid and cid != prev_case) or (cid and not prev_case)
    if should_log:
        phase = "run" if not cid else "case"
        append_allocation_log(
            project_id=project_id,
            action="lease_claim",
            message=f"租号成功 {ident}" + (f" ({phase})" if phase == "run" and not cid else ""),
            env=str(env or "")[:32],
            run_id=run_id,
            case_id=cid,
            sn=sn,
            account_id=account_id,
            account_ident=ident,
            detail={"score_reason": "claim", "lease_phase": phase},
        )
    return True


def _clear_leased(
    project_id: str,
    account_id: str,
    run_id: str,
    *,
    account_slot: str = "",
) -> None:
    """幂等释放当前 slot；未指定 slot 只允许释放 default。"""
    from mino_nexus.resources.pool_account_store import set_account_lease

    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next((r for r in rows if str(r.get("id") or "") == str(account_id)), None)
    if not row:
        return
    lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
    if str(lease.get("run_id") or "") != str(run_id or ""):
        return
    if normalize_account_slot(lease.get("slot")) != normalize_account_slot(account_slot):
        return
    from mino_nexus.resources.resource_allocation_log import append_allocation_log
    from mino_nexus.resources.project_env import account_ident as _ident_fn

    rid = str(run_id or "").strip()
    aid = str(account_id or "").strip()
    ident = _ident_fn(row) if row else aid[:12]
    if not set_account_lease(project_id, account_id, {}, expected_lease=lease):
        return
    append_allocation_log(
        project_id=project_id,
        action="lease_release",
        message=f"释放租约 {ident}",
        run_id=rid,
        case_id=str(lease.get("case_id") or "")[:80],
        sn=str(lease.get("sn") or "")[:64],
        account_id=aid,
        account_ident=ident,
        detail={"reason": "clear_leased"},
    )


def renew_run_lease(ctx: Any) -> bool:
    """Renew every account in the execution-unit ledger; one failure fails all."""
    leases = [
        dict(row) for row in (getattr(ctx, "leased_account_leases", None) or [])
        if isinstance(row, dict) and _account_id(row)
    ]
    legacy = getattr(ctx, "resource_lease", None) or {}
    if not leases and isinstance(legacy, dict) and legacy.get("account_id"):
        leases = [dict(legacy)]
    if not leases:
        return False
    for lease in leases:
        pid = str(lease.get("project_id") or "").strip()
        aid = str(lease.get("account_id") or "").strip()
        rid = str(lease.get("run_id") or getattr(ctx, "run_id", "") or "").strip()
        if not pid or not aid or not rid or rid != str(getattr(ctx, "run_id", "") or ""):
            return False
        try:
            if not _mark_leased(
                pid,
                aid,
                rid,
                app_id=str(lease.get("app_id") or getattr(ctx, "app_id", "") or ""),
                case_id=str(lease.get("case_id") or getattr(ctx, "case_id", "") or "")[:64],
                sn=str(lease.get("sn") or getattr(ctx, "sn", "") or ""),
                env=str(lease.get("env") or getattr(ctx, "env_profile", "") or ""),
                slot=str(lease.get("slot") or DEFAULT_ACCOUNT_SLOT),
                platform=str(lease.get("platform") or getattr(ctx, "platform", "") or ""),
                target_package=str(lease.get("target_package") or getattr(ctx, "target_package", "") or ""),
                env_surface=str(lease.get("env_surface") or ""),
                session_id=str(lease.get("session_id") or getattr(ctx, "session_id", "") or ""),
            ):
                return False
        except Exception as exc:
            SLog.w(TAG, f"renew lease failed account={aid[:12]}: {exc!r}")
            return False
    return True


def apply_lease_to_ctx(
    ctx: Any,
    row: dict[str, Any],
    *,
    project_id: str,
    slot: str = DEFAULT_ACCOUNT_SLOT,
    app_id: str = "",
    platform: str = "",
    target_package: str = "",
    env_surface: str = "",
) -> bool:
    """Add or replace one slot while retaining every other leased account."""
    from mino_nexus.resources.accounts.app_scope import (
        normalize_app_ids,
        validate_project_apps,
    )
    from mino_nexus.resources.gmail_alias_lease import enrich_gmail_alias_account_row
    from mino_nexus.resources.pool_account_store import read_leased_account_credentials

    _seed_legacy_account_views(ctx)
    src = row if isinstance(row, dict) else {}
    account_id = _account_id(src)
    lease = src.get("lease") if isinstance(src.get("lease"), dict) else {}
    resolved_slot = normalize_account_slot(slot or lease.get("slot"))
    ctx_app = str(getattr(ctx, "app_id", "") or "").strip()
    resolved_app = str(app_id or lease.get("app_id") or ctx_app).strip()
    if not resolved_app:
        SLog.w(TAG, f"lease has no app scope account={account_id[:12]}")
        return False
    try:
        validate_project_apps(project_id, [resolved_app])
    except ValueError:
        SLog.w(TAG, f"lease app outside project account={account_id[:12]} app={resolved_app}")
        return False

    same_primary_app = resolved_app == ctx_app
    resolved_platform = str(
        platform or lease.get("platform") or (getattr(ctx, "platform", "") if same_primary_app else "") or ""
    ).strip()
    resolved_target = str(
        target_package
        or lease.get("target_package")
        or (getattr(ctx, "target_package", "") if same_primary_app else "")
        or ""
    ).strip()
    # A device surface belongs to the current application. Cross-application
    # credentials must carry their own surface and must never inherit the device's.
    resolved_surface = str(
        env_surface
        or lease.get("env_surface")
        or (getattr(ctx, "env_surface", "") if same_primary_app else "")
        or ""
    ).strip()

    for item in leased_account_entries(ctx):
        if normalize_account_slot(item.get("slot")) == resolved_slot:
            if _account_id(item) and _account_id(item) != account_id:
                SLog.w(
                    TAG,
                    f"account slot replacement rejected slot={resolved_slot} "
                    f"current={_account_id(item)[:12]} next={account_id[:12]}",
                )
                return False
            continue
        if _account_id(item) == account_id:
            SLog.w(
                TAG,
                f"account already leased by another slot account={account_id[:12]} slot={resolved_slot}",
            )
            return False

    scoped_ctx = copy.copy(ctx)
    scoped_ctx.app_id = resolved_app
    scoped_ctx.platform = resolved_platform
    scoped_ctx.target_package = resolved_target
    scoped_ctx.env_surface = resolved_surface
    scoped_ctx.project_id = str(project_id or getattr(ctx, "project_id", "") or "")
    try:
        env_doc = ps.project_env(project_id) if project_id else None
    except KeyError:
        env_doc = None
    row = enrich_gmail_alias_account_row(src, scoped_ctx, env_doc=env_doc)
    if (
        project_id
        and str(row.get("email") or "").strip() != str(src.get("email") or "").strip()
        and "gmail_alias auto" in str(row.get("note") or "")
    ):
        try:
            from mino_nexus.resources.project_env import normalize_project_env, save_one_test_account

            doc = normalize_project_env(ps.project_env(project_id))
            save_one_test_account(doc, row, project_id=project_id, account_id=account_id)
        except Exception:
            pass

    ident = account_ident(row)
    facets = account_facets(row)
    credentials = read_leased_account_credentials(
        project_id,
        account_id,
        run_id=str(getattr(ctx, "run_id", "") or ""),
        case_id=str(getattr(ctx, "case_id", "") or ""),
        sn=str(getattr(ctx, "sn", "") or ""),
        app_id=resolved_app,
    )
    if credentials is None:
        SLog.w(TAG, f"lease credential read denied account={account_id[:12]}")
        return False
    lease_meta = {
        "account_id": account_id,
        "project_id": str(project_id or ""),
        "run_id": str(getattr(ctx, "run_id", "") or ""),
        "session_id": str(lease.get("session_id") or getattr(ctx, "session_id", "") or ""),
        "account_ident": ident,
        "sn": str(getattr(ctx, "sn", "") or ""),
        "case_id": str(getattr(ctx, "case_id", "") or ""),
        "env": str(row.get("env") or ""),
        "facets": facets,
        "profile_id": str(row.get("profile_id") or ""),
        "score": int(row.get("score") or 0),
        "reason": str(row.get("reason") or ""),
        "slot": resolved_slot,
        "app_id": resolved_app,
        "platform": resolved_platform,
        "target_package": resolved_target,
        "env_surface": resolved_surface,
        "leased_at": str(lease.get("leased_at") or lease.get("at") or ""),
        "expires_at": str(lease.get("expires_at") or ""),
        "lease_id": str(lease.get("lease_id") or ""),
    }
    account_meta = {
        "id": account_id,
        "account_id": account_id,
        "display_name": str(row.get("display_name") or ""),
        # ident 仅供展示；登录填表必须读 email/phone 的真实字段。
        "ident": ident,
        "phone": str(row.get("phone") or ""),
        "email": str(row.get("email") or ""),
        "username": str(row.get("username") or ""),
        "password": str(credentials.get("password") or ""),
        "otp": str(credentials.get("otp") or ""),
        "facets": facets,
        "profile_id": lease_meta["profile_id"],
        "env": lease_meta["env"],
        "label": account_label(row),
        # App scope is a lease invariant and is consumed again by callbacks.
        # Dropping it made a successfully leased row look out of scope later.
        "app_ids": normalize_app_ids(row.get("app_ids", [resolved_app])),
        "slot": resolved_slot,
        "app_id": resolved_app,
        "platform": resolved_platform,
        "target_package": resolved_target,
        "env_surface": resolved_surface,
    }
    accounts = [
        dict(item) for item in (getattr(ctx, "leased_accounts", None) or [])
        if isinstance(item, dict) and normalize_account_slot(item.get("slot")) != resolved_slot
    ]
    leases = [
        dict(item) for item in (getattr(ctx, "leased_account_leases", None) or [])
        if isinstance(item, dict) and normalize_account_slot(item.get("slot")) != resolved_slot
    ]
    accounts.append(account_meta)
    leases.append(lease_meta)
    ctx.leased_accounts = accounts
    ctx.leased_account_leases = leases
    _rebuild_account_views(ctx)
    return True

def current_lease_is_active(ctx: Any, account_id: str = "") -> bool:
    """Verify one leased account, or the compatibility/default account when omitted."""
    wanted = str(account_id or "").strip()
    leases = [
        dict(row) for row in (getattr(ctx, "leased_account_leases", None) or [])
        if isinstance(row, dict)
    ]
    if wanted:
        current = next((row for row in leases if _account_id(row) == wanted), {})
    else:
        current = getattr(ctx, "resource_lease", None) or {}
    if not isinstance(current, dict) or not current.get("account_id"):
        return False
    account_id = str(current.get("account_id") or "")
    project_id = str(current.get("project_id") or "")
    run_id = str(getattr(ctx, "run_id", "") or "")
    if not account_id or not project_id or not run_id or current.get("run_id") != run_id:
        return False
    if wanted:
        account_rows = [row for row in leased_account_entries(ctx) if _account_id(row) == wanted]
        if not account_rows:
            return False
        account = account_rows[0]
    else:
        picked = getattr(ctx, "picked_account", None) or {}
        if not isinstance(picked, dict) or _account_id(picked) != account_id:
            return False
        account = picked
        account_rows = [picked]

    def scoped(row: dict[str, Any], key: str) -> str:
        fallback = getattr(ctx, key if key != "env" else "env_profile", "") or ""
        return str(row.get(key) or fallback or "").strip()

    def same_scope(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for key in ("app_id", "platform", "target_package", "env_surface"):
            left_value = scoped(left, key)
            right_value = scoped(right, key)
            if left_value and right_value and left_value != right_value:
                return False
        return True

    current_slot = normalize_account_slot(current.get("slot"))
    if any(normalize_account_slot(row.get("slot")) != current_slot for row in account_rows):
        return False
    account_slot = normalize_account_slot(account.get("slot"))
    if current_slot != account_slot or not same_scope(current, account):
        return False
    try:
        app_id = str(current.get("app_id") or getattr(ctx, "app_id", "") or "")
        app = ps.require_app(app_id)
        if str(app.get("project_id") or "") != project_id:
            return False
        env_doc = ps.project_env(project_id)
    except KeyError:
        return False
    for row in list_test_accounts(env_doc, project_id=project_id):
        if str(row.get("id") or "") != account_id:
            continue
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if lease_expired(lease):
            return False
        for key in ("run_id", "case_id", "sn"):
            expected = str(current.get(key) or getattr(ctx, key, "") or "")
            if str(lease.get(key) or "") != expected:
                return False
        if normalize_account_slot(lease.get("slot")) != account_slot:
            return False
        if not same_scope(current, lease) or not same_scope(account, lease):
            return False
        env = str(current.get("env") or getattr(ctx, "env_profile", "") or "")
        return not env or str(row.get("env") or "") == env
    return False


def _restore_leases_for_run(
    ctx: Any,
    run_id: str,
    *,
    target_slot: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Restore context only after all matching slots pass identity and scope checks."""
    if str(run_id or "").strip() != str(getattr(ctx, "run_id", "") or "").strip():
        return [], "account_restore_run_mismatch"
    from mino_nexus.resources.accounts.app_scope import (
        account_matches_app,
        validate_project_apps,
    )

    primary_app = str(getattr(ctx, "app_id", "") or "").strip()
    if not primary_app:
        return [], "缺少 app_id"
    try:
        app = ps.require_app(primary_app)
    except KeyError:
        return [], f"未知应用 {primary_app}"
    project_id = str(app.get("project_id") or "").strip()
    if not project_id:
        return [], "无项目"
    _purge_expired_leases(project_id)
    env_doc = ps.project_env(project_id)
    rid = str(run_id or "").strip()
    if not rid:
        return [], "缺少 run_id"
    want_case = str(getattr(ctx, "case_id", "") or "").strip()
    want_sn = str(getattr(ctx, "sn", "") or "").strip()
    wanted_slot = normalize_account_slot(target_slot) if str(target_slot or "").strip() else ""

    found: list[tuple[str, dict[str, Any], dict[str, Any], str, str, str, str]] = []
    by_slot: dict[str, str] = {}
    by_account: dict[str, str] = {}
    for row in list_test_accounts(env_doc, project_id=project_id):
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if str(lease.get("run_id") or "").strip() != rid:
            continue
        leased_case = str(lease.get("case_id") or "").strip()
        if want_case and leased_case and leased_case != want_case:
            continue
        lease_sn = str(lease.get("sn") or "").strip()
        if want_sn and lease_sn and lease_sn != want_sn:
            continue
        slot = normalize_account_slot(lease.get("slot"))
        if wanted_slot and slot != wanted_slot:
            continue
        requested_env = str(getattr(ctx, "env_profile", "") or "").strip()
        if requested_env and str(row.get("env") or "") != requested_env:
            return [], "account_slot_scope_mismatch"
        requested_session = str(getattr(ctx, "session_id", "") or "").strip()
        if lease.get("session_id") and requested_session and lease["session_id"] != requested_session:
            return [], "account_slot_scope_mismatch"
        if lease_expired(lease):
            return [], "resource_account_missing"
        lease_app = str(lease.get("app_id") or primary_app).strip()
        try:
            validate_project_apps(project_id, [lease_app])
        except ValueError:
            return [], f"租约应用不在当前项目: {lease_app}"
        if not account_matches_app(row, lease_app):
            return [], f"租约账号不属于应用范围: {_account_id(row)}"
        account_id = _account_id(row)
        if slot in by_slot:
            return [], f"同一 slot 存在多条活跃租约: {slot}"
        if account_id in by_account:
            return [], f"同一账号被多个 slot 租用: {account_id}"
        by_slot[slot] = account_id
        by_account[account_id] = slot
        same_primary_app = lease_app == primary_app
        platform = str(
            lease.get("platform")
            or (getattr(ctx, "platform", "") if same_primary_app else "")
            or ""
        ).strip()
        target_package = str(
            lease.get("target_package")
            or (getattr(ctx, "target_package", "") if same_primary_app else "")
            or ""
        ).strip()
        env_surface = str(
            lease.get("env_surface")
            or (getattr(ctx, "env_surface", "") if same_primary_app else "")
            or ""
        ).strip()
        if wanted_slot and slot == wanted_slot:
            requested_platform = str(getattr(ctx, "platform", "") or "").strip()
            requested_target = str(getattr(ctx, "target_package", "") or "").strip()
            requested_surface = str(getattr(ctx, "env_surface", "") or "").strip()
            if (
                lease_app != primary_app
                or (platform and requested_platform and platform != requested_platform)
                or (target_package and requested_target and target_package != requested_target)
                or (env_surface and requested_surface and env_surface != requested_surface)
            ):
                return [], "account_slot_scope_mismatch"
        found.append((slot, row, lease, lease_app, platform, target_package, env_surface))

    if not found:
        return [], "本 run 无活跃租约"

    temp = copy.copy(ctx)
    if wanted_slot:
        temp.leased_accounts = [
            dict(item) for item in (getattr(ctx, "leased_accounts", None) or [])
            if isinstance(item, dict) and normalize_account_slot(item.get("slot")) != wanted_slot
        ]
        temp.leased_account_leases = [
            dict(item) for item in (getattr(ctx, "leased_account_leases", None) or [])
            if isinstance(item, dict) and normalize_account_slot(item.get("slot")) != wanted_slot
        ]
    else:
        temp.leased_accounts = []
        temp.leased_account_leases = []
    temp.picked_account = {}
    temp.resource_lease = {}
    temp.leased_account_by_slot = {}
    restored: list[dict[str, Any]] = []
    for slot, row, _lease, lease_app, platform, target_package, env_surface in sorted(found, key=lambda item: item[0]):
        ok = apply_lease_to_ctx(
            temp,
            row,
            project_id=project_id,
            slot=slot,
            app_id=lease_app,
            platform=platform,
            target_package=target_package,
            env_surface=env_surface,
        )
        if not ok:
            return [], "租约凭证读取失败，未恢复账号"
        restored.append(dict(row))
    if not renew_run_lease(temp):
        return [], "账号租约续租失败，未恢复凭证"
    for name in (
        "leased_accounts",
        "leased_account_leases",
        "leased_account_by_slot",
        "picked_account",
        "resource_lease",
        "accounts_brief",
        "resource_env",
    ):
        setattr(ctx, name, copy.deepcopy(getattr(temp, name, None)))
    _rebuild_account_views(ctx)
    return restored, ""


def restore_leases_for_run(ctx: Any, run_id: str) -> tuple[list[dict[str, Any]], str]:
    """Restore every active account slot for this run/session atomically."""
    return _restore_leases_for_run(ctx, run_id)


def restore_lease_slot_for_run(
    ctx: Any,
    run_id: str,
    slot: Any = DEFAULT_ACCOUNT_SLOT,
) -> tuple[dict[str, Any] | None, str]:
    """Restore only one slot while preserving every independently leased slot."""
    rows, err = _restore_leases_for_run(ctx, run_id, target_slot=normalize_account_slot(slot))
    if not rows:
        return None, err
    row = leased_account_for_slot(ctx, slot)
    return (row or rows[0]), ""


def restore_lease_for_run(ctx: Any, run_id: str) -> tuple[dict[str, Any] | None, str]:
    """Compatibility wrapper: restore all and return default/first for old callers."""
    rows, err = restore_leases_for_run(ctx, run_id)
    if not rows:
        return None, err
    default = leased_account_for_slot(ctx, DEFAULT_ACCOUNT_SLOT)
    return (default or rows[0]), (err if default is None else "")

def row_satisfies_requirements(
    row: dict[str, Any],
    requirements: dict[str, Any],
    env_doc: dict[str, Any],
    *,
    observed: dict[str, str] | None = None,
) -> bool:
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.resources.resource_pool import account_facet_values_for_match, match_requirements

    if not isinstance(row, dict) or not isinstance(requirements, dict):
        return False
    defs = {
        str(d.get("key") or ""): d
        for d in merged_pool_field_defs(env_doc)
        if str(d.get("key") or "")
    }
    facets = account_facet_values_for_match(row)
    ok, score, _ = match_requirements(
        facets,
        requirements,
        observed=observed,
        field_defs=defs,
    )
    return bool(ok and score >= 0)



def persist_account_facets(
    project_id: str,
    account_id: str,
    facets: dict[str, str],
    *,
    log_ctx: Any = None,
) -> None:
    from mino_nexus.resources.account_facet_sync import (
        FacetLogContext,
        persist_account_facets_with_log,
    )

    ctx = log_ctx if isinstance(log_ctx, FacetLogContext) else None
    persist_account_facets_with_log(project_id, account_id, facets, log_ctx=ctx)


def lease_for_context(ctx, params, **kwargs):
    from mino_nexus.resources.resource_claim_tx import reset_claim_failure
    reset_claim_failure()
    from mino_nexus.resources.account_fact_scope import fact_scope
    with fact_scope(ctx):
        return _lease_for_context(ctx, params, **kwargs)


def _lease_for_context(
    ctx: Any,
    params: dict[str, Any] | None,
    *,
    ai_reasoning: str = "",
    need_facets: dict[str, Any] | None = None,
    observed_by_account: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """从 ctx.app_id 对应项目号池租号。成功返回 (row, '')，失败返回 (None, reason)。"""
    run_id = str(getattr(ctx, "run_id", "") or "").strip()
    slot = normalize_account_slot(getattr(ctx, "account_slot", ""))

    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    if not app_id:
        return None, "缺少 app_id，无法定位号池"

    try:
        app = ps.require_app(app_id)
    except KeyError:
        return None, f"未知应用 {app_id}"

    project_id = str(app.get("project_id") or "").strip()
    if not project_id:
        return None, "应用未归属项目，号池不可用"

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        return None, f"项目 {project_id} 不存在"

    requirements = _requirements_from_ctx(
        ctx,
        params or {},
        need_facets=need_facets,
        ai_reasoning=ai_reasoning,
        env_doc=env_doc,
    )
    from mino_nexus.resources.resource_pool import relax_pool_session_for_device_login

    requirements = relax_pool_session_for_device_login(requirements)
    if (need_facets or {}).get("advisory_business_data"):
        # Script allocation uses business state as a preference, never as inventory certification.
        strict = {"id", "account_id", "phone", "email", "username", "env"}
        required = list(requirements.get("all") or [])
        requirements["all"] = [clause for clause in required if clause.get("facet") in strict]
        requirements["prefer"] = [clause for clause in required if clause.get("facet") not in strict] + list(requirements.get("prefer") or [])
        if not requirements["all"] and not requirements["prefer"]:
            requirements["prefer"] = [{"facet": "health", "op": "eq", "value": "available"}]
    if not (requirements.get("all") or requirements.get("prefer")):
        return None, "缺少租号需求（用例前置 / requirements）"

    prompt = str((params or {}).get("precondition")
                 or (need_facets or {}).get("prompt") or ai_reasoning or "").strip()
    from mino_nexus.resources.account_ident_parse import (
        account_row_matches_hints,
        extract_account_ident_hints,
    )

    ident_hints = extract_account_ident_hints(prompt)


    from mino_nexus.resources.gmail_alias_lease import (
        ensure_ctx_env_surface,
        gmail_alias_lease_enabled,
        provision_gmail_alias_account,
        should_provision_gmail_alias,
        try_provision_after_lease_miss as try_provision_gmail_after_miss,
    )
    from mino_nexus.resources.phone_pool_lease import (
        phone_pool_lease_enabled,
        phone_seq_account_id,
        provision_phone_seq_account,
        should_provision_phone_seq,
        try_provision_after_lease_miss as try_provision_phone_after_miss,
    )

    ensure_ctx_env_surface(ctx, env_doc)
    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    platform = str(getattr(ctx, "platform", "") or "android")
    target_id = str(getattr(ctx, "target_package", "") or "")
    env_surface = str(getattr(ctx, "env_surface", "") or "")
    holder_sn = str(getattr(ctx, "sn", "") or "").strip()
    excluded_account_ids = _excluded_accounts_for_slot(ctx, slot)

    restored, rerr = restore_lease_slot_for_run(ctx, run_id, slot)
    if rerr and rerr != "本 run 无活跃租约":
        return None, rerr
    if restored and (need_facets or {}).get("idempotent_slot"):
        if not _script_identity_matches(restored, {"account_requirements": requirements}, env_doc):
            return None, "account_slot_identity_mismatch"
    if restored and ident_hints and not account_row_matches_hints(restored, ident_hints):
        SLog.i(
            TAG,
            f"run lease ident mismatch run={run_id[:12]} "
            f"account={account_ident(restored)} hints={ident_hints[:2]}; re-pick",
        )
        _clear_leased(project_id, str(restored.get("id") or ""), run_id, account_slot=slot)
        release_ctx_slot(ctx, slot)
        restored = None
    if restored and not _row_matches_ctx_login_kind(restored, ctx, env_doc, prompt=prompt):
        SLog.i(
            TAG,
            f"run lease login-kind mismatch run={run_id[:12]} "
            f"account={account_ident(restored)} kind={_login_kind_for_ctx(ctx, env_doc, prompt=prompt)}; re-pick",
        )
        _clear_leased(project_id, str(restored.get("id") or ""), run_id, account_slot=slot)
        release_ctx_slot(ctx, slot)
        restored = None
    if restored:
        aid = str(restored.get("id") or "")
        if should_provision_gmail_alias(
            requirements, prompt=prompt, env_doc=env_doc, ctx=ctx
        ) and "gmail_alias auto" not in str(restored.get("note") or ""):
            _clear_leased(project_id, aid, run_id, account_slot=slot)
            release_ctx_slot(ctx, slot)
            restored = None
        elif should_provision_phone_seq(
            requirements, prompt=prompt, env_doc=env_doc, ctx=ctx
        ) and not ident_hints and aid != phone_seq_account_id(ctx, project_id=project_id, run_id=run_id):
            _clear_leased(project_id, aid, run_id, account_slot=slot)
            release_ctx_slot(ctx, slot)
            restored = None
        else:
            obs = observed_by_account.get(aid) if isinstance(observed_by_account, dict) and aid else None
            if row_satisfies_requirements(restored, requirements, env_doc, observed=obs):
                if ident_hints and not account_row_matches_hints(restored, ident_hints):
                    SLog.i(
                        TAG,
                        f"restored account fails ident hints account={account_ident(restored)}",
                    )
                    _clear_leased(project_id, aid, run_id, account_slot=slot)
                    release_ctx_slot(ctx, slot)
                elif not _row_matches_ctx_login_kind(restored, ctx, env_doc, prompt=prompt):
                    _clear_leased(project_id, aid, run_id, account_slot=slot)
                    release_ctx_slot(ctx, slot)
                else:
                    if not _mark_leased(project_id, aid, run_id,
                            app_id=app_id,
                            case_id=str(getattr(ctx, "case_id", "") or ""), sn=holder_sn,
                            env=env_profile, requirements=requirements,
                            slot=slot, platform=platform, target_package=target_id,
                            env_surface=env_surface, session_id=str(getattr(ctx, "session_id", "") or "")):
                        from mino_nexus.resources.resource_claim_tx import claim_failure_code
                        return None, claim_failure_code("resource_busy")
                    return restored, ""
            else:
                SLog.i(
                    TAG,
                    f"run lease mismatch run={run_id[:12]} account={account_ident(restored)}; re-pick for case",
                )
                release_ctx_slot(ctx, slot)

    if should_provision_gmail_alias(requirements, prompt=prompt, env_doc=env_doc, ctx=ctx):
        alias_row = provision_gmail_alias_account(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if alias_row:
            account_id = str(alias_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                app_id=app_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
                requirements=requirements,
                slot=slot,
                platform=platform,
                target_package=target_id,
                env_surface=env_surface,
                session_id=str(getattr(ctx, "session_id", "") or ""),
            ):
                if not apply_lease_to_ctx(ctx, alias_row, project_id=project_id, slot=slot, app_id=app_id, platform=platform, target_package=target_id, env_surface=env_surface):
                    _clear_leased(project_id, account_id, run_id, account_slot=slot)
                    return None, "租约凭证读取失败"
                SLog.i(TAG, f"leased gmail alias (fresh) {account_ident(alias_row)} run={run_id[:12]}")
                return alias_row, ""
    if not ident_hints and should_provision_phone_seq(requirements, prompt=prompt, env_doc=env_doc, ctx=ctx):
        phone_row = provision_phone_seq_account(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if phone_row:
            account_id = str(phone_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                app_id=app_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
                requirements=requirements,
                slot=slot,
                platform=platform,
                target_package=target_id,
                env_surface=env_surface,
                session_id=str(getattr(ctx, "session_id", "") or ""),
            ):
                if not apply_lease_to_ctx(ctx, phone_row, project_id=project_id, slot=slot, app_id=app_id, platform=platform, target_package=target_id, env_surface=env_surface):
                    _clear_leased(project_id, account_id, run_id, account_slot=slot)
                    return None, "租约凭证读取失败"
                SLog.i(TAG, f"leased phone seq (fresh) {account_ident(phone_row)} run={run_id[:12]}")
                return phone_row, ""
        return None, "新账号未能满足前置或资源仍被占用；未回退复用旧账号"

    accounts = list_test_accounts(env_doc, project_id=project_id)
    if not accounts and not gmail_alias_lease_enabled(env_doc, ctx) and not phone_pool_lease_enabled(
        env_doc, ctx
    ):
        if ctx is not None and _login_mode_for_ctx(ctx, env_doc, prompt=prompt) != "auto":
            return None, _lease_failure_base(env_doc, ctx, prompt=prompt)
        return None, "号池为空，请先在项目里添加测试账号"

    _purge_expired_leases(project_id)
    candidate = _pick_row(
        env_doc,
        project_id=project_id,
        app_id=str(getattr(ctx, "app_id", "") or ""),
        requirements=requirements,
        env_profile=env_profile,
        surface=str(getattr(ctx, "env_surface", "") or ""),
        platform=platform,
        target_id=target_id,
        run_id=run_id,
        holder_sn=holder_sn,
        prompt=prompt,
        observed_by_account=observed_by_account,
        excluded_account_ids=excluded_account_ids,
    )
    row: dict[str, Any] | None = None
    if candidate:
        account_id = str(candidate.get("id") or "")
        if account_id and run_id:
            if _mark_leased(
                project_id,
                account_id,
                run_id,
                app_id=app_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
                requirements=requirements,
                slot=slot,
                platform=platform,
                target_package=target_id,
                env_surface=env_surface,
                session_id=str(getattr(ctx, "session_id", "") or ""),
            ):
                row = candidate
            else:
                from mino_nexus.resources.resource_claim_tx import claim_failure_code
                return None, claim_failure_code("resource_busy")
        else:
            row = candidate

    if not row:
        # The normal ranker excludes held accounts. Probe only hard identity
        # matches to report the real holder instead of provisioning a substitute.
        identity_clauses = [clause for clause in requirements.get("all", [])
                            if clause.get("facet") in {"id", "account_id", "email", "phone", "username"}]
        if identity_clauses:
            from mino_nexus.resources.resource_pool import lease_blocks_other_holder
            for held in list_test_accounts(env_doc, project_id=project_id):
                if _script_identity_matches(held, {"account_requirements": {"all": identity_clauses}}, env_doc):
                    lease = held.get("lease") or {}
                    if lease_blocks_other_holder(lease, run_id=run_id, sn=holder_sn):
                        claimed = _mark_leased(project_id, _account_id(held), run_id,
                                     case_id=str(getattr(ctx, "case_id", "") or ""), sn=holder_sn,
                                     app_id=app_id, slot=slot, platform=platform,
                                     target_package=target_id, env_surface=env_surface,
                                     session_id=str(getattr(ctx, "session_id", "") or ""))
                        if claimed:
                            if apply_lease_to_ctx(ctx, held, project_id=project_id, slot=slot, app_id=app_id,
                                                  platform=platform, target_package=target_id, env_surface=env_surface):
                                return held, ""
                            _clear_leased(project_id, _account_id(held), run_id, account_slot=slot)
                            return None, "租约凭证读取失败"
                        from mino_nexus.resources.resource_claim_tx import claim_failure_code
                        return None, claim_failure_code("resource_account_lease_held")
        alias_row = try_provision_gmail_after_miss(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if not alias_row and not ident_hints:
            alias_row = try_provision_phone_after_miss(
                ctx,
                project_id=project_id,
                env_doc=env_doc,
                requirements=requirements,
                run_id=run_id,
            )
        if alias_row:
            account_id = str(alias_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                app_id=app_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
                requirements=requirements,
                slot=slot,
                platform=platform,
                target_package=target_id,
                env_surface=env_surface,
                session_id=str(getattr(ctx, "session_id", "") or ""),
            ):
                if not apply_lease_to_ctx(ctx, alias_row, project_id=project_id, slot=slot, app_id=app_id, platform=platform, target_package=target_id, env_surface=env_surface):
                    _clear_leased(project_id, account_id, run_id, account_slot=slot)
                    return None, "租约凭证读取失败"
                SLog.i(
                    TAG,
                    f"leased gmail alias after miss {account_ident(alias_row)} run={run_id[:12]}",
                )
                return alias_row, ""
        hint = _lease_unavailable_hint(
            env_doc,
            project_id=project_id,
            requirements=requirements,
            run_id=run_id,
            ctx=ctx,
            prompt=prompt,
        )
        base = _lease_failure_base(env_doc, ctx, prompt=prompt)
        from mino_nexus.resources.resource_allocation_log import append_allocation_log

        append_allocation_log(
            project_id=project_id,
            action="lease_fail",
            message=f"{base}{hint}"[:500],
            env=env_profile,
            run_id=run_id,
            case_id=str(getattr(ctx, "case_id", "") or ""),
            sn=holder_sn,
            package_id=target_id,
            detail={"requirements": requirements},
        )
        from mino_nexus.resources.resource_claim_tx import claim_failure_code
        held = claim_failure_code("")
        if held:
            return None, held
        return None, f"{base}{hint}"

    if not apply_lease_to_ctx(ctx, row, project_id=project_id, slot=slot, app_id=app_id, platform=platform, target_package=target_id, env_surface=env_surface):
        _clear_leased(project_id, str(row.get("id") or ""), run_id, account_slot=slot)
        return None, "租约凭证读取失败"
    SLog.i(TAG, f"leased {account_ident(row)} for run={run_id[:12]} score={row.get('score')}")
    return row, ""


def account_login_policy(ctx: Any) -> dict[str, Any]:
    """Return the OTP/login policy effective for one scoped account context."""
    from mino_nexus.resources.otp_resolve import otp_context, resolve_gmail_inbox

    try:
        env_doc, secrets, otp_slot = otp_context(ctx)
    except Exception:
        return {}
    otp = otp_slot if isinstance(otp_slot, dict) else {}
    login = secrets.get("login") if isinstance(secrets, dict) else {}
    login = login if isinstance(login, dict) else {}
    if not otp and not login:
        return {}
    login_kind = str(login.get("kind") or "phone")
    inbox = ""
    if login_kind == "email":
        try:
            inbox = resolve_gmail_inbox(env_doc, ctx)
        except Exception:
            inbox = ""
    return {
        "otp_mode": str(otp.get("mode") or "auto"),
        "otp_fixed": str(otp.get("fixed") or ""),
        "otp_from_allowlist": [str(x) for x in (otp.get("from_allowlist") or []) if str(x).strip()],
        "otp_subject_contains": str(otp.get("subject_contains") or ""),
        "otp_poll_interval_ms": int(otp.get("poll_interval_ms") or 200),
        "otp_max_wait_ms": int(otp.get("max_wait_ms") or 90000),
        "login_kind": login_kind,
        "otp_inbox_address": inbox if login_kind == "email" else "",
    }


def _script_identity_matches(account, case, env_doc) -> bool:
    requirements = (case or {}).get("account_requirements") or {}
    clauses = [clause for clause in requirements.get("all", []) if isinstance(clause, dict)
               and clause.get("facet") in {"id", "account_id", "email", "phone", "username", "env"}]
    return not clauses or row_satisfies_requirements(account, {"all": clauses, "prefer": []}, env_doc)


def lease_scoped_script_account(
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    slot: Any = DEFAULT_ACCOUNT_SLOT,
    app_id: Any = None,
    platform: Any = None,
) -> tuple[dict[str, Any] | None, Any | None, str]:
    """Idempotently lease one account slot across any supported app/platform.

    ``slot`` addresses an independent identity. Reusing it returns the same
    active lease; changing its app/platform is rejected rather than silently
    replacing the account.
    """
    from mino_nexus.resources.resource_claim_tx import reset_claim_failure
    reset_claim_failure()
    try:
        wanted_slot = validate_account_slot(slot)
    except ValueError:
        return None, None, "account_slot_invalid"

    _seed_legacy_account_views(ctx)
    existing = leased_account_for_slot(ctx, wanted_slot)
    explicit_app = app_id is not None
    explicit_platform = platform is not None
    if explicit_app and type(app_id) is not str:
        return None, None, "account_app_invalid"
    if explicit_platform and type(platform) is not str:
        return None, None, "account_platform_not_supported"
    requested_app = str(app_id or "").strip() if explicit_app else ""
    requested_platform = str(platform or "").strip() if explicit_platform else ""
    if explicit_app and not requested_app:
        return None, None, "account_app_invalid"
    if explicit_platform and not requested_platform:
        return None, None, "account_platform_not_supported"

    if existing:
        existing_lease = next(
            (
                dict(row) for row in (getattr(ctx, "leased_account_leases", None) or [])
                if isinstance(row, dict)
                and normalize_account_slot(row.get("slot")) == wanted_slot
            ),
            {},
        )
        existing_app = str(existing_lease.get("app_id") or existing.get("app_id") or "").strip()
        existing_platform = str(
            existing_lease.get("platform") or existing.get("platform") or ""
        ).strip()
        if explicit_app:
            if existing_app and existing_app != requested_app:
                return None, None, "account_slot_scope_mismatch"
            if not existing_app and requested_app != str(getattr(ctx, "app_id", "") or ""):
                return None, None, "account_slot_scope_mismatch"
        if explicit_platform:
            if existing_platform and existing_platform != requested_platform:
                return None, None, "account_slot_scope_mismatch"
            fallback_platform = str(getattr(ctx, "platform", "") or "android").strip()
            if not existing_platform and requested_platform != fallback_platform:
                return None, None, "account_slot_scope_mismatch"
        account_id = _account_id(existing_lease) or _account_id(existing)
        if not account_id or not current_lease_is_active(ctx, account_id):
            return None, None, "resource_account_missing"
        scoped_ctx = copy.copy(ctx)
        scoped_ctx.account_slot = wanted_slot
        scoped_ctx.app_id = existing_app or str(getattr(ctx, "app_id", "") or "")
        scoped_ctx.platform = existing_platform or str(getattr(ctx, "platform", "") or "")
        scoped_ctx.target_package = str(
            existing_lease.get("target_package") or existing.get("target_package") or ""
        )
        scoped_ctx.env_surface = str(
            existing_lease.get("env_surface") or existing.get("env_surface") or ""
        )
        if not _script_identity_matches(existing, case, {}):
            return None, None, "account_slot_identity_mismatch"
        # This context represents one slot, so compatibility fields must not
        # retain the default or most-recent account from the parent context.
        scoped_ctx.picked_account = dict(existing)
        scoped_ctx.resource_lease = dict(existing_lease)
        return existing, scoped_ctx, ""

    target_app = str(requested_app if explicit_app else getattr(ctx, "app_id", "") or "").strip()
    if not target_app:
        return None, None, "account_app_invalid"
    raw_platform = requested_platform if explicit_platform else str(getattr(ctx, "platform", "") or "android")
    target_plat = str(raw_platform or "").strip()
    try:
        from mino_nexus.core.platform_contract import TARGET_PLATFORMS
        from mino_nexus.resources.application_channels import app_platforms
        app = ps.require_app(target_app)
        if target_plat not in TARGET_PLATFORMS or target_plat not in app_platforms(app):
            return None, None, "account_platform_not_supported"
        primary_app = str(getattr(ctx, "app_id", "") or "").strip()
        if not primary_app:
            return None, None, "account_app_invalid"
        primary = ps.require_app(primary_app)
        project_id = str(primary.get("project_id") or "").strip()
        if not project_id:
            return None, None, "account_project_missing"
        from mino_nexus.resources.accounts.app_scope import validate_project_apps
        validate_project_apps(project_id, [target_app])
        env_doc = ps.project_env(project_id)
        same_scope = (
            target_app == primary_app
            and target_plat == str(getattr(ctx, "platform", "") or "android")
        )
        requested_surface = str(getattr(ctx, "env_surface", "") or "") if same_scope else ""
        target_package = str(getattr(ctx, "target_package", "") or "") if same_scope else ""
        env_profile = str(getattr(ctx, "env_profile", "") or "test")
        from mino_nexus.resources.project_env import resolve_surface_for_app
        surface = resolve_surface_for_app(
            env_doc,
            app_id=target_app,
            platform=target_plat,
            target_id=target_package,
            surface=requested_surface,
            env_profile=env_profile,
        )
        if not surface:
            return None, None, "account_surface_not_found"
        from mino_nexus.resources.app_automation import package_for_app
        package = package_for_app(
            app,
            env_profile=env_profile,
            platform=target_plat,
            surface=surface,
            env_doc=env_doc,
        )
    except KeyError:
        return None, None, "account_app_not_in_project"
    except ValueError as exc:
        code = str(exc)
        if code in {"account_app_not_in_project", "account_app_not_found"}:
            return None, None, "account_app_not_in_project"
        if code.startswith("surface_"):
            return None, None, "account_surface_not_found"
        if code == "application_channel_ambiguous":
            return None, None, "account_surface_selection_required"
        return None, None, "account_scope_invalid"

    scoped_ctx = copy.copy(ctx)
    scoped_ctx.leased_accounts = [dict(row) for row in (getattr(ctx, "leased_accounts", None) or []) if isinstance(row, dict)]
    scoped_ctx.leased_account_leases = [dict(row) for row in (getattr(ctx, "leased_account_leases", None) or []) if isinstance(row, dict)]
    scoped_ctx.leased_account_by_slot = dict(getattr(ctx, "leased_account_by_slot", None) or {})
    scoped_ctx.account_slot = wanted_slot
    scoped_ctx.app_id = target_app
    scoped_ctx.platform = target_plat
    scoped_ctx.target_package = str(package or "")
    scoped_ctx.env_surface = surface
    scoped_ctx.project_id = project_id
    # Compatibility fields belong to default/first, never to the slot being allocated.
    scoped_ctx.picked_account = {}
    scoped_ctx.resource_lease = {}
    leased, err = lease_script_account(scoped_ctx, case)
    if not leased:
        return None, None, err or "resource_account_missing"
    account = leased_account_for_slot(scoped_ctx, wanted_slot) or dict(leased)
    ctx.leased_accounts = [dict(row) for row in (getattr(scoped_ctx, "leased_accounts", None) or []) if isinstance(row, dict)]
    ctx.leased_account_leases = [dict(row) for row in (getattr(scoped_ctx, "leased_account_leases", None) or []) if isinstance(row, dict)]
    _rebuild_account_views(ctx)
    return account, scoped_ctx, ""


def lease_script_account(ctx: Any, case: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """脚本要账号时再租。只认用例上写明的 facet 子句和模板，不扫描前置散文。"""
    from mino_nexus.resources.resource_pool import empty_requirements

    row = case if isinstance(case, dict) else {}
    explicit = row.get("account_requirements") if isinstance(row.get("account_requirements"), dict) else {}
    template_id = str(row.get("account_template_id") or row.get("template_id") or "").strip()
    req = empty_requirements()
    from mino_nexus.resources.account_data_match import validate_data_requirements
    req["data_requirements"] = validate_data_requirements(row.get("data_requirements"))
    env = str(getattr(ctx, "env_profile", "") or "test").strip().lower()
    if env:
        req["env"] = env
    for bucket in ("all", "prefer"):
        req[bucket] = [
            clause for clause in (explicit.get(bucket) or [])
            if isinstance(clause, dict) and str(clause.get("facet") or "").strip() and str(clause.get("op") or "").strip()
        ]
    need = {
        "need_account": True,
        "advisory_business_data": True,
        "idempotent_slot": True,
        "skip_prose": True,
        "prompt": "",
        "session": "",
        "profile": False,
        "address": False,
        "template_id": template_id,
        "requirements": req,
    }
    return lease_for_context(ctx, {"template_id": template_id} if template_id else {}, need_facets=need)


def ensure_case_account_lease(
    run_id: str,
    *,
    app_id: str,
    env_profile: str = "test",
    platform: str = "android",
    target_package: str = "",
    case: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
    plugin_user_id: str = "",
) -> tuple[bool, str]:
    """按当前用例前置租号；与同 run 已有租约冲突时释放并重新选号。"""
    from mino_nexus.resources.session.ensure import account_need_from_case

    from mino_nexus.resources.session.scene_gate import ensure_case_scene

    from mino_nexus.resources import project_store as ps

    project_id = ""
    app_key = str(app_id or "").strip()
    if app_key:
        try:
            app_row = ps.require_app(app_key)
            project_id = str(app_row.get("project_id") or "").strip()
        except KeyError:
            project_id = ""
    env_doc = ps.project_env(project_id) if project_id else None
    merged_scene = ensure_case_scene(
        case or {},
        scene if isinstance(scene, dict) else None,
        env_doc=env_doc,
        target_package=str(target_package or ""),
        env_profile=str(env_profile or "test"),
    )
    if isinstance(case, dict):
        case["case_scene"] = merged_scene
    need = account_need_from_case(case, merged_scene)
    if not need.get("need_account"):
        return True, ""
    rid = str(run_id or "").strip()
    cid = str((case or {}).get("case_id") or "")[:24]
    device_sn = str((case or {}).get("sn") or "").strip()
    from mino_nexus.resources.gmail_alias_lease import ensure_ctx_env_surface

    ctx = SimpleNamespace(
        run_id=rid,
        case_id=cid,
        sn=device_sn,
        app_id=str(app_id or "").strip(),
        env_profile=str(env_profile or "test").strip(),
        platform=str(platform or "android").strip(),
        target_package=str(target_package or "").strip(),
        plugin_user_id=str(plugin_user_id or "").strip(),
    )
    if env_doc:
        ensure_ctx_env_surface(ctx, env_doc)
    prompt = str(need.get("prompt") or "").strip()
    row, err = lease_for_context(
        ctx,
        {"precondition": prompt} if prompt else {},
        need_facets=need,
    )
    if row:
        SLog.i(
            TAG,
            f"case lease ok run={rid[:12]} case={cid} account={account_ident(row)}",
        )
        return True, ""
    reason = err or "租号失败"
    SLog.w(TAG, f"case lease failed run={rid[:12]} case={cid}: {reason}")
    return False, reason


def ensure_run_account_lease(
    run_id: str,
    *,
    app_id: str,
    env_profile: str = "test",
    platform: str = "android",
    target_package: str = "",
    case: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """兼容旧调用：等同 ensure_case_account_lease。"""
    return ensure_case_account_lease(
        run_id,
        app_id=app_id,
        env_profile=env_profile,
        platform=platform,
        target_package=target_package,
        case=case,
        scene=scene,
    )


def release_ctx_account(ctx: Any, account_id: str = "", *, slot: str = "") -> None:
    """Release one account from ctx without touching any other leased slot."""
    wanted_id = str(account_id or "").strip()
    wanted_slot = normalize_account_slot(slot) if str(slot or "").strip() else ""
    account_rows = [
        dict(item) for item in (getattr(ctx, "leased_accounts", None) or [])
        if isinstance(item, dict)
    ]
    lease_rows = [
        dict(item) for item in (getattr(ctx, "leased_account_leases", None) or [])
        if isinstance(item, dict)
    ]
    selected: dict[str, Any] = {}
    if wanted_id:
        selected = next((row for row in lease_rows if _account_id(row) == wanted_id), {})
    elif wanted_slot:
        selected = next(
            (row for row in lease_rows if normalize_account_slot(row.get("slot")) == wanted_slot),
            {},
        )
    else:
        selected = dict(getattr(ctx, "resource_lease", None) or {})
    if not selected:
        return
    selected_slot = normalize_account_slot(selected.get("slot") or wanted_slot)
    selected_id = _account_id(selected)
    pid = str(selected.get("project_id") or "").strip()
    rid = str(selected.get("run_id") or getattr(ctx, "run_id", "") or "").strip()
    if not pid or not selected_id:
        return
    from mino_nexus.records.write_state import protected
    if protected(rid, sn=str(selected.get("sn") or getattr(ctx, "sn", "") or "")):
        return
    try:
        _clear_leased(
            pid,
            selected_id,
            rid,
            account_slot=selected_slot,
        )
    except Exception as exc:
        SLog.w(TAG, f"release lease failed: {exc!r}")
        return
    ctx.leased_accounts = [
        row for row in account_rows
        if not (
            _account_id(row) == selected_id
            or normalize_account_slot(row.get("slot")) == selected_slot
        )
    ]
    ctx.leased_account_leases = [
        row for row in lease_rows
        if not (
            _account_id(row) == selected_id
            or normalize_account_slot(row.get("slot")) == selected_slot
        )
    ]
    _rebuild_account_views(ctx)


def release_ctx_slot(ctx: Any, slot: Any = DEFAULT_ACCOUNT_SLOT) -> None:
    """Release one idempotency slot; other slots stay leased."""
    wanted = normalize_account_slot(slot)
    lease = next(
        (
            row for row in (getattr(ctx, "leased_account_leases", None) or [])
            if isinstance(row, dict) and normalize_account_slot(row.get("slot")) == wanted
        ),
        {},
    )
    account = leased_account_for_slot(ctx, wanted)
    release_ctx_account(
        ctx,
        _account_id(lease) or _account_id(account),
        slot=wanted,
    )


def release_ctx_lease(ctx: Any) -> None:
    """Explicitly release every slot owned by this execution context."""
    lease_rows = [
        dict(row) for row in (getattr(ctx, "leased_account_leases", None) or [])
        if isinstance(row, dict) and _account_id(row)
    ]
    if not lease_rows:
        legacy = dict(getattr(ctx, "resource_lease", None) or {})
        if _account_id(legacy):
            lease_rows = [legacy]
    seen: set[tuple[str, str, str]] = set()
    for lease in lease_rows:
        pid = str(lease.get("project_id") or "").strip()
        aid = _account_id(lease)
        rid = str(lease.get("run_id") or getattr(ctx, "run_id", "") or "").strip()
        slot = normalize_account_slot(lease.get("slot"))
        key = (pid, aid, slot)
        if not pid or not aid or key in seen:
            continue
        seen.add(key)
        from mino_nexus.records.write_state import protected
        if protected(rid, sn=str(lease.get("sn") or getattr(ctx, "sn", "") or "")):
            continue
        try:
            _clear_leased(pid, aid, rid, account_slot=slot)
        except Exception as exc:
            SLog.w(TAG, f"release lease failed: {exc!r}")
    ctx.leased_accounts = []
    ctx.leased_account_leases = []
    ctx.leased_account_by_slot = {}
    ctx.resource_lease = {}
    ctx.picked_account = {}
    ctx.accounts_brief = ""

def release_run_lease(run_id: str, *, app_id: str = "") -> int:
    """run 结束：按 run_id 清空号池占用（兜底，幂等）。返回释放条数。"""
    from mino_nexus.resources.pool_account_store import clear_leases_for_run

    rid = str(run_id or "").strip()
    if not rid:
        return 0
    project_ids: list[str] = []
    if app_id:
        try:
            app = ps.require_app(app_id)
            pid = str(app.get("project_id") or "").strip()
            if pid:
                project_ids.append(pid)
        except KeyError:
            pass
    if not project_ids:
        for proj in ps.list_projects():
            pid = str(proj.get("id") or "")
            if pid:
                project_ids.append(pid)
    seen: set[str] = set()
    total = 0
    for pid in project_ids:
        if pid in seen:
            continue
        seen.add(pid)
        try:
            n = clear_leases_for_run(pid, rid)
            if n:
                total += n
                SLog.i(TAG, f"released {n} lease(s) run={rid[:12]} project={pid[:8]}")
        except Exception as exc:
            SLog.w(TAG, f"release_run_lease project={pid[:8]}: {exc!r}")
    return total
