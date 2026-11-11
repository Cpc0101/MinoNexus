import type { Env } from "./env";
import { hubStub } from "./env";
import { handleMcp } from "./mcp";
import { NexusHub } from "./hub";
import { preflight, withCors } from "./cors";
import { enhancedHealth, handleCompat, requireMcpBearer } from "./compat";
import { listDevices, listScouts } from "./db";

export { NexusHub };

function acceptWorkbenchWs(request: Request): Response {
  if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
    return new Response("Expected WebSocket", { status: 426 });
  }
  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();
  server.addEventListener("message", (event) => {
    try {
      const raw = typeof event.data === "string" ? event.data : "";
      const msg = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      if (msg.action === "ping" || msg.type === "ping") {
        server.send(JSON.stringify({ action: "pong", code: 200 }));
      }
    } catch {
      /* ignore non-JSON */
    }
  });
  return new Response(null, { status: 101, webSocket: client });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return preflight(request);
    }

    const url = new URL(request.url);
    let response: Response;

    if (url.pathname === "/health") {
      response = await enhancedHealth(env);
    } else if (url.pathname === "/mcp") {
      response = await handleMcp(request, env);
    } else if (url.pathname === "/node") {
      return hubStub(env).fetch(request);
    } else if (url.pathname === "/ws") {
      return acceptWorkbenchWs(request);
    } else if (url.pathname === "/v1/scouts") {
      const denied = requireMcpBearer(request, env);
      if (denied) return withCors(request, denied);
      response = Response.json(await listScouts(env.DB));
    } else if (url.pathname === "/v1/devices") {
      const denied = requireMcpBearer(request, env);
      if (denied) return withCors(request, denied);
      const onlineOnly = url.searchParams.get("online_only") === "1";
      response = Response.json(await listDevices(env.DB, onlineOnly));
    } else if (url.pathname === "/") {
      response = Response.json({
        service: "mino-nexus-cloudflare",
        version: "0.0.1",
        workbench: true,
        endpoints: {
          health: "GET /health",
          scout_ws: "GET /node (WebSocket)",
          workbench_ws: "GET /ws (WebSocket)",
          mcp: "POST /mcp (OAuth mino_oat_ / manual mino_mcp_)",
          oauth: "GET /.well-known/oauth-* ; /oauth/*",
          auth: "POST /auth/login|/auth/register",
          scouts: "GET /v1/scouts (Bearer MCP_BEARER_TOKEN)",
          devices: "GET /v1/devices (Bearer MCP_BEARER_TOKEN)",
        },
      });
    } else {
      const compat = await handleCompat(request, env, url.pathname);
      response = compat || new Response("Not found", { status: 404 });
    }

    return withCors(request, response);
  },
} satisfies ExportedHandler<Env>;
