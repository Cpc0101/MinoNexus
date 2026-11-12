/** Scout↔Nexus wire types aligned with docs/接口/协议.md Envelope framing. */

export const PROTOCOL_VERSION = 2;
export const OFFLINE_AFTER_MS = 45_000;
export const HEARTBEAT_INTERVAL_SEC = 15;

export type DeviceSnapshot = {
  device_id: string;
  platform?: string;
  label?: string;
  ready?: boolean;
  meta?: Record<string, unknown>;
};

export type EnvelopeIn = {
  v?: number;
  type: string;
  msg_id: string;
  ts?: string;
  reply_to?: string | null;
  payload: Record<string, unknown>;
};

export type NexusOutbound = Record<string, unknown>;

function msgId(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function nowIso(): string {
  return new Date().toISOString();
}

const SCOUT_INBOUND = new Set(["REGISTER", "HEARTBEAT", "RESULT", "EXECUTE"]);

export function parseScoutJson(raw: string): EnvelopeIn | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  const type = String(obj.type || "");
  if (!SCOUT_INBOUND.has(type)) return null;
  if (!obj.msg_id || typeof obj.msg_id !== "string") return null;
  const payload =
    obj.payload && typeof obj.payload === "object"
      ? (obj.payload as Record<string, unknown>)
      : {};
  return {
    v: Number(obj.v || PROTOCOL_VERSION),
    type,
    msg_id: obj.msg_id,
    ts: typeof obj.ts === "string" ? obj.ts : undefined,
    reply_to: typeof obj.reply_to === "string" ? obj.reply_to : null,
    payload,
  };
}

/** Match mino_nexus.core.wire_identity.next_auxiliary_step_idx — unique negative, never -1. */
let auxiliaryStep = -Math.floor(Date.now() * 1000);

function nextAuxiliaryStepIdx(): number {
  auxiliaryStep -= 1;
  return auxiliaryStep;
}

/**
 * N→S EXECUTE for management caps (node.tools / …).
 * Match node_tools._rpc + assign_execute_identity: unique auxiliary step_idx,
 * no binding fields (any non-empty binding field makes BindingGuard reject).
 */
export function executeEnvelope(args: {
  capability_id: string;
  params?: Record<string, unknown>;
  timeout_sec?: number;
}): NexusOutbound {
  return {
    v: PROTOCOL_VERSION,
    type: "EXECUTE",
    msg_id: msgId(),
    reply_to: null,
    ts: nowIso(),
    payload: {
      run_id: "",
      step_idx: nextAuxiliaryStepIdx(),
      sn: "",
      capability_id: args.capability_id,
      params: args.params || {},
      timeout_sec: args.timeout_sec ?? 6,
    },
  };
}

/** Plugin / download-centre fields from REGISTER or HEARTBEAT. */
export function contractFromPayload(payload: Record<string, unknown>): {
  plugins?: unknown[] | null;
  plugins_ready?: boolean | null;
  tool_tasks_version?: number;
  plugin_warning?: string;
  runtime_environment?: string;
} {
  const out: {
    plugins?: unknown[] | null;
    plugins_ready?: boolean | null;
    tool_tasks_version?: number;
    plugin_warning?: string;
    runtime_environment?: string;
  } = {};
  if ("plugins" in payload) {
    out.plugins = Array.isArray(payload.plugins) ? payload.plugins : payload.plugins == null ? null : [];
  }
  if ("plugins_ready" in payload) {
    out.plugins_ready =
      typeof payload.plugins_ready === "boolean" ? payload.plugins_ready : null;
  }
  if ("tool_tasks_version" in payload) {
    const v = Number(payload.tool_tasks_version || 0);
    out.tool_tasks_version = v === 1 || v === 2 ? v : 0;
  }
  if ("plugin_warning" in payload) {
    out.plugin_warning = String(payload.plugin_warning || "").slice(0, 200);
  }
  if ("runtime_environment" in payload) {
    out.runtime_environment = String(payload.runtime_environment || "");
  }
  return out;
}

export function envelope(
  type: string,
  payload: Record<string, unknown>,
  replyTo?: string,
): NexusOutbound {
  return {
    v: PROTOCOL_VERSION,
    type,
    msg_id: msgId(),
    reply_to: replyTo || null,
    ts: nowIso(),
    payload,
  };
}

export function registeredOk(
  replyTo: string,
  args: {
    node_token: string;
    owner_user_id: string;
    node_id?: string;
    connection_generation: string;
  },
): NexusOutbound {
  return envelope(
    "REGISTERED",
    {
      accepted: true,
      nexus_version: "0.0.1-cloudflare",
      protocol_version: PROTOCOL_VERSION,
      session_token: `sess-${msgId()}`,
      heartbeat_interval_sec: HEARTBEAT_INTERVAL_SEC,
      reason: "",
      warnings: [],
      node_id: args.node_id || "",
      platform_contract_version: 1,
      node_token: args.node_token,
      owner_user_id: args.owner_user_id,
      node_connection_generation: args.connection_generation,
    },
    replyTo,
  );
}

export function registeredReject(replyTo: string, reason: string): NexusOutbound {
  return envelope(
    "REGISTERED",
    {
      accepted: false,
      nexus_version: "0.0.1-cloudflare",
      protocol_version: PROTOCOL_VERSION,
      reason,
      platform_contract_version: 1,
      warnings: [],
    },
    replyTo,
  );
}

/** Map protocol DeviceManifest list into hub device snapshots. */
export function devicesFromRegisterPayload(payload: Record<string, unknown>): DeviceSnapshot[] {
  const list = Array.isArray(payload.devices) ? payload.devices : [];
  const out: DeviceSnapshot[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const d = item as Record<string, unknown>;
    const device_id = String(d.sn || d.device_id || "").trim();
    if (!device_id) continue;
    const readiness = d.readiness && typeof d.readiness === "object" ? (d.readiness as Record<string, unknown>) : null;
    const channels = d.channels && typeof d.channels === "object" ? (d.channels as Record<string, unknown>) : {};
    const ready =
      readiness?.ready === true ||
      Object.values(channels).some((v) => String(v || "").toLowerCase() === "connected");
    out.push({
      device_id,
      platform: d.platform ? String(d.platform) : undefined,
      label: d.model ? String(d.model) : device_id,
      ready: Boolean(ready),
      meta: {
        os: d.os || undefined,
        channels,
        desktop_state: d.desktop_state || undefined,
      },
    });
  }
  return out;
}

export function devicesFromHeartbeatPayload(payload: Record<string, unknown>): DeviceSnapshot[] {
  const delta = Array.isArray(payload.device_delta) ? payload.device_delta : [];
  if (!delta.length) return [];
  return devicesFromRegisterPayload({ devices: delta });
}

export function hostFromPayload(payload: Record<string, unknown>): Record<string, unknown> | undefined {
  const host = payload.host;
  if (host && typeof host === "object") return host as Record<string, unknown>;
  const hostname = String(payload.hostname || "").trim();
  if (!hostname && !payload.arch) return undefined;
  return {
    hostname: hostname || undefined,
    arch: payload.arch || undefined,
    os: payload.platform || undefined,
  };
}
