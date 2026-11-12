# Mino Nexus — Cloudflare free hub (0.0.1)

New implementation on the `cloudflare` branch. **Not** a port of the Python FastAPI Nexus.

## Scope

1. **Scout online channel** — WebSocket `GET /node` with `REGISTER` / `HEARTBEAT`
2. **Agent MCP** — `POST /mcp` with OAuth 2.1 (`mino_oat_`) or Workbench manual token (`mino_mcp_`); tools from mcp0.0.1 YAML: `agent_guide`, `project_list`, `project_create`, `scout_list`, `device_list`, `app_create`, `app_delete`, `plugin_status`, `node_channel_program`, `plugin_install`, `project_script_repo_set`, `repo_apps_sync`, `repo_apps_get`, `case_list` / `requirement_list`（需求 id、名称、链接）, `requirement_save`, `case_run`（`script_targets` 或 `case_ids` → `script.run`）, `task_get`
3. **Device status sync** — devices reported by Scouts, queryable via MCP / HTTP
4. **Workbench HTTP** — D1 auth/sessions; install-token; nodes/devices; projects/apps; AI provider settings; plugin/channel-program install via Scout EXECUTE

Pages frontend: `https://workbench.minocc.top` → API `https://nexus.minocc.top`.

Still limited: `case_run` is MVP (`script_targets` or explicit `case_ids`; no case body library, account pool, leases, write-safety, or session events). Empty `case_ids` under a requirement asks Scout `script.list`. Scout `script.callback` EXECUTE is answered on the hub socket.

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

Workbench login uses real email/password accounts in D1.
MCP (`/mcp`) accepts OAuth access tokens (`mino_oat_`) or Workbench-issued manual tokens (`mino_mcp_`). Shared `MCP_BEARER_TOKEN` is probe-only (`initialize` / `ping` / `tools/list`).
Consent page: `https://workbench.minocc.top/oauth/consent`.
`case_run` MVP needs migration `0008_case_runner.sql` applied to D1 (`wrangler d1 migrations apply`). Runs are driven by `ctx.waitUntil` in the Worker; Workers cap post-response `waitUntil` time, so long multi-case runs may be cut off until the runner moves to a Durable Object alarm / Queue / Workflow.
