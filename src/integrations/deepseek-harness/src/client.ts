/**
 * TS client for the local Corti OSS `/api/v1/memory/*` endpoints.
 * Mirrors `src/integrations/hermes/_client.py` and
 * `src/integrations/claude-code/hooks/scripts/utils/corti-api.js`.
 */

export interface CortiConfig {
  baseUrl: string;
  appId: string;
  projectId: string;
  userId: string;
  agentId: string;
}

export interface Episode {
  id: string;
  session_id: string;
  timestamp: string;
  sender_ids: string[];
  summary: string;
  subject: string;
  episode: string;
  type: string;
  score: number;
}

interface Envelope<T> {
  ok: boolean;
  status: number;
  data?: T;
  error?: unknown;
}

const DEFAULT_TIMEOUT_MS = 30_000;
const FLUSH_TIMEOUT_MS = 120_000;
const ADD_BATCH_SIZE = 20;

export class CortiClient {
  private readonly cfg: CortiConfig;
  /**
   * High-water mark of timestamps sent by this client (any session).
   * Corti derives message_id from (session_id, timestamp_ms, per-batch
   * idx); overlapping async `add()` calls for the same session can land
   * in the same millisecond and collide on the PK (silent INSERT OR
   * IGNORE drops). A client-level monotonic clock guarantees uniqueness
   * for every session this client writes.
   */
  private lastTs = 0;

  constructor(cfg: CortiConfig) {
    this.cfg = cfg;
  }

  private scope(): Record<string, string> {
    return {
      app_id: this.cfg.appId,
      project_id: this.cfg.projectId,
      user_id: this.cfg.userId,
    };
  }

  private async post<T>(path: string, body: Record<string, unknown>, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<Envelope<T>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.cfg.baseUrl}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await res.text();
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
      if (!res.ok) return { ok: false, status: res.status, error: parsed ?? text };
      const envelope = parsed as { data?: unknown } | null;
      return { ok: true, status: res.status, data: (envelope?.data ?? parsed) as T };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return { ok: false, status: 0, error: controller.signal.aborted ? `timeout after ${timeoutMs}ms` : message };
    } finally {
      clearTimeout(timer);
    }
  }

  /** POST /api/v1/memory/search */
  async search(query: string, opts: { topK?: number; method?: string } = {}): Promise<Envelope<{ episodes: Episode[] }>> {
    return this.post("/api/v1/memory/search", {
      query,
      method: opts.method ?? "hybrid",
      top_k: opts.topK ?? 8,
      ...this.scope(),
    });
  }

/** POST /api/v1/memory/get — recent memories, newest first (`page` is 1-based). */
async recent(
    pageSize = 10,
    page = 1,
  ): Promise<Envelope<{ memories?: Episode[]; episodes?: Episode[]; items?: Episode[] }>> {
    return this.post("/api/v1/memory/get", {
      memory_type: "episode",
      page,
      page_size: pageSize,
      sort_by: "timestamp",
      sort_order: "desc",
      ...this.scope(),
    });
  }

  /** POST /api/v1/memory/add — messages chunked into batches */
  async add(
    sessionId: string,
    messages: ReadonlyArray<{ role: "user" | "assistant"; content: string; timestamp?: number }>,
  ): Promise<Envelope<unknown>> {
    // Monotonic unique timestamps across ALL add() calls of this client:
    // Corti derives message_id from (session_id, timestamp_ms, per-batch
    // idx); batches reset idx, and overlapping async calls (turn-end
    // fire-and-forget + tool-triggered adds) can share a millisecond.
    // Both collide on the PK and silently drop (INSERT OR IGNORE).
    //
    // Guarantees here:
    //  - generated timestamps are STRICTLY GREATER than every timestamp
    //    provided in this call (base = maxProvided + 1 floor), so a
    //    generated ts can never equal a provided one (same-batch idx
    //    alignment would collide);
    //  - the client-level high-water mark rules out cross-call collisions;
    //  - loops instead of Math.max(...spread): no argument-limit risk on
    //    large inputs.
    // Caller-provided timestamps are passed through verbatim — replay
    // determinism (same payload → same ids → retry dedup) depends on it.
    let maxProvided = 0;
    for (const m of messages) {
      const ts = m.timestamp ?? 0;
      if (ts > maxProvided) maxProvided = ts;
    }
    const base = Math.max(Date.now(), this.lastTs + 1, maxProvided + 1);
    const formatted = messages.map((m, i) => ({
      sender_id: m.role === "assistant" ? this.cfg.agentId : this.cfg.userId,
      sender_name: m.role === "assistant" ? this.cfg.agentId : "user",
      role: m.role,
      timestamp: m.timestamp ?? base + i,
      content: m.content,
    }));
    // Advance the high-water mark past every timestamp actually sent
    // (synchronous block: no await between read and write, so overlapping
    // async calls cannot interleave here in the JS event loop).
    for (const m of formatted) {
      if (m.timestamp > this.lastTs) this.lastTs = m.timestamp;
    }
    let last: Envelope<unknown> = { ok: true, status: 0 };
    for (let i = 0; i < formatted.length; i += ADD_BATCH_SIZE) {
      last = await this.post("/api/v1/memory/add", {
        session_id: sessionId,
        messages: formatted.slice(i, i + ADD_BATCH_SIZE),
        ...this.scope(),
      });
      if (!last.ok) return last;
    }
    return last;
  }

  /** POST /api/v1/memory/flush — trigger extraction for one session */
  async flush(sessionId: string): Promise<Envelope<unknown>> {
    return this.post("/api/v1/memory/flush", {
      session_id: sessionId,
      ...this.scope(),
    }, FLUSH_TIMEOUT_MS);
  }
}
