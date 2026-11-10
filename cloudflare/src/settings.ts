/** Per-user settings JSON blobs (AI providers first). */

const AI_PRESETS = [
  {
    id: "openai",
    name: "OpenAI",
    api_type: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o",
    model_options: ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    sdk_provider: "openai",
  },
  {
    id: "anthropic",
    name: "Anthropic",
    api_type: "anthropic",
    base_url: "https://api.anthropic.com",
    model: "claude-sonnet-4-20250514",
    model_options: ["claude-3-5-sonnet-latest", "claude-sonnet-4-20250514"],
    sdk_provider: "anthropic",
  },
  {
    id: "google",
    name: "Google Gemini",
    api_type: "gemini",
    base_url: "https://generativelanguage.googleapis.com/v1beta",
    model: "gemini-2.5-flash",
    model_options: ["gemini-2.5-flash", "gemini-2.5-pro"],
    sdk_provider: "gemini",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    api_type: "openai",
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    model_options: ["deepseek-chat", "deepseek-reasoner"],
    sdk_provider: "deepseek",
  },
  {
    id: "qwen",
    name: "通义千问",
    api_type: "openai",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    model_options: ["qwen-plus", "qwen-max", "qwen-turbo"],
    sdk_provider: "dashscope",
  },
] as const;

async function getJson(
  db: D1Database,
  owner: string,
  key: string,
): Promise<Record<string, unknown>> {
  const row = await db
    .prepare(`SELECT value_json FROM user_settings WHERE owner_user_id = ? AND key = ?`)
    .bind(owner, key)
    .first<{ value_json: string }>();
  if (!row?.value_json) return {};
  try {
    return JSON.parse(row.value_json) as Record<string, unknown>;
  } catch {
    return {};
  }
}

async function putJson(
  db: D1Database,
  owner: string,
  key: string,
  value: Record<string, unknown>,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO user_settings (owner_user_id, key, value_json, updated_at_ms)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(owner_user_id, key) DO UPDATE SET
         value_json=excluded.value_json,
         updated_at_ms=excluded.updated_at_ms`,
    )
    .bind(owner, key, JSON.stringify(value), Date.now())
    .run();
}

function maskKey(raw: unknown): string {
  const key = String(raw || "");
  if (!key) return "";
  if (key.length <= 8) return "****";
  return `${key.slice(0, 3)}…${key.slice(-4)}`;
}

function publicProvider(id: string, stored: Record<string, unknown> | undefined, preset?: (typeof AI_PRESETS)[number]) {
  const base = preset || {
    id,
    name: id,
    api_type: "openai",
    base_url: "",
    model: "",
    model_options: [] as string[],
    sdk_provider: "openai",
  };
  const row = stored || {};
  const hasKey = Boolean(String(row.api_key || "").trim());
  return {
    id,
    name: String(row.name || base.name),
    api_type: String(row.api_type || base.api_type),
    base_url: String(row.base_url || base.base_url),
    model: String(row.model || base.model),
    model_options: Array.isArray(row.model_options) ? row.model_options : base.model_options,
    sdk_provider: String(row.sdk_provider || base.sdk_provider),
    embedding_model: String(row.embedding_model || ""),
    configured: hasKey,
    enabled: row.enabled !== false,
    case_execution_use: Boolean(row.case_execution_use),
    api_key_masked: hasKey ? maskKey(row.api_key) : "",
    validation: row.validation || undefined,
  };
}

export async function listAIProviders(db: D1Database, owner: string) {
  const ai = await getJson(db, owner, "ai.providers");
  const providers = AI_PRESETS.map((p) =>
    publicProvider(p.id, ai[p.id] as Record<string, unknown> | undefined, p),
  );
  for (const [id, row] of Object.entries(ai)) {
    if (id.startsWith("_") || AI_PRESETS.some((p) => p.id === id)) continue;
    if (row && typeof row === "object") {
      providers.push(publicProvider(id, row as Record<string, unknown>));
    }
  }
  return {
    providers,
    sdk_providers: ["openai", "anthropic", "gemini", "deepseek", "dashscope"],
    presets: AI_PRESETS,
    default_provider: String(ai._default_provider || "openai"),
    usage: {
      case_execution_provider_id: String(ai._case_execution_provider_id || ""),
    },
  };
}

export async function saveAIProvider(
  db: D1Database,
  owner: string,
  providerId: string,
  payload: Record<string, unknown>,
) {
  const id = String(providerId || "").trim().toLowerCase();
  if (!id) throw new Error("provider_id required");
  const ai = await getJson(db, owner, "ai.providers");
  const prev = (ai[id] as Record<string, unknown>) || {};
  const next: Record<string, unknown> = { ...prev };
  for (const [k, v] of Object.entries(payload)) {
    if (k === "clear_key" || k === "set_default" || k === "api_key") continue;
    if (v !== undefined) next[k] = v;
  }
  if (payload.clear_key) delete next.api_key;
  else if (typeof payload.api_key === "string" && payload.api_key.trim()) {
    next.api_key = payload.api_key.trim();
  }
  ai[id] = next;
  if (payload.set_default) ai._default_provider = id;
  if (payload.case_execution_use) ai._case_execution_provider_id = id;
  await putJson(db, owner, "ai.providers", ai);
  return listAIProviders(db, owner);
}

export async function deleteAIProvider(db: D1Database, owner: string, providerId: string) {
  const id = String(providerId || "").trim().toLowerCase();
  const ai = await getJson(db, owner, "ai.providers");
  delete ai[id];
  await putJson(db, owner, "ai.providers", ai);
  return { deleted: true };
}
