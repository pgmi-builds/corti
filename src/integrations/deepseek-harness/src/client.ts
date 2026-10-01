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

/** One episode as returned by the runtime-interop endpoints (catalog/hits). */
export interface RuntimeHit {
  id: string;
  subject: string;
  summary: string;
  timestamp: string;
  score: number;
  sender_ids: string[];
}

/** A finished session as recorded by the server. */
export interface SessionSummary {
  session_id: string;
  user_id: string;
  app_id: string;
  project_id: string;
  agent_id?: string | null;
  first_prompt: string;
  turn_count: number;
  started_at?: string | null;
  ended_at?: string | null;
  reason?: string | null;
  recorded_at: string;
}

/** Response of POST /api/v1/memory/session/start. */
export interface SessionStartResult {
  request_id: string;
  block: string;
  display: string;
  catalog: RuntimeHit[];
  total_episodes: number;
  last_session: SessionSummary | null;
  profile_line: string;
  degraded: string[];
}

/** Response of POST /api/v1/memory/prefetch. */
export interface PrefetchResult {
  request_id: string;
  /** Non-null means "inject nothing" — a normal outcome, not an error. */
  skipped: "trivial_prompt" | "no_relevant_hits" | null;
  block: string;
  display: string;
  hits: RuntimeHit[];
  degraded: string[];
}

/** Response of POST /api/v1/memory/session/end. */
export interface SessionEndResult {
  request_id: string;
  stored: boolean;
  summary: SessionSummary;
  display: string;
}

/** Host facts for POST /api/v1/memory/session/start (undefined fields are dropped). */
export interface SessionStartOptions {
  sessionId?: string;
  recencyWindow?: number;
  recencySample?: number;
  recentCount?: number;
  maxChars?: number;
  includeProfile?: boolean;
}

/** Host facts for POST /api/v1/memory/prefetch (undefined fields are dropped). */
export interface PrefetchOptions {
  query: string;
  method?: string;
  topK?: number;
  minScore?: number;
  maxChars?: number;
  includeProfile?: boolean;
  sessionId?: string;
}

/** Host facts for POST /api/v1/memory/session/end. */
export interface SessionEndPayload {
  sessionId: string;
  firstPrompt?: string;
  turnCount?: number;
  startedAt?: string;
  endedAt?: string;
  reason?: string;
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

  /**
   * POST /api/v1/memory/search — recall for the model-facing tool.
   *
   * Degradation is the server's job: a leg that is unavailable is substituted
   * server-side and reported in `degraded[]`, so this client never re-issues a
   * request as a different method.
   */
  async search(
    query: string,
    opts: { topK?: number; method?: string } = {},
  ): Promise<Envelope<{ episodes: Episode[] }>> {
    return this.post<{ episodes: Episode[] }>("/api/v1/memory/search", {
      query,
      method: opts.method ?? "hybrid",
      top_k: opts.topK ?? 8,
      ...this.scope(),
    });
  }

  /**
   * POST /api/v1/memory/session/start — the once-per-session injected block.
   *
   * The server owns selection, ordering, truncation and the block text; the
   * caller forwards scope plus host facts only. Options left undefined are
   * dropped from the JSON body, so the server's own defaults decide.
   */
  async sessionStart(opts: SessionStartOptions = {}): Promise<Envelope<SessionStartResult>> {
    return this.post<SessionStartResult>("/api/v1/memory/session/start", {
      ...this.scope(),
      agent_id: this.cfg.agentId,
      session_id: opts.sessionId,
      recency_window: opts.recencyWindow,
      recency_sample: opts.recencySample,
      recent_count: opts.recentCount,
      max_chars: opts.maxChars,
      include_profile: opts.includeProfile,
    });
  }

  /**
   * POST /api/v1/memory/prefetch — the per-turn recalled block.
   *
   * A non-null `skipped` in the response means "inject nothing"; that is a
   * normal outcome, not an error.
   */
  async prefetch(opts: PrefetchOptions): Promise<Envelope<PrefetchResult>> {
    return this.post<PrefetchResult>("/api/v1/memory/prefetch", {
      query: opts.query,
      method: opts.method,
      top_k: opts.topK,
      min_score: opts.minScore,
      max_chars: opts.maxChars,
      include_profile: opts.includeProfile,
      session_id: opts.sessionId,
      agent_id: this.cfg.agentId,
      ...this.scope(),
    });
  }

  /**
   * POST /api/v1/memory/session/end — record a finished session so any
   * runtime's `/session/start` can report it as "last session".
   */
  async sessionEnd(payload: SessionEndPayload): Promise<Envelope<SessionEndResult>> {
    return this.post<SessionEndResult>("/api/v1/memory/session/end", {
      ...this.scope(),
      agent_id: this.cfg.agentId,
      session_id: payload.sessionId,
      first_prompt: payload.firstPrompt,
      turn_count: payload.turnCount,
      started_at: payload.startedAt,
      ended_at: payload.endedAt,
      reason: payload.reason,
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
