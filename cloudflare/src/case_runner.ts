/**
 * Validate and persist a run before asynchronous Scout submission.
 * Detailed records stay on Scout; the scheduler reconciles summary indexes.
 */

import type { Env } from "./env";
import { listDevices } from "./db";
import { scoutExecute, merged } from "./rpc";
import { getRepo, publicUrl } from "./script_repo";
import { getRepoApps, hasApp, keyForApp } from "./repo_apps";
import { listRequirements, requirementIdOk } from "./requirements";
import { findRunByRequest, newCaseRow, type RunDoc } from "./run_store";
import { enqueueRun, deviceAvailability, TaskError } from "./task_scheduler";

const MAX_CASES = 100;
const CASE_ID = /^[A-Za-z0-9_.\-]{1,120}$/;
const REVISION = /^[A-Za-z0-9][A-Za-z0-9._\/\-]{0,119}$/;
const PLATFORMS = new Set(["android", "ios", "web", "mac", "win"]);

export type StartRunArgs = {
  userId: string;
  app_id: string;
  sn?: string;
  case_ids?: unknown;
  script_targets?: unknown;
  revision?: string;
  env_profile?: string;
  env_surface?: string;
  project_id?: string;
  client_request_id?: string;
  waitUntil: (promise: Promise<unknown>) => void;
};

export type StartRunResult =
  | { ok: true; task_id: string; run_id: string; status: string; total: number; sn: string; reused: boolean }
  | { ok: false; status: number; reason_code: string; msg: string };

type DeviceView = {
  device_id: string;
  scout_id: string;
  platform: string | null;
  ready: boolean;
  online: boolean;
  meta: Record<string, unknown> | null;
};

function refuse(status: number, reason_code: string, msg: string): StartRunResult {
  return { ok: false, status, reason_code, msg };
}

function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function newRunId(): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
  return `run_${stamp}_${randomHex(3).toUpperCase()}`;
}

export function mapScoutStatus(value: string): string {
  const text = String(value || "").toLowerCase();
  if (["pass", "passed", "ok", "success"].includes(text)) return "pass";
  if (["cancelled", "canceled"].includes(text)) return "cancelled";
  if (text === "blocked") return "blocked";
  if (text === "declined") return "declined";
  if (["skipped", "skip"].includes(text)) return "skipped";
  return "fail";
}

type ScriptTarget = { requirement_id: string; case_ids: string[] };

function parseScriptTargets(raw: unknown): ScriptTarget[] | string | null {
  if (raw == null) return null;
  if (!Array.isArray(raw) || !raw.length) return "script_targets 需要至少一条需求";
  const out: ScriptTarget[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return "script_targets 格式不对";
    const row = item as Record<string, unknown>;
    const requirement_id = String(row.requirement_id || "").trim();
    if (!requirementIdOk(requirement_id)) {
      return "需求 ID 须以字母开头，只含字母、数字、下划线或连字符，最长 64 位";
    }
    const ids = cleanCaseIds(row.case_ids);
    if (typeof ids === "string") return ids;
    out.push({ requirement_id, case_ids: ids });
  }
  return out;
}

function cleanCaseIds(raw: unknown): string[] | string {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    const id = String(item ?? "").trim();
    if (!id) continue;
    if (!CASE_ID.test(id)) return `case_id ${JSON.stringify(id)} 不合法：只允许字母、数字、下划线、点和连字符`;
    if (!out.includes(id)) out.push(id);
  }
  if (out.length > MAX_CASES) return `一次最多执行 ${MAX_CASES} 条用例`;
  return out;
}

function deviceHint(
  device: DeviceView,
  platform: string,
  env: string,
  appId: string,
  appKey: string,
): Record<string, unknown> {
  const hint: Record<string, unknown> = { env, app_id: appId };
  if (appKey) hint.app_key = appKey;
  if (platform === "android") {
    const channels =
      device.meta && typeof device.meta.channels === "object" && device.meta.channels
        ? (device.meta.channels as Record<string, unknown>)
        : {};
    hint.adb_serial = String(channels.serial || "") || device.device_id;
    if (device.meta && device.meta.model) hint.model = String(device.meta.model);
  } else if (platform === "ios") {
    hint.udid = device.device_id;
  }
  return hint;
}

async function resolveDevice(
  env: Env,
  userId: string,
  sn: string,
  appPlatforms: string[],
): Promise<{ device: DeviceView; platform: string } | { error: StartRunResult }> {
  const { results } = await env.DB.prepare(`SELECT scout_id, owner_user_id FROM scouts`).all<{
    scout_id: string;
    owner_user_id: string | null;
  }>();
  const owned = new Set(
    (results || [])
      .filter((s) => s.owner_user_id === userId)
      .map((s) => s.scout_id),
  );
  const devices = (await listDevices(env.DB, false)) as DeviceView[];
  const platformOk = (d: DeviceView) =>
    !appPlatforms.length || !d.platform || appPlatforms.includes(String(d.platform));

  let picked: DeviceView | undefined;
  if (sn) {
    const same = devices.filter((d) => d.device_id === sn);
    if (!same.length) return { error: refuse(404, "device_not_found", "没有这个设备") };
    const mine = same.filter((d) => owned.has(d.scout_id));
    if (!mine.length) return { error: refuse(403, "not_owned", "设备不属于当前账号") };
    picked = mine.find((d) => d.online) || mine[0];
    if (!picked.online) return { error: refuse(409, "node_offline", "设备所在节点不在线") };
    if (!picked.ready) return { error: refuse(409, "device_not_ready", "设备尚未就绪") };
    if (!platformOk(picked)) {
      return {
        error: refuse(
          422,
          "platform_mismatch",
          `设备平台 ${picked.platform} 与应用平台 ${appPlatforms.join("/")} 不一致`,
        ),
      };
    }
  } else {
    picked = devices.find(
      (d) => owned.has(d.scout_id) && d.online && d.ready && platformOk(d),
    );
    if (!picked) return { error: refuse(409, "device_unavailable", "没有可用设备，请指定 sn 或先让节点上线") };
  }
  const platform = String(picked.platform || (appPlatforms.length === 1 ? appPlatforms[0] : ""));
  return { device: picked, platform };
}

async function listRequirementScripts(
  env: Env,
  args: {
    scout_id: string;
    sn: string;
    platform: string;
    run_id: string;
    repo_url: string;
    branch: string;
    revision: string;
    requirement_id: string;
  },
): Promise<{ ok: true; cases: Array<{ case_id: string; script_path: string }> } | { ok: false; error: StartRunResult }> {
  const out = await scoutExecute(env, {
    scout_id: args.scout_id,
    capability_id: "script.list",
    timeout_ms: 200_000,
    params: {
      repo_url: args.repo_url,
      branch: args.branch,
      revision: args.revision,
      requirement_id: args.requirement_id,
    },
    execute: { run_id: args.run_id, sn: args.sn, platform: args.platform },
  });
  if (out.http !== 200 || (out.status && out.status.toLowerCase() !== "pass")) {
    const summary = out.summary || out.error || "列出脚本失败";
    return { ok: false, error: refuse(422, "script_list_failed", summary.slice(0, 240)) };
  }
  const detail = merged(out);
  const block = detail.script_list && typeof detail.script_list === "object"
    ? (detail.script_list as Record<string, unknown>)
    : {};
  const rows = Array.isArray(block.cases) ? block.cases : [];
  const cases: Array<{ case_id: string; script_path: string }> = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const item = row as Record<string, unknown>;
    const case_id = String(item.case_id || "").trim();
    const script_path = String(item.script_path || "").trim();
    if (case_id && script_path) cases.push({ case_id, script_path });
  }
  return { ok: true, cases };
}

export async function startRun(env: Env, args: StartRunArgs): Promise<StartRunResult> {
  const db = env.DB;
  const userId = String(args.userId || "").trim();
  const appRef = String(args.app_id || "").trim();
  if (!appRef) return refuse(422, "app_id_missing", "缺少 app_id");

  const targets = parseScriptTargets(args.script_targets);
  if (typeof targets === "string") return refuse(422, "script_targets_invalid", targets);
  const caseIds = cleanCaseIds(args.case_ids);
  if (typeof caseIds === "string") return refuse(422, "case_ids_invalid", caseIds);
  if (!targets && !caseIds.length) {
    return refuse(
      422,
      "case_ids_missing",
      "请传 script_targets（需求 ID + 用例 ID）或 case_ids。hub 不从用例正文展开",
    );
  }

  const revision = String(args.revision || "").trim();
  if (revision && !REVISION.test(revision)) {
    return refuse(422, "revision_invalid", "revision 必须是 commit SHA 或分支/标签名");
  }

  const requestFingerprint = JSON.stringify({ app: appRef, sn: String(args.sn || ""), case_ids: caseIds, script_targets: targets, revision,
    project: String(args.project_id || ""), env_profile: String(args.env_profile || "test"), env_surface: String(args.env_surface || "") });
  const reused = await findRunByRequest(db, userId, String(args.client_request_id || ""));
  if (reused) {
    if (reused.request_fingerprint && reused.request_fingerprint !== requestFingerprint) return refuse(409, "request_conflict", "相同 client_request_id 不能用于不同任务参数");
    return {
      ok: true,
      task_id: reused.run_id,
      run_id: reused.run_id,
      status: reused.status,
      total: reused.cases.length,
      sn: reused.sn,
      reused: true,
    };
  }

  const app = await db
    .prepare(
      `SELECT app_id, project_id, name, platform FROM apps
       WHERE owner_user_id = ? AND (app_id = ? OR name = ?)
       ORDER BY (app_id = ?) DESC LIMIT 1`,
    )
    .bind(userId, appRef, appRef, appRef)
    .first<{ app_id: string; project_id: string; name: string; platform: string | null }>();
  if (!app) return refuse(404, "app_not_found", "没有这个应用");
  const wantedProject = String(args.project_id || "").trim();
  if (wantedProject && wantedProject !== app.project_id) {
    return refuse(422, "project_scope_invalid", "应用不属于指定项目");
  }
  const projectId = app.project_id;

  const repo = await getRepo(db, projectId);
  if (!repo.url.trim()) {
    return refuse(422, "script_repo_missing", "项目还没有配置用例脚本仓库");
  }
  const manifest = await getRepoApps(db, projectId);
  if (Object.keys(manifest.apps).length && !(await hasApp(db, projectId, app.app_id))) {
    return refuse(422, "app_not_in_repo_config", "脚本仓库的 config.yaml 里没有这个应用，请先 repo_apps_sync");
  }
  const appKey = await keyForApp(db, projectId, app.app_id);

  const appPlatforms = String(app.platform || "")
    .split(",")
    .map((p) => p.trim().toLowerCase())
    .filter((p) => PLATFORMS.has(p));
  const resolved = await resolveDevice(env, userId, String(args.sn || "").trim(), appPlatforms);
  if ("error" in resolved) return resolved.error;
  const { device, platform } = resolved;
  const scout = await db.prepare("SELECT record_api_version FROM scouts WHERE scout_id=?").bind(device.scout_id).first<{ record_api_version: number }>();
  if (Number(scout?.record_api_version || 0) < 1) return refuse(409, "record_api_required", "目标 Scout 尚不支持持久任务记录，请先升级 Scout");
  const views = await deviceAvailability(env, userId, { only_online: false, app_id: app.app_id });
  const view = views.find(row => row.sn === device.device_id && row.scout_id === device.scout_id);
  if (!view?.executable) return refuse(409, String(view?.blocking_code || "device_unavailable"), "设备当前不能接受任务");

  const runId = newRunId();
  let planned: Array<{ case_id: string; script_path: string }> = caseIds.map((id) => ({
    case_id: id,
    script_path: "",
  }));
  if (targets) {
    const known = new Map((await listRequirements(db, projectId)).map((row) => [row.id, row]));
    planned = [];
    const seen = new Set<string>();
    for (const target of targets) {
      if (!known.has(target.requirement_id)) {
        return refuse(422, "requirement_missing", `需求 ${target.requirement_id} 还没有登记`);
      }
      let ids = target.case_ids;
      const listed = new Map<string, string>();
      if (!ids.length) {
        const found = await listRequirementScripts(env, {
          scout_id: device.scout_id,
          sn: device.device_id,
          platform,
          run_id: runId,
          repo_url: publicUrl(repo.url),
          branch: repo.branch,
          revision,
          requirement_id: target.requirement_id,
        });
        if (!found.ok) return found.error;
        if (!found.cases.length) {
          return refuse(422, "script_missing", `需求 ${target.requirement_id} 的目录里没有脚本`);
        }
        ids = found.cases.map((item) => item.case_id);
        for (const item of found.cases) listed.set(item.case_id, item.script_path);
      }
      for (const id of ids) {
        if (seen.has(id)) return refuse(422, "case_ids_invalid", `用例 ${id} 在本次下发里重复`);
        seen.add(id);
        planned.push({
          case_id: id,
          script_path: listed.get(id) || `tests/${target.requirement_id}/${id}.py`,
        });
      }
    }
    if (planned.length > MAX_CASES) {
      return refuse(422, "case_ids_invalid", `一次最多执行 ${MAX_CASES} 条用例`);
    }
  }
  const envProfile = String(args.env_profile || "").trim() || "test";
  const now = new Date().toISOString();
  const doc: RunDoc = {
    run_id: runId,
    task_id: runId,
    client_request_id: String(args.client_request_id || "").trim().slice(0, 160),
    request_fingerprint: requestFingerprint,
    owner_user_id: userId,
    project_id: projectId,
    app_id: app.app_id,
    app_name: app.name,
    app_key: appKey,
    scout_id: device.scout_id,
    sn: device.device_id,
    platform,
    env_profile: envProfile,
    env_surface: String(args.env_surface || "").trim(),
    revision,
    repo_url: publicUrl(repo.url),
    repo_branch: repo.branch,
    status: "queued",
    error: "",
    reason_code: "",
    started_at: now,
    finished_at: null,
    cases: planned.map((item) => {
      const row = newCaseRow(item.case_id, device.device_id, `ses_${randomHex(12)}`);
      row.script_path = item.script_path;
      return row;
    }),
  };
  try {
    const queued = await enqueueRun(env, doc, deviceHint(device, platform, envProfile, app.app_id, appKey));
    return { ok: true, task_id: queued.run_id, run_id: queued.run_id, status: queued.status, total: queued.cases.length, sn: queued.sn, reused: queued.run_id !== runId };
  } catch (err) {
    if (err instanceof TaskError) return refuse(err.http, err.code, err.message);
    throw err;
  }
}
