import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import {
  OFFLINE_AFTER_MS,
  normalizeDevices,
  parseScoutJson,
  type DeviceSnapshot,
  type NexusOutbound,
} from "./protocol";
import { ensureSchema } from "./sql";

type ScoutAttachment = {
  scout_id: string;
  node_token: string;
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function send(ws: WebSocket, msg: NexusOutbound): void {
  ws.send(JSON.stringify(msg));
}

function randomToken(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class NexusHub extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ctx.blockConcurrencyWhile(async () => {
      ensureSchema(this.ctx.storage.sql);
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/internal/scouts") {
      return json(this.listScouts());
    }
    if (url.pathname === "/internal/devices") {
      const onlineOnly = url.searchParams.get("online_only") === "1";
      return json(this.listDevices(onlineOnly));
    }

    if (url.pathname === "/node") {
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return new Response("Expected WebSocket", { status: 426 });
      }
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      this.ctx.acceptWebSocket(server);
      return new Response(null, { status: 101, webSocket: client });
    }

    return new Response("Not found", { status: 404 });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (typeof message !== "string") {
      send(ws, { type: "ERROR", code: "binary_unsupported", message: "text JSON only" });
      return;
    }

    const msg = parseScoutJson(message);
    if (!msg) {
      send(ws, { type: "ERROR", code: "bad_json", message: "invalid scout message" });
      return;
    }

    if (msg.type === "REGISTER") {
      await this.onRegister(ws, msg.scout_id, msg.token, msg.scout_version, msg.host, msg.devices);
      return;
    }

    if (msg.type === "HEARTBEAT") {
      await this.onHeartbeat(ws, msg.scout_id, msg.devices, msg.device_delta);
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    if (att?.scout_id) {
      this.markScoutOffline(att.scout_id);
    }
    try {
      ws.close(code, reason);
    } catch {
      /* already closed */
    }
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    if (att?.scout_id) {
      this.markScoutOffline(att.scout_id);
    }
  }

  private async onRegister(
    ws: WebSocket,
    scoutIdRaw: string,
    token: string,
    scoutVersion: string | undefined,
    host: Record<string, unknown> | undefined,
    devices: DeviceSnapshot[] | undefined,
  ): Promise<void> {
    const scout_id = String(scoutIdRaw || "").trim();
    if (!scout_id) {
      send(ws, { type: "ERROR", code: "missing_scout_id", message: "scout_id required" });
      ws.close(1008, "missing_scout_id");
      return;
    }

    const existing = this.getScout(scout_id);
    const pairOk = token === this.env.SCOUT_PAIR_TOKEN;
    const nodeOk = Boolean(existing && existing.node_token === token);
    if (!pairOk && !nodeOk) {
      send(ws, { type: "ERROR", code: "unauthorized", message: "invalid token" });
      ws.close(1008, "unauthorized");
      return;
    }

    const node_token = existing?.node_token || randomToken();
    const now = Date.now();
    this.ctx.storage.sql.exec(
      `INSERT INTO scouts (scout_id, node_token, online, last_seen_ms, scout_version, host_json, meta_json)
       VALUES (?, ?, 1, ?, ?, ?, NULL)
       ON CONFLICT(scout_id) DO UPDATE SET
         node_token=excluded.node_token,
         online=1,
         last_seen_ms=excluded.last_seen_ms,
         scout_version=excluded.scout_version,
         host_json=excluded.host_json`,
      scout_id,
      node_token,
      now,
      scoutVersion ?? null,
      host ? JSON.stringify(host) : null,
    );

    this.replaceDevices(scout_id, normalizeDevices(devices), now, true);
    ws.serializeAttachment({ scout_id, node_token } satisfies ScoutAttachment);
    send(ws, {
      type: "REGISTERED",
      scout_id,
      node_token,
      offline_after_ms: OFFLINE_AFTER_MS,
    });
  }

  private async onHeartbeat(
    ws: WebSocket,
    scoutIdHint: string | undefined,
    devices: DeviceSnapshot[] | undefined,
    deviceDelta: DeviceSnapshot[] | undefined,
  ): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    const scout_id = att?.scout_id || String(scoutIdHint || "").trim();
    if (!scout_id || !att) {
      send(ws, { type: "ERROR", code: "not_registered", message: "REGISTER first" });
      return;
    }

    const now = Date.now();
    this.ctx.storage.sql.exec(
      `UPDATE scouts SET online=1, last_seen_ms=? WHERE scout_id=?`,
      now,
      scout_id,
    );

    const full = normalizeDevices(devices);
    if (full.length) {
      this.replaceDevices(scout_id, full, now, true);
      return;
    }
    const delta = normalizeDevices(deviceDelta);
    if (delta.length) {
      this.upsertDevices(scout_id, delta, now);
    }
  }

  private replaceDevices(
    scout_id: string,
    devices: DeviceSnapshot[],
    now: number,
    markMissingOffline: boolean,
  ): void {
    if (markMissingOffline) {
      this.ctx.storage.sql.exec(
        `UPDATE devices SET online=0, updated_at_ms=? WHERE scout_id=?`,
        now,
        scout_id,
      );
    }
    this.upsertDevices(scout_id, devices, now);
  }

  private upsertDevices(scout_id: string, devices: DeviceSnapshot[], now: number): void {
    for (const d of devices) {
      this.ctx.storage.sql.exec(
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
        d.device_id,
        scout_id,
        d.platform ?? null,
        d.label ?? null,
        d.ready ? 1 : 0,
        d.meta ? JSON.stringify(d.meta) : null,
        now,
      );
    }
  }

  private markScoutOffline(scout_id: string): void {
    const now = Date.now();
    this.ctx.storage.sql.exec(
      `UPDATE scouts SET online=0, last_seen_ms=? WHERE scout_id=?`,
      now,
      scout_id,
    );
    this.ctx.storage.sql.exec(
      `UPDATE devices SET online=0, updated_at_ms=? WHERE scout_id=?`,
      now,
      scout_id,
    );
  }

  private getScout(scout_id: string): { node_token: string } | null {
    const rows = this.ctx.storage.sql
      .exec(`SELECT node_token FROM scouts WHERE scout_id=?`, scout_id)
      .toArray();
    const row = rows[0];
    if (!row) return null;
    return { node_token: String(row.node_token) };
  }

  private listScouts(): unknown[] {
    const now = Date.now();
    const rows = this.ctx.storage.sql
      .exec(
        `SELECT scout_id, online, last_seen_ms, scout_version, host_json FROM scouts ORDER BY scout_id`,
      )
      .toArray();
    return rows.map((r) => {
      const last = Number(r.last_seen_ms || 0);
      const online =
        Number(r.online) === 1 && now - last <= OFFLINE_AFTER_MS;
      return {
        scout_id: r.scout_id,
        online,
        last_seen_ms: last,
        scout_version: r.scout_version ?? null,
        host: r.host_json ? JSON.parse(String(r.host_json)) : null,
      };
    });
  }

  private listDevices(onlineOnly: boolean): unknown[] {
    const now = Date.now();
    const rows = this.ctx.storage.sql
      .exec(
        `SELECT d.device_id, d.scout_id, d.platform, d.label, d.ready, d.online, d.meta_json, d.updated_at_ms,
                s.last_seen_ms AS scout_last_seen_ms, s.online AS scout_online
         FROM devices d
         LEFT JOIN scouts s ON s.scout_id = d.scout_id
         ORDER BY d.device_id`,
      )
      .toArray();

    const out = rows.map((r) => {
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
}
