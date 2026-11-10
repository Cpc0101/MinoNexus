import type { Env } from "./env";
import { listDevices, listScouts } from "./db";

type JsonRpcId = string | number | null;

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: JsonRpcId;
  method?: string;
  params?: Record<string, unknown>;
};

const TOOLS = [
  {
    name: "agent_guide",
    description:
      "Cloudflare free hub: Scout online, device sync, MCP tools. No Workbench / case_run.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "scout_list",
    description: "List Scout nodes known to this hub, with online/last_seen.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "device_list",
    description: "List devices last reported by Scouts (heartbeat / register).",
    inputSchema: {
      type: "object",
      properties: {
        online_only: { type: "boolean", description: "If true, only online devices" },
      },
    },
  },
] as const;

function rpcResult(id: JsonRpcId | undefined, result: unknown): Response {
  return Response.json({ jsonrpc: "2.0", id: id ?? null, result });
}

function rpcError(
  id: JsonRpcId | undefined,
  code: number,
  message: string,
): Response {
  return Response.json({
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code, message },
  });
}

function unauthorized(): Response {
  return new Response("Unauthorized", { status: 401 });
}

export async function handleMcp(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const auth = request.headers.get("authorization") || "";
  const expected = `Bearer ${env.MCP_BEARER_TOKEN}`;
  if (!env.MCP_BEARER_TOKEN || auth !== expected) {
    return unauthorized();
  }

  let body: JsonRpcRequest;
  try {
    body = (await request.json()) as JsonRpcRequest;
  } catch {
    return rpcError(null, -32700, "Parse error");
  }

  const method = String(body.method || "");
  const id = body.id;

  if (method === "initialize") {
    return rpcResult(id, {
      protocolVersion: "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "mino-nexus-cloudflare", version: "0.0.1" },
    });
  }

  if (method === "notifications/initialized" || method === "ping") {
    return rpcResult(id, {});
  }

  if (method === "tools/list") {
    return rpcResult(id, { tools: TOOLS });
  }

  if (method === "tools/call") {
    const name = String(body.params?.name || "");
    const args = (body.params?.arguments || {}) as Record<string, unknown>;
    try {
      const text = await callTool(env, name, args);
      return rpcResult(id, {
        content: [{ type: "text", text }],
        isError: false,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return rpcResult(id, {
        content: [{ type: "text", text: message }],
        isError: true,
      });
    }
  }

  return rpcError(id, -32601, `Method not found: ${method}`);
}

async function callTool(
  env: Env,
  name: string,
  args: Record<string, unknown>,
): Promise<string> {
  if (name === "agent_guide") {
    return JSON.stringify(
      {
        scope: ["scout_online", "mcp", "device_sync"],
        storage: "d1",
        endpoints: {
          health: "GET /health",
          scout_ws: "GET /node (WebSocket)",
          mcp: "POST /mcp",
          scouts: "GET /v1/scouts",
          devices: "GET /v1/devices",
        },
        out_of_scope: [
          "case_run",
          "workbench",
          "script_repo",
          "navigation_atlas",
          "account_leases",
        ],
      },
      null,
      2,
    );
  }

  if (name === "scout_list") {
    return JSON.stringify(await listScouts(env.DB), null, 2);
  }
  if (name === "device_list") {
    const onlineOnly = Boolean(args.online_only);
    return JSON.stringify(await listDevices(env.DB, onlineOnly), null, 2);
  }
  throw new Error(`Unknown tool: ${name}`);
}
