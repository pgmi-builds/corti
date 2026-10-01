/**
 * Corti OSS API client
 * 
 * Bridges the legacy cloud plugin's API surface to the local Corti OSS server.
 * 
 * Cloud API -> OSS API mapping:
 *   POST /api/v1/memories/search  -> POST /api/v1/memory/search
 *   POST /api/v1/memories/group   -> POST /api/v1/memory/add
 *   POST /api/v1/memories/get     -> POST /api/v1/memory/get
 *   (implicit async)              -> POST /api/v1/memory/flush  (explicit)
 * 
 * Runtime interop (the server owns the recall policy):
 *   session/start -> the once-per-session block + display
 *   prefetch      -> the once-per-turn block + display (or skipped)
 *   session/end   -> the session record behind session/start
 * 
 * Scoping changes:
 *   group_id, user_id, Bearer token  ->  app_id, project_id, user_id, agent_id (no auth)
 * 
 * sender_id mapping (matches backfill_claude_code.py):
 *   user messages  -> sender_id = "default"
 *   assistant msgs -> sender_id = "pc-claude-code"
 */

import { getConfig } from './config.js';
import { debug, setDebugPrefix } from './debug.js';

setDebugPrefix('CortiAPI');
const TIMEOUT_MS = 30000;

// ── helpers ──────────────────────────────────────────────────────────────────

async function postJSON(url, body, timeoutMs = TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    const text = await response.text();
    let data;
    try { data = JSON.parse(text); } catch { data = null; }
    if (!response.ok) {
      return { ok: false, status: response.status, error: data || text };
    }
    // Every 200 wraps its payload in { request_id, data } (docs/api.md).
    return { ok: true, status: response.status, data: data?.data };
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      return { ok: false, status: 0, error: `timeout after ${timeoutMs}ms` };
    }
    return { ok: false, status: 0, error: error.message };
  }
}

function buildScope() {
  const config = getConfig();
  return {
    app_id: config.appId,
    project_id: config.projectId,
    user_id: config.userId,
  };
}

// ── search ───────────────────────────────────────────────────────────────────

/**
 * Search memories from local Corti OSS.
 * @param {string} query - Search query text
 * @param {Object} options
 * @param {number} options.topK - Max results (default: 10)
 * @param {string} options.retrieveMethod - keyword|vector|hybrid|agentic (default: 'hybrid')
 * @returns {Promise<Object>} Envelope { ok, data?, error? }
 */
export async function searchMemories(query, options = {}) {
  const config = getConfig();
  const { topK = 10, retrieveMethod = 'hybrid' } = options;
  const body = {
    query,
    method: retrieveMethod,
    top_k: topK,
    ...buildScope(),
  };
  debug('searchMemories', { url: `${config.baseUrl}/api/v1/memory/search`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/search`, body);
}

// ── add (store) ──────────────────────────────────────────────────────────────

/**
 * Add messages to Corti OSS.
 * Sends user+assistant as a pair, matching the backfill script's approach.
 * 
 * @param {string} sessionId - Corti session ID
 * @param {Array<{content: string, role: string, timestamp?: number}>} messages
 * @returns {Promise<Object>} Envelope { ok, data?, error? }
 */
export async function addMemories(sessionId, messages) {
  const config = getConfig();
  const formatted = messages.map(m => ({
    sender_id: m.role === 'assistant' ? config.agentId : config.userId,
    sender_name: m.role === 'assistant' ? 'pc-claude-code' : 'user',
    role: m.role,
    timestamp: m.timestamp || Date.now(),
    content: m.content,
  }));
  const body = {
    session_id: sessionId,
    messages: formatted,
    ...buildScope(),
  };
  debug('addMemories', { url: `${config.baseUrl}/api/v1/memory/add`, msgCount: formatted.length });
  return postJSON(`${config.baseUrl}/api/v1/memory/add`, body);
}

/**
 * Flush a session to trigger extraction (episode/atomic_fact/foresight).
 * @param {string} sessionId
 * @returns {Promise<Object>} Envelope { ok, data?, error? }
 */
export async function flushSession(sessionId) {
  const config = getConfig();
  const body = {
    session_id: sessionId,
    ...buildScope(),
  };
  debug('flushSession', { url: `${config.baseUrl}/api/v1/memory/flush`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/flush`, body, 120000);
}

// ── get (recent memories) ────────────────────────────────────────────────────

/**
 * Get recent memories from Corti OSS (newest first).
 * @param {Object} options
 * @param {number} options.pageSize - Results per page (default: 100)
 * @returns {Promise<Object>} Envelope { ok, data?, error? }
 */
export async function getMemories(options = {}) {
  const config = getConfig();
  const { pageSize = 100 } = options;
  const body = {
    memory_type: 'episode',
    page: 1,
    page_size: pageSize,
    ...buildScope(),
  };
  debug('getMemories', { url: `${config.baseUrl}/api/v1/memory/get`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/get`, body);
}

// ── runtime interop (server-owned policy) ────────────────────────────────────
//
// These endpoints render finished text: the block to inject and the one-line
// display. The plugin only forwards scope plus host facts (session id, prompt,
// transcript stats); any option left undefined is dropped from the request
// body so the server's own defaults decide.

/**
 * Fetch the once-per-session block (profile, last session, recent catalog).
 * @param {Object} [options]
 * @param {string} [options.sessionId] - Host session id
 * @param {number} [options.recencyWindow] - Episodes scanned for the catalog
 * @param {number} [options.recencySample] - Random sample size (0 = newest first)
 * @param {number} [options.recentCount] - Catalog size when recencySample is 0
 * @param {number} [options.maxChars] - Character ceiling for the block
 * @param {boolean} [options.includeProfile]
 * @returns {Promise<Object>} Envelope { ok, status, data?, error? }
 */
export async function startSession(options = {}) {
  const config = getConfig();
  const { sessionId, recencyWindow, recencySample, recentCount, maxChars, includeProfile } = options;
  const body = {
    ...buildScope(),
    agent_id: config.agentId,
    session_id: sessionId,
    recency_window: recencyWindow,
    recency_sample: recencySample,
    recent_count: recentCount,
    max_chars: maxChars,
    include_profile: includeProfile,
  };
  debug('startSession', { url: `${config.baseUrl}/api/v1/memory/session/start`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/session/start`, body);
}

/**
 * Fetch the per-turn recall block.
 * A non-null `skipped` in the response is a normal outcome: inject nothing.
 * @param {string} query - The user's prompt
 * @param {Object} [options]
 * @param {string} [options.method] - keyword|vector|hybrid|agentic
 * @param {number} [options.topK]
 * @param {number} [options.minScore]
 * @param {number} [options.maxChars]
 * @param {boolean} [options.includeProfile]
 * @param {string} [options.sessionId]
 * @returns {Promise<Object>} Envelope { ok, status, data?, error? }
 */
export async function prefetchMemories(query, options = {}) {
  const config = getConfig();
  const { method, topK, minScore, maxChars, includeProfile, sessionId } = options;
  const body = {
    query,
    method,
    top_k: topK,
    min_score: minScore,
    max_chars: maxChars,
    include_profile: includeProfile,
    session_id: sessionId,
    agent_id: config.agentId,
    ...buildScope(),
  };
  debug('prefetchMemories', { url: `${config.baseUrl}/api/v1/memory/prefetch`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/prefetch`, body);
}

/**
 * Record a finished session so session/start can report it to any runtime.
 * @param {Object} payload
 * @param {string} payload.sessionId - REQUIRED: the hook payload's session_id
 * @param {string} [payload.firstPrompt] - First user prompt from the transcript
 * @param {number} [payload.turnCount]
 * @param {string} [payload.startedAt] - ISO timestamp
 * @param {string} [payload.endedAt] - ISO timestamp
 * @param {string} [payload.reason]
 * @returns {Promise<Object>} Envelope { ok, status, data?, error? }
 */
export async function endSession(payload = {}) {
  const config = getConfig();
  const { sessionId, firstPrompt, turnCount, startedAt, endedAt, reason } = payload;
  const body = {
    ...buildScope(),
    agent_id: config.agentId,
    session_id: sessionId,
    first_prompt: firstPrompt,
    turn_count: turnCount,
    started_at: startedAt,
    ended_at: endedAt,
    reason,
  };
  debug('endSession', { url: `${config.baseUrl}/api/v1/memory/session/end`, body });
  return postJSON(`${config.baseUrl}/api/v1/memory/session/end`, body);
}

