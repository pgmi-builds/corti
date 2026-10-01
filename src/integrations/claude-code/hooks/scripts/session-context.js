#!/usr/bin/env node

/**
 * Corti SessionStart Hook
 * Asks the Corti server for the once-per-session block and injects it verbatim.
 * No local recall policy: what to inject, how much, and in what order is the
 * server's decision (see /api/v1/memory/session/start).
 */

// Check Node.js version early
const nodeVersion = process.versions?.node;
if (!nodeVersion) {
  console.error(JSON.stringify({
    continue: true,
    systemMessage: '⚠️ Corti: Node.js environment not detected. Please install Node.js 18+ to use Corti.'
  }));
  process.exit(0);
}

const [major] = nodeVersion.split('.').map(Number);
if (major < 18) {
  console.error(JSON.stringify({
    continue: true,
    systemMessage: `⚠️ Corti: Node.js ${nodeVersion} is too old. Please upgrade to Node.js 18+.`
  }));
  process.exit(0);
}

import { startSession } from './utils/corti-api.js';
import { getConfig } from './utils/config.js';
import { debug, setDebugPrefix } from './utils/debug.js';

setDebugPrefix('session-start');

async function main() {
  // Read hook input to get cwd
  let hookInput = {};
  try {
    let input = '';
    for await (const chunk of process.stdin) {
      input += chunk;
    }
    if (input) {
      hookInput = JSON.parse(input);
    }
  } catch (parseError) {
    console.log(JSON.stringify({
      continue: true,
      systemMessage: `⚠️ Corti: Failed to parse hook input - ${parseError.message}`
    }));
    return;
  }

  // Set cwd from hook input
  if (hookInput.cwd) {
    process.env.CORTI_CWD = hookInput.cwd;
  }

  const config = getConfig();

  if (!config.isConfigured) {
    console.log(JSON.stringify({ continue: true }));
    return;
  }

  try {
    // The server renders both strings from the shared memory store
    const response = await startSession({ sessionId: hookInput.session_id });
    if (!response.ok) {
      debug('startSession error:', response.error);
      const detail = typeof response.error === 'string'
        ? response.error
        : `HTTP ${response.status}`;
      console.log(JSON.stringify({
        continue: true,
        systemMessage: `⚠️ Corti: Session context unavailable (${detail}) - starting without it.`
      }));
      return;
    }

    const { block, display } = response.data || {};

    // Output: display to the user, block into the model's context
    console.log(JSON.stringify({
      continue: true,
      systemMessage: display,
      systemPrompt: block
    }));

  } catch (error) {
    // Don't block session start on errors
    debug('error:', error.message);
    console.log(JSON.stringify({
      continue: true,
      systemMessage: `⚠️ Corti: ${error.name}: ${error.message}`
    }));
  }
}

// Top-level error handler for uncaught exceptions during module load
process.on('uncaughtException', (error) => {
  let userMessage = '⚠️ Corti SessionStart failed: ';

  if (error.code === 'ERR_MODULE_NOT_FOUND') {
    const moduleName = error.message.match(/Cannot find package '([^']+)'/)?.[1] || 'unknown';
    userMessage += `Missing dependency '${moduleName}'. Run: cd ${process.cwd()} && npm install`;
  } else if (error.code === 'ERR_REQUIRE_ESM') {
    userMessage += `Module format error. Ensure package.json has "type": "module"`;
  } else {
    userMessage += `${error.name}: ${error.message}`;
  }

  console.log(JSON.stringify({
    continue: true,
    systemMessage: userMessage
  }));
  process.exit(0);
});

main();
