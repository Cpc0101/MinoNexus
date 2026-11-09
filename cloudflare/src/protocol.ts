/** Minimal Scout↔Nexus wire types for the Cloudflare free hub. Not a port of legacy protocol.py. */

export type ScoutMsgType = "REGISTER" | "HEARTBEAT";
export type NexusMsgType = "REGISTERED" | "ERROR";

export type DeviceSnapshot = {
  device_id: string;
  platform?: string;
  label?: string;
  ready?: boolean;
  meta?: Record<string, unknown>;
};

export type RegisterMsg = {
  type: "REGISTER";
  scout_id: string;
  token: string;
  scout_version?: string;
  host?: Record<string, unknown>;
  devices?: DeviceSnapshot[];
};

export type HeartbeatMsg = {
  type: "HEARTBEAT";
  scout_id?: string;
  devices?: DeviceSnapshot[];
  device_delta?: DeviceSnapshot[];
};

export type RegisteredMsg = {
  type: "REGISTERED";
  scout_id: string;
  node_token: string;
  offline_after_ms: number;
};

export type ErrorMsg = {
  type: "ERROR";
  code: string;
  message: string;
};

export type ScoutInbound = RegisterMsg | HeartbeatMsg;
export type NexusOutbound = RegisteredMsg | ErrorMsg;

export const OFFLINE_AFTER_MS = 45_000;

export function parseScoutJson(raw: string): ScoutInbound | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const type = (data as { type?: unknown }).type;
  if (type === "REGISTER" || type === "HEARTBEAT") {
    return data as ScoutInbound;
  }
  return null;
}

export function normalizeDevices(
  list: DeviceSnapshot[] | undefined,
): DeviceSnapshot[] {
  if (!Array.isArray(list)) return [];
  const out: DeviceSnapshot[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const device_id = String(item.device_id || "").trim();
    if (!device_id) continue;
    out.push({
      device_id,
      platform: item.platform ? String(item.platform) : undefined,
      label: item.label ? String(item.label) : undefined,
      ready: Boolean(item.ready),
      meta: item.meta && typeof item.meta === "object" ? item.meta : undefined,
    });
  }
  return out;
}
