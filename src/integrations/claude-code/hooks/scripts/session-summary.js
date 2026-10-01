#!/usr/bin/env node

/**
 * Corti SessionEnd Hook
 * Parses the transcript (host-specific shape) and hands the session facts to
 * the Corti server, which owns the session record. No local session file:
 * the summary is stored server-side so every runtime can read it.
 */

import { readFileSync, existsSync } from 'fs';
import { getConfig } from './utils/config.js';
import { endSession } from './utils/corti-api.js';
import { debug, setDebugPrefix } from './utils/debug.js';

setDebugPrefix('session-end');

// Same prefix the Stop hook uses to write episodes, so the stored digest and
// the episodes it describes share one session key.
const CORTI_SESSION_PREFIX = 'claude-code-live';

/**
 * Read transcript and extract key content
 * @param {string} transcriptPath - Path to the transcript JSONL file
 * @returns {Object|null} Extracted content
 */
function extractTranscriptContent(transcriptPath) {
  try {
    if (!existsSync(transcriptPath)) {
      return null;
    }

    const content = readFileSync(transcriptPath, 'utf8');
    const lines = content.trim().split('\n').filter(Boolean);

    let firstUserPrompt = null;
    let turnCount = 0;
    let firstTimestamp = null;
    let lastTimestamp = null;

    for (const line of lines) {
      try {
        const entry = JSON.parse(line);

        // Track timestamps
        if (entry.timestamp) {
          if (!firstTimestamp) firstTimestamp = entry.timestamp;
          lastTimestamp = entry.timestamp;
        }

        // Count turns
        if (entry.type === 'system' && entry.subtype === 'turn_duration') {
          turnCount++;
        }

        // Extract user messages (not tool_result)
        if (entry.type === 'user' && entry.message?.role === 'user') {
          const msgContent = entry.message.content;
          if (typeof msgContent === 'string' && msgContent.trim()) {
            if (!firstUserPrompt) {
              firstUserPrompt = msgContent.trim();
            }
          }
        }
      } catch { }
    }

    return {
      firstUserPrompt: firstUserPrompt?.substring(0, 200) || '',
      turnCount,
      firstTimestamp,
      lastTimestamp
    };
  } catch {
    return null;
  }
}

async function main() {
  // Read hook input
  let hookInput = {};
  try {
    let input = '';
    for await (const chunk of process.stdin) {
      input += chunk;
    }
    if (input) {
      hookInput = JSON.parse(input);
    }
  } catch {
    process.exit(0);
  }

  const { session_id, transcript_path, cwd, reason } = hookInput;

  // Skip if no transcript or session id
  if (!transcript_path || !session_id) {
    process.exit(0);
  }

  // Set cwd for config
  if (cwd) {
    process.env.CORTI_CWD = cwd;
  }

  const config = getConfig();
  if (!config.isConfigured) {
    process.exit(0);
  }

  // Extract content from transcript
  const content = extractTranscriptContent(transcript_path);
  if (!content || content.turnCount === 0) {
    process.exit(0);
  }

  const response = await endSession({
    sessionId: `${CORTI_SESSION_PREFIX}-${session_id}`,
    firstPrompt: content.firstUserPrompt,
    turnCount: content.turnCount,
    startedAt: content.firstTimestamp,
    endedAt: content.lastTimestamp,
    reason
  });

  if (!response.ok) {
    debug('session/end error:', response.error);
    process.exit(0);
  }

  const message = response.data?.display || '';

  // Log to unified debug file
  debug('output', message);

  console.log(JSON.stringify({ systemMessage: message }));
}

main().catch(() => process.exit(0));
