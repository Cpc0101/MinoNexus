import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import {
  OFFLINE_AFTER_MS,
  devicesFromHeartbeatPayload,
  devicesFromRegisterPayload,
  envelope,
  hostFromPayload,
  parseScoutJson,
  registeredOk,
  registeredReject,
  type NexusOutbound,
} from "./protocol";
import {
  getScoutNodeToken,
  markScoutOffline,
  replaceDevices,
  touchScout,
  upsertDevices,
  upsertScoutOnline,
} from "./db";
import { consumeInstallToken } from "./install";

type ScoutAttachment = {
  scout_id: string;
  node_token: string;
  connection_generation: string;
};

function send(ws: WebSocket, msg: NexusOutbound): void {
  ws.send(JSON.stringify(msg));
}

function randomToken(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function connectionGeneration(): string {
  return `conn_${randomToken().slice(0, 16)}`;
}

/** Durable Object only owns hibernated Scout WebSockets. Scout/device rows live in D1. */
export class NexusHub extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

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
      return;
    }

    const msg = parseScoutJson(message);
    if (!msg) {
      return;
    }

    if (msg.type === "REGISTER") {
      await this.onRegister(ws, msg.msg_id, msg.payload);
      return;
    }

    if (msg.type === "HEARTBEAT") {
      await this.onHeartbeat(ws, msg.payload);
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    if (att?.scout_id) {
      await markScoutOffline(this.env.DB, att.scout_id, Date.now());
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
      await markScoutOffline(this.env.DB, att.scout_id, Date.now());
    }
  }

  private async onRegister(
    ws: WebSocket,
    msgId: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    const scout_id = String(payload.node_id || payload.scout_id || "").trim();
    const token = String(payload.token || "").trim();

    if (!scout_id) {
      send(ws, registeredReject(msgId, "node_id required"));
      try {
        ws.close(1008, "missing_node_id");
      } catch {
        /* ignore */
      }
      return;
    }

    if (Number(payload.account_isolation_version || 0) !== 1) {
      send(ws, registeredReject(msgId, "请升级 Scout：当前客户端未支持账号隔离"));
      try {
        ws.close(1008, "account_isolation");
      } catch {
        /* ignore */
      }
      return;
    }

    if (Number(payload.platform_contract_version || 0) !== 1) {
      send(ws, registeredReject(msgId, "平台合同无效，请升级 Scout 至平台合同 v1"));
      try {
        ws.close(1008, "platform_contract");
      } catch {
        /* ignore */
      }
      return;
    }

    const existingToken = await getScoutNodeToken(this.env.DB, scout_id);
    const pairOk = token === this.env.SCOUT_PAIR_TOKEN;
    const nodeOk = Boolean(existingToken && existingToken === token);
    const install = !pairOk && !nodeOk ? await consumeInstallToken(this.env.DB, token, scout_id) : null;

    if (!pairOk && !nodeOk && !install) {
      send(ws, registeredReject(msgId, "节点凭证或归属无效"));
      try {
        ws.close(1008, "unauthorized");
      } catch {
        /* ignore */
      }
      return;
    }

    const node_token = existingToken || randomToken();
    const owner_user_id = install?.user_id || (pairOk ? "cf-pair" : "");
    const now = Date.now();
    const host = hostFromPayload(payload);
    await upsertScoutOnline(this.env.DB, {
      scout_id,
      node_token,
      now,
      scout_version: payload.scout_version ? String(payload.scout_version) : null,
      host_json: host ? JSON.stringify(host) : null,
      owner_user_id: owner_user_id || null,
    });

    await replaceDevices(this.env.DB, scout_id, devicesFromRegisterPayload(payload), now, true);

    const generation = connectionGeneration();
    ws.serializeAttachment({
      scout_id,
      node_token,
      connection_generation: generation,
    } satisfies ScoutAttachment);

    send(
      ws,
      registeredOk(msgId, {
        node_token,
        owner_user_id: owner_user_id || "cf-pair",
        connection_generation: generation,
      }),
    );
  }

  private async onHeartbeat(ws: WebSocket, payload: Record<string, unknown>): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    const scout_id = att?.scout_id || String(payload.node_id || "").trim();
    if (!scout_id || !att) {
      return;
    }

    const now = Date.now();
    await touchScout(this.env.DB, scout_id, now);

    const delta = devicesFromHeartbeatPayload(payload);
    if (delta.length) {
      await upsertDevices(this.env.DB, scout_id, delta, now);
    }

    // Nexus may ignore heartbeat replies; keep silent.
    void envelope;
  }
}
