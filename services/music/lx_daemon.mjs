/**
 * LX Music custom source daemon.
 * Long-lived process: init once, serve musicUrl repeatedly.
 *
 * Protocol: newline-delimited JSON on stdin/stdout.
 * Request:  {"id":1,"action":"musicUrl","platform":"wy","quality":"128k","musicInfo":{},"timeoutMs":20000}
 * Response: {"id":1,"ok":true,"url":"https://..."}
 */
import { createInterface } from 'node:readline';

import { buildLxMusicInfo, LxScriptHost } from './lx_runtime.mjs';

function parseArgs(argv) {
  const options = {
    scriptPath: '',
    autoUpdate: true,
    updateMinIntervalMs: 24 * 60 * 60 * 1000,
    defaultTimeoutMs: 20000,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--script-path') {
      options.scriptPath = argv[i + 1] || '';
      i += 1;
    } else if (arg === '--no-auto-update') {
      options.autoUpdate = false;
    } else if (arg === '--update-min-interval-ms') {
      options.updateMinIntervalMs = Number(argv[i + 1] || options.updateMinIntervalMs);
      i += 1;
    } else if (arg === '--default-timeout-ms') {
      options.defaultTimeoutMs = Number(argv[i + 1] || options.defaultTimeoutMs);
      i += 1;
    }
  }
  if (!options.scriptPath) {
    throw new Error('missing --script-path');
  }
  return options;
}

function writeResponse(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function main() {
  const options = parseArgs(process.argv);
  const host = new LxScriptHost({
    scriptPath: options.scriptPath,
    autoUpdate: options.autoUpdate,
    updateMinIntervalMs: options.updateMinIntervalMs,
    defaultTimeoutMs: options.defaultTimeoutMs,
  });
  host.loadFromFile(options.scriptPath);

  const rl = createInterface({ input: process.stdin });
  let shuttingDown = false;

  try {
    await host.init(options.defaultTimeoutMs);
  } catch (err) {
    writeResponse({
      id: 0,
      ok: false,
      error: err?.message || String(err),
    });
    process.exit(1);
  }

  const handleRequest = async (message) => {
    const requestId = message.id;
    const action = message.action;
    const timeoutMs = Number(message.timeoutMs) > 0
      ? Number(message.timeoutMs)
      : options.defaultTimeoutMs;

    try {
      switch (action) {
        case 'ping':
          writeResponse({
            id: requestId,
            ok: true,
            status: 'ready',
            scriptMd5: host.scriptMd5,
            version: host.scriptMeta.version,
            platforms: host.supportedPlatforms,
          });
          break;
        case 'reload':
          await host.reload(timeoutMs);
          writeResponse({
            id: requestId,
            ok: true,
            status: 'ready',
            scriptMd5: host.scriptMd5,
            version: host.scriptMeta.version,
            platforms: host.supportedPlatforms,
          });
          break;
        case 'musicUrl': {
          const platform = message.platform || 'wy';
          const quality = message.quality || '128k';
          const musicInfo = buildLxMusicInfo(message.musicInfo || {});
          const url = await host.musicUrl(platform, quality, musicInfo, timeoutMs);
          writeResponse({ id: requestId, ok: true, url });
          break;
        }
        case 'shutdown':
          shuttingDown = true;
          writeResponse({ id: requestId, ok: true, status: 'shutdown' });
          rl.close();
          process.exit(0);
          break;
        default:
          writeResponse({
            id: requestId,
            ok: false,
            error: `unknown action: ${action}`,
          });
      }
    } catch (err) {
      writeResponse({
        id: requestId,
        ok: false,
        error: err?.message || String(err),
      });
    }
  };

  rl.on('line', (line) => {
    const trimmed = String(line || '').trim();
    if (!trimmed) {
      return;
    }
    let message;
    try {
      message = JSON.parse(trimmed);
    } catch (err) {
      writeResponse({ id: null, ok: false, error: `invalid json: ${err.message}` });
      return;
    }
    void handleRequest(message);
  });

  rl.on('close', () => {
    if (!shuttingDown) {
      process.exit(0);
    }
  });

  writeResponse({
    id: 0,
    ok: true,
    event: 'started',
    scriptPath: options.scriptPath,
    scriptMd5: host.scriptMd5,
    version: host.scriptMeta.version,
    platforms: host.supportedPlatforms,
  });
}

main().catch((err) => {
  writeResponse({ id: 0, ok: false, error: err?.message || String(err) });
  process.exit(1);
});
