import type { Env } from "./env";
import { hubStub } from "./env";
import { handleMcp } from "./mcp";
import { NexusHub } from "./hub";
import { preflight, withCors } from "./cors";
import { enhancedHealth, handleCompat, requireMcpBearer } from "./compat";

export { NexusHub };

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
      // Scout protocol: auth is REGISTER token, not MCP bearer.
      return hubStub(env).fetch(request);
    } else if (url.pathname === "/v1/scouts") {
      const denied = requireMcpBearer(request, env);
      if (denied) return withCors(request, denied);
      response = await hubStub(env).fetch(new Request("https://hub/internal/scouts"));
    } else if (url.pathname === "/v1/devices") {
      const denied = requireMcpBearer(request, env);
      if (denied) return withCors(request, denied);
      const onlineOnly = url.searchParams.get("online_only") === "1";
      response = await hubStub(env).fetch(
        new Request(`https://hub/internal/devices?online_only=${onlineOnly ? "1" : "0"}`),
      );
    } else if (url.pathname === "/") {
      response = Response.json({
        service: "mino-nexus-cloudflare",
        version: "0.0.1",
        workbench: false,
        endpoints: {
          health: "GET /health",
          scout_ws: "GET /node (WebSocket)",
          mcp: "POST /mcp (Bearer MCP_BEARER_TOKEN)",
          scouts: "GET /v1/scouts (Bearer MCP_BEARER_TOKEN)",
          devices: "GET /v1/devices (Bearer MCP_BEARER_TOKEN)",
        },
        note: "Full Workbench UI is not supported on this free hub.",
      });
    } else {
      const compat = await handleCompat(request, env, url.pathname);
      response = compat || new Response("Not found", { status: 404 });
    }

    return withCors(request, response);
  },
} satisfies ExportedHandler<Env>;
