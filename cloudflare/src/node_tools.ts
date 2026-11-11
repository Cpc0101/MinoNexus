/**
 * Mirror mino_nexus.resources.node_tools.tools — same catalog + RPC shape.
 */

import type { Env } from "./env";
import { hubStub } from "./env";
import { getScoutContract, saveToolSnapshot } from "./db";

/** Same fixed catalog as mino_nexus.resources.node_tools.TOOLS */
export const TOOLS: Array<[string, string, string]> = [
  ["scout.core", "Scout 核心", "节点运行"],
  ["chromium", "Chromium", "Web 自动化"],
  ["adb.keyboard", "ADB Keyboard", "Android 文本输入"],
  ["appium", "Appium", "移动端自动化"],
  ["cli.feishu", "飞书 CLI", "工作项接入"],
  ["cli.meego", "Meego CLI", "工作项接入"],
];

function catalogUnknown(detail: string) {
  return TOOLS.map(([tool_id, label, purpose]) => ({
    tool_id,
    label,
    purpose,
    state: "unknown",
    detail,
  }));
}

export async function readNodeTools(
  env: Env,
  nodeId: string,
  userId: string,
): Promise<Record<string, unknown>> {
  const scout = await getScoutContract(env.DB, nodeId);
  const online = Boolean(scout?.online);
  const version = Number(scout?.tool_tasks_version || 0);
  let stale = true;
  let items: unknown[] | null = null;
  let reported_at: number | undefined;

  if (online && (version === 1 || version === 2)) {
    try {
      const rpc = await hubStub(env).fetch("https://hub/rpc", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          scout_id: nodeId,
          capability_id: "node.tools",
          params: {},
          timeout_ms: 12000,
        }),
      });
      const body = (await rpc.json()) as {
        ok?: boolean;
        extra?: Record<string, unknown>;
        data?: Record<string, unknown>;
      };
      if (rpc.status === 200) {
        const merged = {
          ...(body.data && typeof body.data === "object" ? body.data : {}),
          ...(body.extra && typeof body.extra === "object" ? body.extra : {}),
        };
        const snapshot = merged.tools;
        if (
          snapshot &&
          typeof snapshot === "object" &&
          [1, 2].includes(Number((snapshot as { version?: number }).version)) &&
          Array.isArray((snapshot as { items?: unknown }).items)
        ) {
          const list = (snapshot as { items: unknown[] }).items;
          const at = Date.now();
          await saveToolSnapshot(env.DB, nodeId, userId, list, at);
          items = list;
          reported_at = Math.floor(at / 1000);
          stale = false;
        }
      }
    } catch {
      /* fall through to cached / catalog */
    }
  }

  if (!items) {
    const cached = scout?.tool_snapshot;
    if (
      cached &&
      cached.owner_user_id === userId &&
      Array.isArray(cached.items) &&
      cached.items.length
    ) {
      items = cached.items;
      reported_at = cached.reported_at
        ? Math.floor(Number(cached.reported_at) / 1000)
        : undefined;
    } else if (online && !(version === 1 || version === 2)) {
      items = catalogUnknown("当前 Scout 未上报下载中心合同，请手动升级");
    } else {
      items = catalogUnknown(
        "节点尚未上报此状态，需要支持下载中心的新版本 Scout",
      );
    }
  }

  return {
    node_id: nodeId,
    online,
    supported: Boolean(scout && (version === 1 || version === 2)),
    contract_version: scout ? version : 0,
    stale,
    reported_at,
    items,
    tasks: [],
  };
}
