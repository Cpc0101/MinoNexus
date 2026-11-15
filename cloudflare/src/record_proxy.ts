/** Read Scout-owned records through the authenticated Nexus node connection. */
import type { Env } from "./env";
import { scoutExecute, merged } from "./rpc";
import { getSessionDispatch, TaskError, type Dispatch } from "./task_scheduler";

async function scoped(env: Env, owner: string, session: string): Promise<Dispatch> {
  const job = await getSessionDispatch(env.DB, owner, session);
  if (!job) throw new TaskError("record_not_found", "没有这个执行记录", 404);
  return job;
}
export async function readRecord(env: Env, owner: string, session: string, capability: "record.status" | "record.events" | "record.artifact", params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const job = await scoped(env, owner, session);
  const result = await scoutExecute(env, { scout_id: job.scout_id, capability_id: capability, timeout_ms: 15_000,
    params: { ...params, task_id: job.run_id, session_id: job.session_id, attempt_id: job.attempt_id, owner_user_id: owner } });
  if (!result.ok) {
    const code = result.error || "record_unavailable";
    if (["scout_offline", "scout_disconnected", "scout_error", "rpc_timeout", "record_unavailable"].includes(code))
      throw new TaskError("evidence_temporarily_unavailable", "保存证据的 Scout 暂不可用，任务摘要仍可查看", 503);
    throw new TaskError(code, result.summary || "无法读取执行证据", ["record_not_found", "artifact_not_found"].includes(code) ? 404 : code === "artifact_expired" ? 410 : code === "record_owner_mismatch" ? 403 : 502);
  }
  const data = merged(result);
  if (capability === "record.status") {
    const identity = { task_id: job.run_id, run_id: job.run_id, session_id: job.session_id, attempt_id: job.attempt_id,
      case_id: job.case_id, owner_user_id: owner, node_id: job.scout_id };
    if (Object.entries(identity).some(([key, value]) => data[key] !== value) || !Number.isSafeInteger(data.summary_version) || Number(data.summary_version) < Math.max(1, job.sync_version) || typeof data.sealed !== "boolean")
      throw new TaskError("record_contract_invalid", "执行节点返回的记录身份或版本无效", 502);
  }
  return { ...data, record_source: "scout" };
}
export async function readEvents(env: Env, owner: string, session: string, fromSeq = 0, limit = 100, maxBytes = 524288): Promise<Record<string, unknown>> {
  fromSeq = Math.max(0, Math.floor(fromSeq)); limit = Math.min(200, Math.max(1, Math.floor(limit))); maxBytes = Math.min(1048576, Math.max(1024, Math.floor(maxBytes)));
  const page = await readRecord(env, owner, session, "record.events", { after_seq: fromSeq, limit, max_bytes: maxBytes });
  if (!Array.isArray(page.items) || page.items.length > limit || !Number.isSafeInteger(page.next_seq) || !Number.isSafeInteger(page.high_watermark) || typeof page.has_more !== "boolean" || typeof page.sealed !== "boolean" || new TextEncoder().encode(JSON.stringify(page)).byteLength > maxBytes + 1024)
    throw new TaskError("record_contract_invalid", "执行节点返回的事件分页无效", 502);
  let previous = fromSeq;
  for (const item of page.items) {
    if (!item || typeof item !== "object" || !Number.isSafeInteger(item.seq) || item.seq <= previous || item.seq > Number(page.high_watermark) || typeof item.type !== "string" || typeof item.ts !== "string" || !item.payload || typeof item.payload !== "object")
      throw new TaskError("record_contract_invalid", "执行节点返回的事件顺序无效", 502);
    previous = item.seq;
  }
  if (page.next_seq !== previous || (page.has_more ? !page.items.length || previous >= Number(page.high_watermark) : previous < Number(page.high_watermark)))
    throw new TaskError("record_contract_invalid", "执行节点返回的事件游标无效", 502);
  return { ...page, session_id: session };
}
export async function screenshotMetadata(env: Env, owner: string, session: string, seq: number, explicitArtifact = ""): Promise<Record<string, unknown>> {
  let artifact = explicitArtifact;
  if (!artifact) {
    const page = await readEvents(env, owner, session, Math.max(0, seq - 1), 1);
    const items = Array.isArray(page.items) ? page.items as Array<Record<string, unknown>> : [];
    const event = items.find(item => Number(item.seq) === seq);
    const payload = event?.payload && typeof event.payload === "object" ? event.payload as Record<string, unknown> : {};
    artifact = String(payload.artifact_id || payload.screenshot_artifact_id || "");
  }
  if (!artifact) throw new TaskError("artifact_not_found", "该事件没有截图", 404);
  const head = await readRecord(env, owner, session, "record.artifact", { artifact_id: artifact, offset: 0, length: 1 });
  return { session_id: session, seq, artifact_id: artifact, mime_type: head.mime_type, total_size: head.total_size, sha256: head.sha256,
    record_source: "scout", url: `/case-runner/sessions/${encodeURIComponent(session)}/artifacts/${encodeURIComponent(artifact)}`, requires_auth: true };
}
function bytes(encoded: unknown): Uint8Array {
  const text = atob(String(encoded || "")), out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i);
  return out;
}
export async function artifactChunk(env: Env, owner: string, session: string, artifact: string, offset = 0, length = 32768): Promise<Record<string, unknown>> {
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(length) || length < 1 || length > 65536)
    throw new TaskError("artifact_range_invalid", "证据分块范围无效，length 最大 65536", 422);
  const data = await readRecord(env, owner, session, "record.artifact", { artifact_id: artifact, offset, length });
  let chunk: Uint8Array;
  try { chunk = bytes(data.data_base64); } catch { throw new TaskError("record_contract_invalid", "证据分块编码无效", 502); }
  const total = Number(data.total_size);
  if (data.artifact_id !== artifact || data.offset !== offset || !Number.isSafeInteger(total) || total < offset || chunk.length > length || data.bytes_read !== chunk.length || offset + chunk.length > total || data.eof !== (offset + chunk.length === total) || (!chunk.length && !data.eof) || !/^[a-f0-9]{64}$/.test(String(data.sha256 || "")) || !/^[\w.+-]+\/[\w.+-]+(?:;[^\r\n]*)?$/.test(String(data.mime_type || "")))
    throw new TaskError("record_contract_invalid", "证据分块内容不完整", 502);
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(chunk);
    if (new TextEncoder().encode(JSON.stringify(text)).byteLength <= 96 * 1024) data.text = text;
  } catch { /* Binary or a partial UTF-8 code point: callers retain base64. */ }
  return data;
}
export async function artifactResponse(env: Env, owner: string, session: string, artifact: string, range: string | null): Promise<Response> {
  const head = await readRecord(env, owner, session, "record.artifact", { artifact_id: artifact, offset: 0, length: 1 });
  const total = Number(head.total_size), mime = String(head.mime_type || "application/octet-stream");
  if (!Number.isSafeInteger(total) || total < 0 || total > 128 * 1024 * 1024) throw new TaskError("artifact_size_invalid", "证据文件超过单次读取上限", 413);
  let start = 0, end = Math.max(0, total - 1);
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (!match || (!match[1] && !match[2])) return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${total}` } });
    if (!match[1]) start = Math.max(0, total - Number(match[2]));
    else { start = Number(match[1]); if (match[2]) end = Math.min(total - 1, Number(match[2])); }
    if (start > end || start >= total) return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${total}` } });
  }
  let offset = start, cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (cancelled || offset > end || total === 0) { controller.close(); return; }
      try {
        const piece = await readRecord(env, owner, session, "record.artifact", { artifact_id: artifact, offset, length: Math.min(262144, end - offset + 1) });
        const chunk = bytes(piece.data_base64);
        if (Number(piece.offset) !== offset || Number(piece.total_size) !== total || !chunk.length || chunk.length > end - offset + 1 || Number(piece.bytes_read) !== chunk.length || (head.sha256 && piece.sha256 !== head.sha256))
          throw new Error("artifact_chunk_invalid");
        if (cancelled) return;
        controller.enqueue(chunk); offset += chunk.length;
        if (offset > end) controller.close();
      } catch (error) { controller.error(error); }
    },
    cancel() { cancelled = true; },
  });
  const headers = new Headers({ "Content-Type": mime, "Content-Length": String(total ? end - start + 1 : 0), "Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" });
  if (range) headers.set("Content-Range", `bytes ${start}-${end}/${total}`);
  if (head.sha256) headers.set("ETag", `"${String(head.sha256).replaceAll('"', '')}"`);
  return new Response(stream, { status: range ? 206 : 200, headers });
}
export async function listSessions(env: Env, owner: string, args: { run_id?: string; app_id?: string; case_id?: string; limit?: number; offset?: number } = {}): Promise<Record<string, unknown>> {
  const limit = Math.max(1, Math.min(100, Number(args.limit || 30))), offset = Math.max(0, Number(args.offset || 0));
  const page = await env.DB.prepare("SELECT d.* FROM script_dispatches d JOIN app_regression_runs r ON r.run_id=d.run_id WHERE d.owner_user_id=? AND (?='' OR d.run_id=?) AND (?='' OR d.case_id=?) AND (?='' OR r.app_id=?) ORDER BY d.created_at_ms DESC LIMIT ? OFFSET ?")
    .bind(owner, args.run_id || "", args.run_id || "", args.case_id || "", args.case_id || "", args.app_id || "", args.app_id || "", limit, offset).all<Dispatch>();
  return { items: (page.results || []).map(row => ({ ...JSON.parse(row.result_json || "{}"), task_id: row.run_id, run_id: row.run_id, session_id: row.session_id, attempt_id: row.attempt_id,
    case_id: row.case_id, record_source: "scout", record_node_id: row.scout_id, detail_availability: row.sync_version ? "available" : "pending" })), limit, offset };
}
