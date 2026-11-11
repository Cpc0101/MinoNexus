/**
 * Port of mino_nexus.casework.script_workspace get_repo / save_repo / public_url.
 * Cloudflare hub stores address only — Scout clones at run time (no Worker checkout).
 */

export type ScriptRepoRow = {
  url: string;
  branch: string;
  updated_at: string;
};

export type WorkspacePlace = {
  root: string;
  checkout: string;
  venv: string;
  ready: boolean;
  error: string;
};

/** Hub has no local case_workspaces; Scout pulls. Keep shape for Workbench. */
export function workspacePlace(): WorkspacePlace {
  return { root: "", checkout: "", venv: "", ready: false, error: "" };
}

export function publicUrl(url: string): string {
  try {
    const parts = new URL(String(url || ""));
    if (!parts.username) return String(url || "");
    const host = parts.port ? `${parts.hostname}:${parts.port}` : parts.hostname;
    return `${parts.protocol}//${host}${parts.pathname}`;
  } catch {
    return String(url || "");
  }
}

export async function getRepo(db: D1Database, projectId: string): Promise<ScriptRepoRow> {
  const pid = String(projectId || "").trim();
  if (!pid) return { url: "", branch: "", updated_at: "" };
  const row = await db
    .prepare(`SELECT url, branch, updated_at FROM project_script_repos WHERE project_id = ?`)
    .bind(pid)
    .first<{ url: string; branch: string; updated_at: string }>();
  if (!row) return { url: "", branch: "", updated_at: "" };
  return {
    url: String(row.url || ""),
    branch: String(row.branch || ""),
    updated_at: String(row.updated_at || ""),
  };
}

export async function saveRepo(
  db: D1Database,
  projectId: string,
  url: string,
  branch: string,
): Promise<ScriptRepoRow & { public_url: string }> {
  const pid = String(projectId || "").trim();
  if (!pid) throw new Error("缺少 project_id");
  const cleaned = String(url || "").trim();
  if (cleaned) {
    // Match urllib.parse.urlsplit(...).scheme ∈ {http, https, ssh, git}
    let scheme = "";
    const m = cleaned.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
    if (m) scheme = m[1].toLowerCase();
    if (!["http", "https", "ssh", "git"].includes(scheme)) {
      throw new Error("script_repo_url_invalid");
    }
  }
  const updated = new Date().toISOString();
  const branchClean = String(branch || "").trim();
  await db
    .prepare(
      `INSERT INTO project_script_repos (project_id, url, branch, updated_at)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(project_id) DO UPDATE SET
         url=excluded.url,
         branch=excluded.branch,
         updated_at=excluded.updated_at`,
    )
    .bind(pid, cleaned, branchClean, updated)
    .run();
  return {
    url: cleaned,
    branch: branchClean,
    updated_at: updated,
    public_url: publicUrl(cleaned),
  };
}

export async function requireOwnedProject(
  db: D1Database,
  projectId: string,
  userId: string,
): Promise<boolean> {
  const row = await db
    .prepare(`SELECT project_id FROM projects WHERE project_id = ? AND owner_user_id = ?`)
    .bind(projectId, userId)
    .first();
  return Boolean(row);
}
