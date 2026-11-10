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
  const row = await db
    .prepare(`SELECT node_token FROM scouts WHERE scout_id = ?`)
    .bind(scout_id)
    .first<{ node_token: string }>();
  return row?.node_token ?? null;
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

export async function markScoutOffline(db: D1Database, scout_id: string, now: number): Promise<void> {
  await db.batch([
    db.prepare(`UPDATE scouts SET online=0, last_seen_ms=? WHERE scout_id=?`).bind(now, scout_id),
    db.prepare(`UPDATE devices SET online=0, updated_at_ms=? WHERE scout_id=?`).bind(now, scout_id),
  ]);
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
      `SELECT scout_id, online, last_seen_ms, scout_version, host_json, owner_user_id FROM scouts ORDER BY scout_id`,
    )
    .all<Omit<ScoutRow, "node_token">>();

  return (results || []).map((r) => {
    const last = Number(r.last_seen_ms || 0);
    const online = Number(r.online) === 1 && now - last <= OFFLINE_AFTER_MS;
    return {
      scout_id: r.scout_id,
      online,
      last_seen_ms: last,
      scout_version: r.scout_version ?? null,
      host: r.host_json ? JSON.parse(String(r.host_json)) : null,
      owner_user_id: r.owner_user_id ?? null,
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
