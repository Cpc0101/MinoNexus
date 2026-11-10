/** Scout install-token issue / status / consume for Workbench Add Node. */

const TTL_SEC = 15 * 60;
const MANIFEST_URL =
  "https://github.com/pengchengluoyi/MinoScout/releases/latest/download/manifest.json";

function randomHex(bytes = 16): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomToken(): string {
  const buf = new Uint8Array(24);
  crypto.getRandomValues(buf);
  return btoa(String.fromCharCode(...buf))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function psQuote(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

type ManifestItem = { os?: string; arch?: string; url?: string };

async function loadTargets(): Promise<{
  manifest_url: string;
  targets: Record<string, string[]>;
}> {
  try {
    const res = await fetch(MANIFEST_URL, { redirect: "follow" });
    if (!res.ok) throw new Error(`manifest ${res.status}`);
    const body = (await res.json()) as { items?: ManifestItem[] } & ManifestItem;
    const rows = Array.isArray(body.items) ? body.items : [body];
    const targets: Record<string, string[]> = {};
    for (const row of rows) {
      if (!row?.url) continue;
      const system =
        row.os === "darwin" || row.os === "win32" || row.os === "linux"
          ? row.os
          : row.os === "macos"
            ? "darwin"
            : row.os === "windows"
              ? "win32"
              : "";
      const arch = row.arch === "arm64" || row.arch === "x64" ? row.arch : "";
      if (!system || !arch) continue;
      targets[system] ||= [];
      if (!targets[system].includes(arch)) targets[system].push(arch);
    }
    if (Object.keys(targets).length) {
      return { manifest_url: MANIFEST_URL, targets };
    }
  } catch {
    /* fall through */
  }
  return {
    manifest_url: MANIFEST_URL,
    targets: { darwin: ["arm64", "x64"], win32: ["x64"], linux: ["x64", "arm64"] },
  };
}

function installCommands(
  manifestUrl: string,
  token: string,
  nexusUrl: string,
  targets: Record<string, string[]>,
): Record<string, string> {
  const directory = manifestUrl.replace(/\/[^/]*$/, "");
  const commands: Record<string, string> = {};
  for (const system of Object.keys(targets)) {
    if (system === "win32") {
      const scriptUrl = psQuote(`${directory}/bootstrap.ps1`);
      commands[system] =
        `$script = Join-Path ([IO.Path]::GetTempPath()) ('mino-scout-' + [guid]::NewGuid() + '.ps1'); ` +
        `Invoke-WebRequest -UseBasicParsing -Uri ${scriptUrl} -OutFile $script; ` +
        `& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Token ${psQuote(token)} ` +
        `-NexusUrl ${psQuote(nexusUrl)} -ManifestUrl ${psQuote(manifestUrl)}`;
    } else {
      commands[system] =
        `curl -fsSL ${shellQuote(`${directory}/bootstrap.sh`)} | bash -s -- ` +
        `--token ${shellQuote(token)} --nexus-url ${shellQuote(nexusUrl)} ` +
        `--manifest-url ${shellQuote(manifestUrl)}`;
    }
  }
  return commands;
}

export async function issueInstallToken(
  db: D1Database,
  user_id: string,
  nexusUrl: string,
): Promise<Record<string, unknown>> {
  const token = randomToken();
  const install_id = randomHex(16);
  const now = Date.now();
  const expires_at_ms = now + TTL_SEC * 1000;
  await db
    .prepare(
      `INSERT INTO install_tokens (token, install_id, user_id, node_id, created_at_ms, expires_at_ms, registered_at_ms)
       VALUES (?, ?, ?, NULL, ?, ?, 0)`,
    )
    .bind(token, install_id, user_id, now, expires_at_ms)
    .run();

  const release = await loadTargets();
  return {
    token,
    install_id,
    expires_at: Math.floor(expires_at_ms / 1000),
    ttl_sec: TTL_SEC,
    nexus_url: nexusUrl,
    targets: release.targets,
    commands: installCommands(release.manifest_url, token, nexusUrl, release.targets),
  };
}

export async function getInstallStatus(
  db: D1Database,
  install_id: string,
  user_id: string,
): Promise<Record<string, unknown> | null> {
  const row = await db
    .prepare(
      `SELECT install_id, user_id, node_id, expires_at_ms, registered_at_ms
       FROM install_tokens WHERE install_id = ? AND user_id = ?`,
    )
    .bind(install_id, user_id)
    .first<{
      install_id: string;
      user_id: string;
      node_id: string | null;
      expires_at_ms: number;
      registered_at_ms: number;
    }>();
  if (!row) return null;

  const now = Date.now();
  let status: string;
  if (row.node_id) status = "registered";
  else if (row.expires_at_ms <= now) status = "expired";
  else status = "pending";

  let online = false;
  let hostname = "";
  if (row.node_id) {
    const scout = await db
      .prepare(`SELECT online, last_seen_ms, host_json FROM scouts WHERE scout_id = ?`)
      .bind(row.node_id)
      .first<{ online: number; last_seen_ms: number; host_json: string | null }>();
    if (scout) {
      online = Number(scout.online) === 1 && now - Number(scout.last_seen_ms || 0) <= 45_000;
      if (scout.host_json) {
        try {
          const host = JSON.parse(scout.host_json) as Record<string, unknown>;
          hostname = String(host.hostname || host.name || "");
        } catch {
          /* ignore */
        }
      }
    }
  }

  return {
    install_id: row.install_id,
    status,
    expires_at: Math.floor(row.expires_at_ms / 1000),
    registered_at: row.registered_at_ms ? Math.floor(row.registered_at_ms / 1000) : 0,
    node_id: row.node_id || "",
    hostname,
    online,
  };
}

export async function consumeInstallToken(
  db: D1Database,
  token: string,
  scout_id: string,
): Promise<{ user_id: string } | null> {
  const now = Date.now();
  const row = await db
    .prepare(
      `SELECT token, user_id, node_id, expires_at_ms FROM install_tokens WHERE token = ?`,
    )
    .bind(token)
    .first<{ token: string; user_id: string; node_id: string | null; expires_at_ms: number }>();
  if (!row || row.expires_at_ms <= now) return null;
  if (row.node_id && row.node_id !== scout_id) return null;

  await db
    .prepare(
      `UPDATE install_tokens SET node_id = ?, registered_at_ms = ? WHERE token = ?`,
    )
    .bind(scout_id, now, token)
    .run();
  return { user_id: row.user_id };
}

export async function scoutReleaseMeta(os: string, arch: string): Promise<Record<string, unknown>> {
  const release = await loadTargets();
  const system = os === "macos" ? "darwin" : os === "windows" ? "win32" : os;
  const arches = release.targets[system] || [];
  return {
    os: system,
    arch,
    available: arches.includes(arch),
    arches,
    manifest_url: release.manifest_url,
    version: "",
  };
}
