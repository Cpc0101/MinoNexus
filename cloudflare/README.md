# Mino Nexus — Cloudflare free hub (0.0.1)

New implementation on the `cloudflare` branch. **Not** a port of the Python FastAPI Nexus.

## Scope

1. **Scout online channel** — WebSocket `GET /node` with `REGISTER` / `HEARTBEAT`
2. **Agent MCP** — `POST /mcp` tools: `agent_guide`, `scout_list`, `device_list`
3. **Device status sync** — devices reported by Scouts, queryable via MCP / HTTP
4. **Workbench HTTP** — D1 auth/sessions; install-token; nodes/devices; projects/apps; AI provider settings; soft stubs for remaining GETs

Pages frontend: `https://workbench.minocc.top` → API `https://nexus.minocc.top`.

Still limited: `case_run` / Scout RPC（插件安装、远程命令）返回 501。

## Runtime shape (free-tier friendly)

- Cloudflare **Worker** as HTTP/MCP edge
- **D1** (`mino-nexus`) for shared scout / device rows (reads from Worker + MCP)
- One **Durable Object** (`NexusHub`) only for hibernated Scout WebSockets
- **Hibernation WebSocket API** so idle Scout connections do not burn DO duration

## Local

```bash
cd cloudflare
cp .dev.vars.example .dev.vars   # edit tokens
npm install
npm run dev
```

Smoke:

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/scouts
curl -s -H "Authorization: Bearer dev-mcp-token" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:8787/mcp
```

Scout first frame example:

```json
{"type":"REGISTER","scout_id":"scout-1","token":"dev-pair-token","devices":[{"device_id":"web-1","platform":"web","ready":true}]}
```

Use the returned `node_token` on later REGISTER reconnects. Heartbeat every ~15s; hub treats a Scout offline after 45s without a fresh last_seen (and immediately on WS close).

## Deploy

```bash
npx wrangler login
npm run deploy
```

Set `SCOUT_PAIR_TOKEN` / `MCP_BEARER_TOKEN` as Worker secrets for non-dev.

## Custom domains

| Host | Product | Role |
|---|---|---|
| `https://nexus.minocc.top` | Worker `mino-nexus` | Scout / MCP / device sync |
| `https://workbench.minocc.top` | Pages `mino-workbench` | Workbench UI |

Hub endpoints:

- Scout WS: `wss://nexus.minocc.top/node`
- MCP: `POST https://nexus.minocc.top/mcp`
- Health: `GET https://nexus.minocc.top/health`

Workbench is built with `VITE_NEXUS_URL=https://nexus.minocc.top` and deployed via:

```bash
cd ../MinoWorkbench
VITE_NEXUS_URL=https://nexus.minocc.top npm run build
npx wrangler pages deploy dist --project-name mino-workbench --branch main
```

DNS for Pages (same Cloudflare zone):

- Type `CNAME`, name `workbench`, target `mino-workbench.pages.dev`, proxied

Workbench login uses real email/password accounts in D1 (not the MCP bearer).
MCP (`/mcp`, `/v1/*`) still requires `Authorization: Bearer <MCP_BEARER_TOKEN>`.
`case_run` remains unavailable on this hub.
