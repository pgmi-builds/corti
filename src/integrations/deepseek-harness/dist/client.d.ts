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
    /** POST /api/v1/memory/search */
    search(query: string, opts?: {
        topK?: number;
        method?: string;
    }): Promise<Envelope<{
        episodes: Episode[];
    }>>;
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
