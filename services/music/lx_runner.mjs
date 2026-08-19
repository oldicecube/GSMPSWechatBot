/**
 * LX Music custom source runner (one-shot CLI, for debugging).
 * Reads JSON from stdin, writes JSON to stdout.
 */
import { readFileSync } from 'node:fs';

import { buildLxMusicInfo, LxScriptHost } from './lx_runtime.mjs';

function readStdin() {
  return readFileSync(0, 'utf8');
}

function writeResult(payload) {
  process.stdout.write(JSON.stringify(payload));
}

async function main() {
  const input = JSON.parse(readStdin());
  const scriptPath = input.scriptPath;
  const platform = input.platform || 'wy';
  const quality = input.quality || '128k';
  const musicInfo = buildLxMusicInfo(input.musicInfo || {});
  const timeoutMs = Number(input.timeoutMs) > 0 ? Number(input.timeoutMs) : 20000;
  const autoUpdate = input.autoUpdate !== false;

  const host = new LxScriptHost({
    scriptPath,
    autoUpdate,
    defaultTimeoutMs: timeoutMs,
  });
  host.loadFromFile(scriptPath);
  await host.init(timeoutMs);
  const url = await host.musicUrl(platform, quality, musicInfo, timeoutMs);
  writeResult({ ok: true, url });
}

main().catch((err) => {
  writeResult({ ok: false, error: err?.message || String(err) });
});
