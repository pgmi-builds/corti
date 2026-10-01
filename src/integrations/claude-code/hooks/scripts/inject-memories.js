#!/usr/bin/env node

/**
 * Memory Plugin - UserPromptSubmit Hook
 *
 * Hands the prompt to the Corti server's /prefetch endpoint, which decides
 * whether anything is worth recalling and renders the block. The trivial
 * prompt rule, the score threshold and the block format all live server-side.
 *
 * Flow:
 * 1. Read prompt from stdin
 * 2. POST it to /api/v1/memory/prefetch
 * 3. Inject nothing when the server reports `skipped` (a normal outcome)
 * 4. Otherwise show the display (systemMessage) and inject the block
 *    (additionalContext) verbatim
 */

import { getConfig } from './utils/config.js';
import { prefetchMemories } from './utils/corti-api.js';
import { debug, setDebugPrefix } from './utils/debug.js';

// Set debug prefix for this script
setDebugPrefix('inject');

/**
 * Main hook handler
 */
async function main() {
  try {
    // Read stdin
    const input = await readStdin();
    const data = JSON.parse(input);
    const prompt = data.prompt || '';

    debug('hookInput:', data);

    // Set cwd from hook input for config.getGroupId()
    if (data.cwd) {
      process.env.CORTI_CWD = data.cwd;
    }

    // Skip if not configured (always true for OSS, but keep the pattern)
    const config = getConfig();
    if (!config.isConfigured) {
      debug('skipped: not configured');
      process.exit(0);
    }

    const response = await prefetchMemories(prompt, { sessionId: data.session_id });
    if (!response.ok) {
      debug('prefetch API error:', response.error);
      process.exit(0);
    }

    const { skipped, block, display } = response.data || {};

    // The server decided nothing is worth injecting - this is normal
    if (skipped) {
      debug('skipped:', skipped);
      process.exit(0);
    }

    // Output JSON with systemMessage (user display) and additionalContext (for Claude)
    const output = {
      systemMessage: display,
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: block
      }
    };

    debug('output:', { systemMessage: display, contextLength: block?.length || 0 });
    process.stdout.write(JSON.stringify(output));
    process.exit(0);

  } catch (error) {
    // Silent on errors - don't block user workflow
    debug('error:', error.message);
    process.exit(0);
  }
}

/**
 * Read all stdin input
 * @returns {Promise<string>}
 */
function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';

    process.stdin.setEncoding('utf8');

    process.stdin.on('data', chunk => {
      data += chunk;
    });

    process.stdin.on('end', () => {
      resolve(data);
    });

    process.stdin.on('error', reject);
  });
}

// Run
main();
