/** Shared scout/device persistence on D1. */

import { OFFLINE_AFTER_MS, type DeviceSnapshot } from "./protocol";

export type ScoutRow = {
  scout_id: string;
  online: number;
  last_seen_ms: number;
  scout_version: string | null;
  host_json: string | null;
  node_token: string;
  owner_user_id?: string | null;
};

export type ScoutContract = {
  scout_id: string;
  online: boolean;
  last_seen_ms: number;
  scout_version: string | null;
  owner_user_id: string | null;
  plugins: unknown[] | null;
  plugins_ready: boolean | null;
  tool_tasks_version: number;
  plugin_warning: string;
  runtime_environment: string;
  tool_snapshot: {
    owner_user_id?: string;
    reported_at?: number;
    items?: unknown[];
  } | null;
};

const PLUGIN_CLASSES = new Set(["cli", "mcp", "bot", "mail", "im"]);

/** Match mino_nexus.resources.node_registry.sanitize_plugins (status + non-secret plain). */
export function sanitizePlugins(raw: unknown): unknown[] | null {
  if (raw === undefined || raw === null) return null;
  if (!Array.isArray(raw)) return [];
  const out: Record<string, unknown>[] = [];
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const item = row as Record<string, unknown>;
    const kind = String(item.class || "").trim();
    const pid = String(item.id || "").trim();
    if (!PLUGIN_CLASSES.has(kind) || !pid) continue;
    const next: Record<string, unknown> = {
      class: kind,
      id: pid,
      installed: Boolean(item.installed),
      configured: Boolean(item.configured),
    };
    if (item.label) next.label = String(item.label);
    if (item.status && typeof item.status === "object") next.status = item.status;
    if (item.values && typeof item.values === "object") next.values = item.values;
    if (Array.isArray(item.saved_secrets)) next.saved_secrets = item.saved_secrets;
    if (Array.isArray(item.fields)) next.fields = item.fields;
    if (item.import_blocked != null) next.import_blocked = Boolean(item.import_blocked);
    out.push(next);
  }
  return out;
}

export type DeviceJoinRow = {
  device_id: string;
  scout_id: string;
  platform: string | null;
  label: string | null;
  ready: number;
  online: number;
  meta_json: string | null;
  updated_at_ms: number;
  scout_last_seen_ms: number | null;
  scout_online: number | null;
};

export async function getScoutNodeToken(
  db: D1Database,
  scout_id: string,
): Promise<string | null> {
  const row = await getScoutAuth(db, scout_id);
  return row?.node_token ?? null;
}

/** Token + owner for REGISTER; reconnect must echo the stored owner, not cf-pair. */
export async function getScoutAuth(
  db: D1Database,
  scout_id: string,
): Promise<{ node_token: string; owner_user_id: string | null } | null> {
  const row = await db
    .prepare(`SELECT node_token, owner_user_id FROM scouts WHERE scout_id = ?`)
    .bind(scout_id)
    .first<{ node_token: string; owner_user_id: string | null }>();
  if (!row?.node_token) return null;
  return { node_token: row.node_token, owner_user_id: row.owner_user_id ?? null };
}

export async function upsertScoutOnline(
  db: D1Database,
  args: {
    scout_id: string;
    node_token: string;
    now: number;
    scout_version?: string | null;
    host_json?: string | null;
    owner_user_id?: string | null;
  },
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO scouts (scout_id, node_token, online, last_seen_ms, scout_version, host_json, meta_json, owner_user_id)
       VALUES (?, ?, 1, ?, ?, ?, NULL, ?)
       ON CONFLICT(scout_id) DO UPDATE SET
         node_token=excluded.node_token,
         online=1,
         last_seen_ms=excluded.last_seen_ms,
         scout_version=excluded.scout_version,
         host_json=excluded.host_json,
         owner_user_id=COALESCE(excluded.owner_user_id, scouts.owner_user_id)`,
    )
    .bind(
      args.scout_id,
      args.node_token,
      args.now,
      args.scout_version ?? null,
      args.host_json ?? null,
      args.owner_user_id ?? null,
    )
    .run();
}

export async function touchScout(db: D1Database, scout_id: string, now: number): Promise<void> {
  await db
    .prepare(`UPDATE scouts SET online=1, last_seen_ms=? WHERE scout_id=?`)
    .bind(now, scout_id)
    .run();
}

export async function updateScoutContract(
  db: D1Database,
  scout_id: string,
  args: {
    now: number;
    scout_version?: string | null;
    host_json?: string | null;
    plugins?: unknown[] | null;
    plugins_ready?: boolean | null;
    tool_tasks_version?: number;
    plugin_warning?: string;
    runtime_environment?: string;
  },
): Promise<void> {
  const pluginsJson =
    args.plugins === undefined
      ? undefined
      : args.plugins === null
        ? null
        : JSON.stringify(args.plugins);
  const ready =
    args.plugins_ready === undefined
      ? undefined
      : args.plugins_ready === null
        ? null
        : args.plugins_ready
          ? 1
          : 0;
  const version =
    args.tool_tasks_version === 1 || args.tool_tasks_version === 2
      ? args.tool_tasks_version
      : args.tool_tasks_version === undefined
        ? undefined
        : 0;

  // Build a single UPDATE; only touch columns the heartbeat actually sent.
  const sets = ["online=1", "last_seen_ms=?"];
  const binds: unknown[] = [args.now];
  if (args.scout_version !== undefined) {
    sets.push("scout_version=?");
    binds.push(args.scout_version);
  }
  if (args.host_json !== undefined) {
    sets.push("host_json=?");
    binds.push(args.host_json);
  }
  if (pluginsJson !== undefined) {
    sets.push("plugins_json=?");
    binds.push(pluginsJson);
  }
  if (ready !== undefined) {
    sets.push("plugins_ready=?");
    binds.push(ready);
  }
  if (version !== undefined) {
    sets.push("tool_tasks_version=?");
    binds.push(version);
  }
  if (args.plugin_warning !== undefined) {
    sets.push("plugin_warning=?");
    binds.push(args.plugin_warning || null);
  }
  if (args.runtime_environment !== undefined) {
    sets.push("runtime_environment=?");
    binds.push(args.runtime_environment || null);
  }
  binds.push(scout_id);
  await db
    .prepare(`UPDATE scouts SET ${sets.join(", ")} WHERE scout_id=?`)
    .bind(...binds)
    .run();
}

export async function saveToolSnapshot(
  db: D1Database,
  scout_id: string,
  owner_user_id: string,
  items: unknown[],
  at_ms: number,
): Promise<void> {
  await db
    .prepare(
      `UPDATE scouts SET tool_snapshot_json=?, tool_snapshot_at_ms=?, tool_snapshot_owner=? WHERE scout_id=?`,
    )
    .bind(JSON.stringify(items), at_ms, owner_user_id, scout_id)
    .run();
}

function parseReady(raw: unknown): boolean | null {
  if (raw === null || raw === undefined) return null;
  return Number(raw) === 1;
}

export async function getScoutContract(
  db: D1Database,
  scout_id: string,
): Promise<ScoutContract | null> {
  const row = await db
    .prepare(
      `SELECT scout_id, online, last_seen_ms, scout_version, owner_user_id,
              plugins_json, plugins_ready, tool_tasks_version, plugin_warning, runtime_environment,
              tool_snapshot_json, tool_snapshot_at_ms, tool_snapshot_owner
       FROM scouts WHERE scout_id=?`,
    )
    .bind(scout_id)
    .first<{
      scout_id: string;
      online: number;
      last_seen_ms: number;
      scout_version: string | null;
      owner_user_id: string | null;
      plugins_json: string | null;
      plugins_ready: number | null;
      tool_tasks_version: number | null;
      plugin_warning: string | null;
      runtime_environment: string | null;
      tool_snapshot_json: string | null;
      tool_snapshot_at_ms: number | null;
      tool_snapshot_owner: string | null;
    }>();
  if (!row) return null;
  const now = Date.now();
  const last = Number(row.last_seen_ms || 0);
  const online = Number(row.online) === 1 && now - last <= OFFLINE_AFTER_MS;
  let plugins: unknown[] | null = null;
  if (row.plugins_json) {
    try {
      plugins = JSON.parse(String(row.plugins_json));
    } catch {
      plugins = [];
    }
  }
  let tool_snapshot: ScoutContract["tool_snapshot"] = null;
  if (row.tool_snapshot_json) {
    try {
      tool_snapshot = {
        owner_user_id: row.tool_snapshot_owner || undefined,
        reported_at: row.tool_snapshot_at_ms || undefined,
        items: JSON.parse(String(row.tool_snapshot_json)),
      };
    } catch {
      tool_snapshot = null;
    }
  }
  return {
    scout_id: row.scout_id,
    online,
    last_seen_ms: last,
    scout_version: row.scout_version,
    owner_user_id: row.owner_user_id,
    plugins,
    plugins_ready: parseReady(row.plugins_ready),
    tool_tasks_version: Number(row.tool_tasks_version || 0),
    plugin_warning: row.plugin_warning || "",
    runtime_environment: row.runtime_environment || "",
    tool_snapshot,
  };
}

export async function markScoutOffline(db: D1Database, scout_id: string, now: number): Promise<void> {
  // Only the scout row. listDevices already ANDs device.online with scout online,
  // so UI goes offline immediately. Clearing devices.online here is harmful: a
  // stale WebSocket close after reconnect wipes ios/web/mac, and heartbeats only
  // send device_delta — so those rows stay offline until the next full REGISTER.
  await db
    .prepare(`UPDATE scouts SET online=0, last_seen_ms=? WHERE scout_id=?`)
    .bind(now, scout_id)
    .run();
}

export async function replaceDevices(
  db: D1Database,
  scout_id: string,
  devices: DeviceSnapshot[],
  now: number,
  markMissingOffline: boolean,
): Promise<void> {
  const stmts: D1PreparedStatement[] = [];
  if (markMissingOffline) {
    stmts.push(
      db.prepare(`UPDATE devices SET online=0, updated_at_ms=? WHERE scout_id=?`).bind(now, scout_id),
    );
  }
  for (const d of devices) {
    stmts.push(
      db
        .prepare(
          `INSERT INTO devices (device_id, scout_id, platform, label, ready, online, meta_json, updated_at_ms)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(device_id) DO UPDATE SET
             scout_id=excluded.scout_id,
             platform=excluded.platform,
             label=excluded.label,
             ready=excluded.ready,
             online=1,
             meta_json=excluded.meta_json,
             updated_at_ms=excluded.updated_at_ms`,
        )
        .bind(
          d.device_id,
          scout_id,
          d.platform ?? null,
          d.label ?? null,
          d.ready ? 1 : 0,
          d.meta ? JSON.stringify(d.meta) : null,
          now,
        ),
    );
  }
  if (stmts.length) {
    await db.batch(stmts);
  }
}

export async function upsertDevices(
  db: D1Database,
  scout_id: string,
  devices: DeviceSnapshot[],
  now: number,
): Promise<void> {
  await replaceDevices(db, scout_id, devices, now, false);
}

export async function listScouts(db: D1Database): Promise<unknown[]> {
  const now = Date.now();
  const { results } = await db
    .prepare(
      `SELECT scout_id, online, last_seen_ms, scout_version, host_json, owner_user_id,
              plugins_json, plugins_ready, tool_tasks_version, plugin_warning, runtime_environment
       FROM scouts ORDER BY scout_id`,
    )
    .all<{
      scout_id: string;
      online: number;
      last_seen_ms: number;
      scout_version: string | null;
      host_json: string | null;
      owner_user_id: string | null;
      plugins_json: string | null;
      plugins_ready: number | null;
      tool_tasks_version: number | null;
      plugin_warning: string | null;
      runtime_environment: string | null;
    }>();

  return (results || []).map((r) => {
    const last = Number(r.last_seen_ms || 0);
    const online = Number(r.online) === 1 && now - last <= OFFLINE_AFTER_MS;
    let plugins: unknown[] | null = null;
    if (r.plugins_json) {
      try {
        plugins = JSON.parse(String(r.plugins_json));
      } catch {
        plugins = [];
      }
    }
    return {
      scout_id: r.scout_id,
      online,
      last_seen_ms: last,
      scout_version: r.scout_version ?? null,
      host: r.host_json ? JSON.parse(String(r.host_json)) : null,
      owner_user_id: r.owner_user_id ?? null,
      plugins,
      plugins_ready: parseReady(r.plugins_ready),
      tool_tasks_version: Number(r.tool_tasks_version || 0),
      plugin_warning: r.plugin_warning || "",
      runtime_environment: r.runtime_environment || "",
    };
  });
}

export async function listDevices(db: D1Database, onlineOnly: boolean): Promise<unknown[]> {
  const now = Date.now();
  const { results } = await db
    .prepare(
      `SELECT d.device_id, d.scout_id, d.platform, d.label, d.ready, d.online, d.meta_json, d.updated_at_ms,
              s.last_seen_ms AS scout_last_seen_ms, s.online AS scout_online
       FROM devices d
       LEFT JOIN scouts s ON s.scout_id = d.scout_id
       ORDER BY d.device_id`,
    )
    .all<DeviceJoinRow>();

  const out = (results || []).map((r) => {
    const scoutSeen = Number(r.scout_last_seen_ms || 0);
    const scoutOnline =
      Number(r.scout_online) === 1 && now - scoutSeen <= OFFLINE_AFTER_MS;
    const deviceOnline = scoutOnline && Number(r.online) === 1;
    return {
      device_id: r.device_id,
      scout_id: r.scout_id,
      platform: r.platform ?? null,
      label: r.label ?? null,
      ready: Number(r.ready) === 1,
      online: deviceOnline,
      updated_at_ms: Number(r.updated_at_ms || 0),
      meta: r.meta_json ? JSON.parse(String(r.meta_json)) : null,
    };
  });
  return onlineOnly ? out.filter((d) => d.online) : out;
}
