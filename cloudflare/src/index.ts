import type { Env } from "./env";
import { hubStub } from "./env";
import { handleMcp } from "./mcp";
import { NexusHub } from "./hub";

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
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return health();
    }

    if (url.pathname === "/mcp") {
      return handleMcp(request, env);
    }

    if (url.pathname === "/node") {
      return hubStub(env).fetch(request);
    }

    if (url.pathname === "/v1/scouts") {
      return hubStub(env).fetch(new Request("https://hub/internal/scouts"));
    }

    if (url.pathname === "/v1/devices") {
      const onlineOnly = url.searchParams.get("online_only") === "1";
      return hubStub(env).fetch(
        new Request(`https://hub/internal/devices?online_only=${onlineOnly ? "1" : "0"}`),
      );
    }

    if (url.pathname === "/") {
      return Response.json({
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
    }

    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
