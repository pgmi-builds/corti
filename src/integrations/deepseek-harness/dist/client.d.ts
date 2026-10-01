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
export declare class CortiClient {
    private readonly cfg;
    /**
     * High-water mark of timestamps sent by this client (any session).
     * Corti derives message_id from (session_id, timestamp_ms, per-batch
     * idx); overlapping async `add()` calls for the same session can land
     * in the same millisecond and collide on the PK (silent INSERT OR
     * IGNORE drops). A client-level monotonic clock guarantees uniqueness
     * for every session this client writes.
     */
    private lastTs;
    constructor(cfg: CortiConfig);
    private scope;
    private post;
    /**
     * POST /api/v1/memory/search — recall for the model-facing tool.
     *
     * Degradation is the server's job: a leg that is unavailable is substituted
     * server-side and reported in `degraded[]`, so this client never re-issues a
     * request as a different method.
     */
    search(query: string, opts?: {
        topK?: number;
        method?: string;
    }): Promise<Envelope<{
        episodes: Episode[];
        degraded?: string[];
    }>>;
    /**
     * POST /api/v1/memory/session/start — the once-per-session injected block.
     *
     * The server owns selection, ordering, truncation and the block text; the
     * caller forwards scope plus host facts only. Options left undefined are
     * dropped from the JSON body, so the server's own defaults decide.
     */
    sessionStart(opts?: SessionStartOptions): Promise<Envelope<SessionStartResult>>;
    /**
     * POST /api/v1/memory/prefetch — the per-turn recalled block.
     *
     * A non-null `skipped` in the response means "inject nothing"; that is a
     * normal outcome, not an error.
     */
    prefetch(opts: PrefetchOptions): Promise<Envelope<PrefetchResult>>;
    /**
     * POST /api/v1/memory/session/end — record a finished session so any
     * runtime's `/session/start` can report it as "last session".
     */
    sessionEnd(payload: SessionEndPayload): Promise<Envelope<SessionEndResult>>;
    /** POST /api/v1/memory/get — recent memories, newest first (`page` is 1-based). */
    recent(pageSize?: number, page?: number): Promise<Envelope<{
        memories?: Episode[];
        episodes?: Episode[];
        items?: Episode[];
    }>>;
    /** POST /api/v1/memory/add — messages chunked into batches */
    add(sessionId: string, messages: ReadonlyArray<{
        role: "user" | "assistant";
        content: string;
        timestamp?: number;
    }>): Promise<Envelope<unknown>>;
    /** POST /api/v1/memory/flush — trigger extraction for one session */
    flush(sessionId: string): Promise<Envelope<unknown>>;
}
export {};
