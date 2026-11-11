/** Shared Scout EXECUTE via Durable Object hub — match Nexus live.send. */

import type { Env } from "./env";
import { hubStub } from "./env";

export type ScoutRpcResult = {
  ok: boolean;
  status: string;
  error: string;
  summary?: string;
  extra: Record<string, unknown>;
  data: Record<string, unknown>;
  http: number;
};

export async function scoutExecute(
  env: Env,
  args: {
    scout_id: string;
    capability_id: string;
    params?: Record<string, unknown>;
    timeout_ms?: number;
    /** Extra Execute payload fields (sn/platform/executor_order/…). */
    execute?: Record<string, unknown>;
  },
): Promise<ScoutRpcResult> {
  const timeout_ms = Math.min(Math.max(Number(args.timeout_ms || 8000), 1000), 700_000);
  const rpc = await hubStub(env).fetch("https://hub/rpc", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      scout_id: args.scout_id,
      capability_id: args.capability_id,
      params: args.params || {},
      timeout_ms,
      execute: args.execute || {},
    }),
  });
  let body: Record<string, unknown> = {};
  try {
    body = (await rpc.json()) as Record<string, unknown>;
  } catch {
    body = { error: "bad_rpc_json" };
  }
  return {
    ok: Boolean(body.ok) && rpc.status === 200,
    status: String(body.status || ""),
    error: String(body.error || ""),
    summary: body.summary ? String(body.summary) : undefined,
    extra:
      body.extra && typeof body.extra === "object"
        ? (body.extra as Record<string, unknown>)
        : {},
    data:
      body.data && typeof body.data === "object"
        ? (body.data as Record<string, unknown>)
        : {},
    http: rpc.status,
  };
}

export function merged(result: ScoutRpcResult): Record<string, unknown> {
  return { ...result.data, ...result.extra };
}
