import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import {
  devicesFromHeartbeatPayload,
  devicesFromRegisterPayload,
  executeEnvelope,
  hostFromPayload,
  parseScoutJson,
  registeredOk,
  registeredReject,
  contractFromPayload,
  type NexusOutbound,
} from "./protocol";
import {
  getScoutAuth,
  mergeHostJson,
  readScoutHost,
  replaceDevices,
  sanitizePlugins,
  updateScoutContract,
  upsertDevices,
  upsertScoutOnline,
} from "./db";
import { consumeInstallToken } from "./install";

type ScoutAttachment = {
  scout_id: string;
  node_token: string;
  connection_generation: string;
  scout_process_generation?: string;
};

type PendingRpc = {
  resolve: (value: Record<string, unknown>) => void;
  reject: (err: Error) => void;
  scout_id: string;
  timer: ReturnType<typeof setTimeout>;
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
  private pending = new Map<string, PendingRpc>();
  private rpcSeq = 0;

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

    if (url.pathname === "/rpc" && request.method === "POST") {
      return this.onRpc(request);
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
      return;
    }

    if (msg.type === "RESULT") {
      this.onResult(msg.reply_to, msg.payload);
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    await this.releaseSocket(ws, "scout_disconnected");
    try {
      ws.close(code, reason);
    } catch {
      /* already closed */
    }
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    await this.releaseSocket(ws, "scout_error");
  }

  /**
   * Only the active connection may release ownership. Generation is stored in
   * DO storage because getWebSockets() is not reliable across hibernation /
   * deploy races.
   *
   * Do NOT flip scouts.online=0 here. Cloudflare WS handshakes flap; immediate
   * offline makes Workbench flicker. listScouts already treats a node offline
   * when last_seen ages past OFFLINE_AFTER_MS — that covers true disconnects
   * without punishing brief reconnects.
   */
  private async releaseSocket(ws: WebSocket, reason: string): Promise<void> {
    const att = ws.deserializeAttachment() as ScoutAttachment | null;
    if (!att?.scout_id || !att.connection_generation) return;
    const key = `active_conn:${att.scout_id}`;
    const active = await this.ctx.storage.get<string>(key);
    if (active && active !== att.connection_generation) {
      return; // retired socket; replacement already owns the scout
    }
    if (this.hasOtherLiveSocket(ws, att.scout_id)) {
      return;
    }
    if (active === att.connection_generation) {
      await this.ctx.storage.delete(key);
    }
    this.rejectPendingForScout(att.scout_id, reason);
  }

  /** True when another hibernated socket still owns this scout (replacement). */
  private hasOtherLiveSocket(self: WebSocket, scout_id: string): boolean {
    for (const other of this.ctx.getWebSockets()) {
      if (other === self) continue;
      const att = other.deserializeAttachment() as ScoutAttachment | null;
      if (att?.scout_id === scout_id) return true;
    }
    return false;
  }

  /** Drop prior sockets for this scout so their close cannot race the new REGISTER. */
  private retireOtherSockets(self: WebSocket, scout_id: string): void {
    for (const other of this.ctx.getWebSockets()) {
      if (other === self) continue;
      const att = other.deserializeAttachment() as ScoutAttachment | null;
      if (att?.scout_id !== scout_id) continue;
      try {
        other.close(1000, "replaced");
      } catch {
        /* ignore */
      }
    }
  }

  private findSocket(scout_id: string): WebSocket | null {
    for (const ws of this.ctx.getWebSockets()) {
      const att = ws.deserializeAttachment() as ScoutAttachment | null;
      if (att?.scout_id === scout_id) return ws;
    }
    return null;
  }

  private rejectPendingForScout(scout_id: string, reason: string): void {
    for (const [id, pending] of this.pending) {
      if (pending.scout_id !== scout_id) continue;
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(new Error(reason));
    }
  }

  private onResult(replyTo: string | null | undefined, payload: Record<string, unknown>): void {
    if (!replyTo) return;
    const pending = this.pending.get(replyTo);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(replyTo);
    pending.resolve(payload);
  }

  private async onRpc(request: Request): Promise<Response> {
    let body: {
      scout_id?: string;
      capability_id?: string;
      params?: Record<string, unknown>;
      timeout_ms?: number;
      execute?: Record<string, unknown>;
    };
    try {
      body = (await request.json()) as typeof body;
    } catch {
      return Response.json({ ok: false, error: "bad_json" }, { status: 400 });
    }
    const scout_id = String(body.scout_id || "").trim();
    const capability_id = String(body.capability_id || "").trim();
    if (!scout_id || !capability_id) {
      return Response.json({ ok: false, error: "scout_id_and_capability_required" }, { status: 400 });
    }
    const ws = this.findSocket(scout_id);
    if (!ws) {
      return Response.json({ ok: false, error: "scout_offline" }, { status: 409 });
    }
    // Plugin install / Scout update can take many minutes — match Nexus timeouts.
    const timeout_ms = Math.min(Math.max(Number(body.timeout_ms || 8000), 1000), 700_000);
    const envMsg = executeEnvelope({
      capability_id,
      params: body.params || {},
      timeout_sec: Math.ceil(timeout_ms / 1000),
    });
    // Durable Object isolate: assign auxiliary step here so it cannot stay at sentinel -1.
    // Match mino_nexus.core.wire_identity.assign_execute_identity.
    const step_idx = -(Date.now() * 1000 + (this.rpcSeq = (this.rpcSeq || 0) + 1));
    const payload = envMsg.payload as Record<string, unknown>;
    payload.step_idx = step_idx;
    // Optional Execute fields (sn/platform/executor_order/device_hint) — never send
    // binding contract fields unless caller explicitly needs execution_binding.v1.
    const extra = body.execute && typeof body.execute === "object" ? body.execute : {};
    for (const [key, value] of Object.entries(extra)) {
      if (value === undefined || value === null || value === "") continue;
      if (
        [
          "binding_id",
          "binding_revision",
          "operation_ref",
          "execution_contract_version",
          "node_connection_generation",
          "scout_process_generation",
          "capability_revision",
        ].includes(key)
      ) {
        continue;
      }
      payload[key] = value;
    }
    const msg_id = String(envMsg.msg_id);

    const result = await new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(msg_id);
        reject(new Error("rpc_timeout"));
      }, timeout_ms);
      this.pending.set(msg_id, { resolve, reject, scout_id, timer });
      try {
        send(ws, envMsg);
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(msg_id);
        reject(err instanceof Error ? err : new Error("send_failed"));
      }
    }).catch((err: Error) => ({ __error: err.message }));

    if (result && typeof result === "object" && "__error" in result) {
      return Response.json(
        { ok: false, error: String((result as { __error: string }).__error) },
        { status: 504 },
      );
    }

    const status = String(
      (result as { status?: unknown }).status || "",
    ).toLowerCase();
    const ok = ["pass", "passed", "ok", "success"].includes(status);
    return Response.json({
      ok,
      status,
      summary: (result as { summary?: string }).summary || "",
      error: (result as { error?: string }).error || "",
      extra: (result as { extra?: Record<string, unknown> }).extra || {},
      data: (result as { data?: Record<string, unknown> }).data || {},
    });
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

    const existing = await getScoutAuth(this.env.DB, scout_id);
    const existingToken = existing?.node_token ?? null;
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
    // Reconnect with node_token must return the persisted owner. Falling back to
    // "cf-pair" here made Scout verify_local_owner fail against the real uid and
    // stuck plugins_ready=false → Download Center "等待节点上报安装能力".
    const owner_user_id =
      (install?.user_id || "").trim() ||
      (nodeOk ? String(existing?.owner_user_id || "").trim() : "") ||
      (pairOk ? "cf-pair" : "");
    const now = Date.now();
    const host = hostFromPayload(payload);
    const previousHost = host ? await readScoutHost(this.env.DB, scout_id) : null;
    await upsertScoutOnline(this.env.DB, {
      scout_id,
      node_token,
      now,
      scout_version: payload.scout_version ? String(payload.scout_version) : null,
      host_json: host ? mergeHostJson(previousHost, host) : null,
      owner_user_id: owner_user_id || null,
    });

    const contract = contractFromPayload(payload);
    await updateScoutContract(this.env.DB, scout_id, {
      now,
      plugins: "plugins" in contract ? sanitizePlugins(contract.plugins) : undefined,
      plugins_ready: contract.plugins_ready,
      tool_tasks_version: contract.tool_tasks_version,
      plugin_warning: contract.plugin_warning,
      runtime_environment: contract.runtime_environment,
    });

    await replaceDevices(this.env.DB, scout_id, devicesFromRegisterPayload(payload), now, true);

    const generation = connectionGeneration();
    // Persist active generation before retiring peers so a lagged close from the
    // previous process cannot mark this scout offline after REGISTER.
    await this.ctx.storage.put(`active_conn:${scout_id}`, generation);
    ws.serializeAttachment({
      scout_id,
      node_token,
      connection_generation: generation,
      scout_process_generation: String(payload.scout_process_generation || ""),
    } satisfies ScoutAttachment);
    this.retireOtherSockets(ws, scout_id);

    send(
      ws,
      registeredOk(msgId, {
        node_token,
        owner_user_id: owner_user_id || String(existing?.owner_user_id || "").trim() || "cf-pair",
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
    const host = hostFromPayload(payload);
    const previousHost = host ? await readScoutHost(this.env.DB, scout_id) : null;
    const contract = contractFromPayload(payload);
    await updateScoutContract(this.env.DB, scout_id, {
      now,
      scout_version: payload.scout_version ? String(payload.scout_version) : undefined,
      host_json: host ? mergeHostJson(previousHost, host) : undefined,
      plugins: "plugins" in contract ? sanitizePlugins(contract.plugins) : undefined,
      plugins_ready: contract.plugins_ready,
      tool_tasks_version: contract.tool_tasks_version,
      plugin_warning: contract.plugin_warning,
      runtime_environment: contract.runtime_environment,
      busy: typeof payload.busy === "boolean" ? payload.busy : undefined,
      draining: typeof payload.draining === "boolean" ? payload.draining : undefined,
    });

    if (payload.scout_process_generation) {
      ws.serializeAttachment({
        ...att,
        scout_process_generation: String(payload.scout_process_generation),
      } satisfies ScoutAttachment);
    }

    const delta = devicesFromHeartbeatPayload(payload);
    if (delta.length) {
      await upsertDevices(this.env.DB, scout_id, delta, now);
    }
  }
}
