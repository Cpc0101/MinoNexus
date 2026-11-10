import type { Env } from "./env";
import { hubStub } from "./env";
import { handleMcp } from "./mcp";
import { NexusHub } from "./hub";
import { preflight, withCors } from "./cors";

export { NexusHub };

function health(): Response {
  return Response.json({
    ok: true,
    service: "mino-nexus-cloudflare",
    version: "0.0.1",
    scope: ["scout_online", "mcp", "device_sync"],
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return preflight(request);
    }

    const url = new URL(request.url);
    let response: Response;

    if (url.pathname === "/health") {
      response = health();
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
        },
      });
    } else {
      response = new Response("Not found", { status: 404 });
    }

    return withCors(request, response);
  },
} satisfies ExportedHandler<Env>;
