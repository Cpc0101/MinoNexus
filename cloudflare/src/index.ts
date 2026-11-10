import type { Env } from "./env";
import { hubStub } from "./env";
import { handleMcp } from "./mcp";
import { NexusHub } from "./hub";
import { preflight, withCors } from "./cors";
import { enhancedHealth, handleCompat } from "./compat";

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
      // WebSocket upgrades must pass through unchanged.
      return hubStub(env).fetch(request);
    } else if (url.pathname === "/v1/scouts") {
      response = await hubStub(env).fetch(new Request("https://hub/internal/scouts"));
    } else if (url.pathname === "/v1/devices") {
      const onlineOnly = url.searchParams.get("online_only") === "1";
      response = await hubStub(env).fetch(
        new Request(`https://hub/internal/devices?online_only=${onlineOnly ? "1" : "0"}`),
      );
    } else if (url.pathname === "/") {
      response = Response.json({
        service: "mino-nexus-cloudflare",
        version: "0.0.1",
        endpoints: {
          health: "GET /health",
          scout_ws: "GET /node (WebSocket)",
          mcp: "POST /mcp",
          scouts: "GET /v1/scouts",
          devices: "GET /v1/devices",
          workbench_compat: [
            "GET /runtime/nodes",
            "GET /device/list",
            "POST /auth/login",
            "GET /auth/status",
          ],
        },
      });
    } else {
      const compat = await handleCompat(request, env, url.pathname);
      response = compat || new Response("Not found", { status: 404 });
    }

    return withCors(request, response);
  },
} satisfies ExportedHandler<Env>;
