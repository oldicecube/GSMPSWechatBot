/**
 * LX Music custom source runtime (aligned with lx-music-desktop preload.js).
 */
import {
  createCipheriv,
  createHash,
  publicEncrypt,
  randomBytes,
  constants,
} from 'node:crypto';
import {
  copyFileSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { URL, URLSearchParams } from 'node:url';
import zlib from 'node:zlib';

const EVENT_NAMES = {
  request: 'request',
  inited: 'inited',
  updateAlert: 'updateAlert',
};

const DEFAULT_MAX_SCRIPT_BYTES = 2 * 1024 * 1024;
const DEFAULT_UPDATE_MIN_INTERVAL_MS = 24 * 60 * 60 * 1000;

export function parseScriptMeta(scriptText) {
  const meta = {
    name: 'custom',
    version: '1',
    description: '',
    author: '',
    homepage: '',
    updateUrl: '',
  };
  const match = /^\/\*[\S\s]+?\*\//.exec(scriptText || '');
  if (!match) {
    return meta;
  }
  for (const line of match[0].split(/\r?\n/)) {
    const result = /^\s?\*\s?@(\w+)\s(.+)$/.exec(line);
    if (!result) {
      continue;
    }
    const key = result[1].toLowerCase();
    if (key in meta) {
      meta[key] = result[2].trim();
    }
  }
  if (!meta.version) {
    meta.version = '1';
  }
  return meta;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function md5Text(text) {
  return createHash('md5').update(text).digest('hex');
}

function isHttpsUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:';
  } catch (_) {
    return false;
  }
}

function httpRequest(url, options = {}, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = { ...(options.headers || {}) };
    let body = null;

    if (options.body != null) {
      body = options.body;
    } else if (options.form) {
      body = new URLSearchParams(options.form).toString();
      if (!headers['Content-Type'] && !headers['content-type']) {
        headers['Content-Type'] = 'application/x-www-form-urlencoded';
      }
    } else if (options.formData) {
      const boundary = `----lxform${randomBytes(8).toString('hex')}`;
      const chunks = [];
      for (const [key, value] of Object.entries(options.formData)) {
        chunks.push(
          `--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`,
        );
      }
      chunks.push(`--${boundary}--\r\n`);
      body = chunks.join('');
      if (!headers['Content-Type'] && !headers['content-type']) {
        headers['Content-Type'] = `multipart/form-data; boundary=${boundary}`;
      }
    }

    const lib = url.startsWith('https') ? https : http;
    const req = lib.request(
      url,
      { method, headers },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks);
          let parsedBody = raw.toString('utf8');
          const response = {
            statusCode: res.statusCode,
            statusMessage: res.statusMessage,
            headers: res.headers,
            bytes: raw.length,
            raw,
            body: parsedBody,
          };
          try {
            response.body = JSON.parse(parsedBody);
            parsedBody = response.body;
          } catch (_) {
            // keep string body
          }
          resolve({ response, body: parsedBody });
        });
      },
    );

    req.on('error', reject);
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error('request timeout'));
    });
    if (body != null) {
      req.write(body);
    }
    req.end();
  });
}

function downloadText(url, timeoutMs, maxBytes) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout: timeoutMs }, (res) => {
      if (res.statusCode && res.statusCode >= 400) {
        reject(new Error(`download failed: HTTP ${res.statusCode}`));
        res.resume();
        return;
      }
      const chunks = [];
      let total = 0;
      res.on('data', (chunk) => {
        total += chunk.length;
        if (total > maxBytes) {
          req.destroy(new Error('download too large'));
          return;
        }
        chunks.push(chunk);
      });
      res.on('end', () => {
        resolve(Buffer.concat(chunks).toString('utf8'));
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error('download timeout'));
    });
  });
}

function validateScriptText(scriptText) {
  if (!scriptText || typeof scriptText !== 'string') {
    throw new Error('empty script');
  }
  if (!/^\/\*/.test(scriptText)) {
    throw new Error('invalid script header');
  }
  if (scriptText.length > DEFAULT_MAX_SCRIPT_BYTES) {
    throw new Error('script too large');
  }
  parseScriptMeta(scriptText);
}

function atomicWriteScript(targetPath, scriptText) {
  const backupPath = `${targetPath}.bak`;
  const tempPath = `${targetPath}.tmp`;
  writeFileSync(tempPath, scriptText, { encoding: 'utf8' });
  try {
    copyFileSync(targetPath, backupPath);
  } catch (_) {
    // first install may not have backup source
  }
  renameSync(tempPath, targetPath);
}

export class LxScriptHost {
  constructor(options = {}) {
    this.scriptPath = options.scriptPath || '';
    this.scriptText = options.scriptText || '';
    this.scriptMeta = parseScriptMeta(this.scriptText);
    this.autoUpdate = options.autoUpdate !== false;
    this.updateMinIntervalMs = Number(options.updateMinIntervalMs) > 0
      ? Number(options.updateMinIntervalMs)
      : DEFAULT_UPDATE_MIN_INTERVAL_MS;
    this.maxScriptBytes = Number(options.maxScriptBytes) > 0
      ? Number(options.maxScriptBytes)
      : DEFAULT_MAX_SCRIPT_BYTES;
    this.defaultTimeoutMs = Number(options.defaultTimeoutMs) > 0
      ? Number(options.defaultTimeoutMs)
      : 20000;

    this.requestHandler = null;
    this.initDone = false;
    this.initError = null;
    this.isInitedApi = false;
    this.isShowedUpdateAlert = false;
    this.updating = false;
    this.lastUpdateAttemptMs = 0;
    this.pendingUpdate = null;
    this.updateLog = '';
    this.supportedPlatforms = [];
    this._processHooksInstalled = false;
  }

  loadFromFile(scriptPath) {
    this.scriptPath = scriptPath;
    this.scriptText = readFileSync(scriptPath, 'utf8');
    this.scriptMeta = parseScriptMeta(this.scriptText);
    this.resetRuntimeState();
    return this;
  }

  resetRuntimeState() {
    this.requestHandler = null;
    this.initDone = false;
    this.initError = null;
    this.isInitedApi = false;
    this.isShowedUpdateAlert = false;
    this.supportedPlatforms = [];
  }

  get scriptMd5() {
    return md5Text(this.scriptText);
  }

  failInit(message) {
    if (!this.initDone && !this.initError) {
      this.initError = String(message || 'source init failed');
    }
  }

  createLxGlobal(timeoutMs = this.defaultTimeoutMs) {
    const host = this;
    const lx = {
      EVENT_NAMES,
      version: '2.0.0',
      env: 'desktop',
      currentScriptInfo: {
        name: host.scriptMeta.name,
        description: host.scriptMeta.description,
        version: host.scriptMeta.version,
        author: host.scriptMeta.author,
        homepage: host.scriptMeta.homepage,
        rawScript: host.scriptText,
      },
      send(eventName, data) {
        return new Promise((resolve, reject) => {
          if (!Object.values(EVENT_NAMES).includes(eventName)) {
            reject(new Error(`The event is not supported: ${eventName}`));
            return;
          }
          switch (eventName) {
            case EVENT_NAMES.inited:
              if (host.isInitedApi) {
                reject(new Error('Script is inited'));
                return;
              }
              host.isInitedApi = true;
              host.initDone = true;
              host.supportedPlatforms = Object.keys(data?.sources || {}).filter(
                (key) => data?.sources?.[key]?.type === 'music',
              );
              resolve();
              break;
            case EVENT_NAMES.updateAlert:
              if (host.isShowedUpdateAlert) {
                reject(new Error('The update alert can only be called once.'));
                return;
              }
              host.isShowedUpdateAlert = true;
              host.pendingUpdate = data;
              host.updateLog = String(data?.log || '');
              resolve();
              break;
            default:
              reject(new Error(`Unknown event name: ${eventName}`));
          }
        });
      },
      on(eventName, handler) {
        if (eventName !== EVENT_NAMES.request) {
          return Promise.reject(new Error(`The event is not supported: ${eventName}`));
        }
        host.requestHandler = handler;
        return Promise.resolve();
      },
      request(url, options, callback) {
        const requestTimeout = Math.min(
          Number(options?.timeout) > 0 ? Number(options.timeout) : timeoutMs,
          timeoutMs,
        );
        httpRequest(url, options || {}, requestTimeout)
          .then(({ response, body }) => callback(null, response, body))
          .catch((err) => callback(err, null, null));
        return () => {};
      },
      utils: {
        crypto: {
          aesEncrypt(buffer, mode, key, iv) {
            const cipher = createCipheriv(mode, key, iv);
            return Buffer.concat([cipher.update(buffer), cipher.final()]);
          },
          rsaEncrypt(buffer, key) {
            const padded = Buffer.concat([Buffer.alloc(128 - buffer.length), buffer]);
            return publicEncrypt({ key, padding: constants.RSA_NO_PADDING }, padded);
          },
          randomBytes(size) {
            return randomBytes(size);
          },
          md5(str) {
            return md5Text(str);
          },
        },
        buffer: {
          from(...args) {
            return Buffer.from(...args);
          },
          bufToString(buf, format) {
            return Buffer.from(buf, 'binary').toString(format);
          },
        },
        zlib: {
          inflate(buf) {
            return new Promise((resolve, reject) => {
              zlib.inflate(buf, (err, data) => {
                if (err) {
                  reject(err);
                } else {
                  resolve(data);
                }
              });
            });
          },
          deflate(data) {
            return new Promise((resolve, reject) => {
              zlib.deflate(data, (err, buf) => {
                if (err) {
                  reject(err);
                } else {
                  resolve(buf);
                }
              });
            });
          },
        },
      },
    };
    return lx;
  }

  executeScript(timeoutMs = this.defaultTimeoutMs) {
    this.resetRuntimeState();
    if (!this._processHooksInstalled) {
      this._processHooksInstalled = true;
      process.on('uncaughtException', (err) => this.failInit(err?.message || err));
      process.on('unhandledRejection', (err) => this.failInit(err?.message || err));
    }
    globalThis.lx = this.createLxGlobal(timeoutMs);
    // eslint-disable-next-line no-eval
    eval(this.scriptText);
  }

  async waitForInit(timeoutMs = this.defaultTimeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (!this.initDone && !this.initError && Date.now() < deadline) {
      await sleep(30);
    }
    if (this.initError) {
      throw new Error(`init failed: ${this.initError}`);
    }
    if (!this.initDone) {
      throw new Error('source init timeout (check script version / network)');
    }
    if (!this.requestHandler) {
      throw new Error('request handler not registered');
    }
  }

  async maybeAutoUpdate(timeoutMs = this.defaultTimeoutMs) {
    if (!this.autoUpdate || !this.pendingUpdate) {
      return false;
    }
    const updateUrl = String(this.pendingUpdate.updateUrl || '').trim();
    this.pendingUpdate = null;
    if (!updateUrl || !isHttpsUrl(updateUrl)) {
      return false;
    }
    const now = Date.now();
    if (now - this.lastUpdateAttemptMs < this.updateMinIntervalMs) {
      return false;
    }
    if (this.updating) {
      return false;
    }

    this.updating = true;
    this.lastUpdateAttemptMs = now;
    try {
      const newScript = await downloadText(updateUrl, timeoutMs, this.maxScriptBytes);
      validateScriptText(newScript);
      if (md5Text(newScript) === this.scriptMd5) {
        return false;
      }
      if (!this.scriptPath) {
        throw new Error('script path is not configured');
      }
      atomicWriteScript(this.scriptPath, newScript);
      this.scriptText = newScript;
      this.scriptMeta = parseScriptMeta(newScript);
      this.executeScript(timeoutMs);
      await this.waitForInit(timeoutMs);
      return true;
    } catch (err) {
      this.updateLog = String(err?.message || err);
      return false;
    } finally {
      this.updating = false;
    }
  }

  async init(timeoutMs = this.defaultTimeoutMs) {
    this.executeScript(timeoutMs);
    await this.waitForInit(timeoutMs);
    await this.maybeAutoUpdate(timeoutMs);
  }

  async reload(timeoutMs = this.defaultTimeoutMs) {
    if (!this.scriptPath) {
      throw new Error('script path is not configured');
    }
    this.loadFromFile(this.scriptPath);
    await this.init(timeoutMs);
  }

  async musicUrl(platform, quality, musicInfo, timeoutMs = this.defaultTimeoutMs) {
    while (this.updating) {
      await sleep(50);
    }
    if (!this.initDone || !this.requestHandler) {
      await this.init(timeoutMs);
    }

    const url = await Promise.race([
      this.requestHandler({
        source: platform,
        action: 'musicUrl',
        info: {
          type: quality,
          musicInfo,
        },
      }),
      sleep(timeoutMs).then(() => {
        throw new Error('musicUrl timeout');
      }),
    ]);

    if (typeof url !== 'string' || !/^https?:\/\//.test(url)) {
      throw new Error('invalid music url returned');
    }
    return url;
  }
}

export function buildLxMusicInfo(musicInfo = {}) {
  const source = String(musicInfo.source || 'wy');
  const songId = String(
    musicInfo.id
      || musicInfo.songmid
      || musicInfo.meta?.songId
      || '',
  );
  const intervalValue = musicInfo.interval;
  let interval = null;
  if (typeof intervalValue === 'number' && intervalValue > 0) {
    const total = Math.floor(intervalValue);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    interval = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  } else if (typeof intervalValue === 'string' && intervalValue.trim()) {
    interval = intervalValue.trim();
  }

  return {
    id: songId,
    songmid: songId,
    name: String(musicInfo.name || ''),
    singer: String(musicInfo.singer || musicInfo.artist || ''),
    source,
    interval,
    meta: {
      songId,
      albumName: String(musicInfo.albumName || musicInfo.album || ''),
      picUrl: musicInfo.meta?.picUrl ?? null,
      qualitys: musicInfo.meta?.qualitys || [],
      _qualitys: musicInfo.meta?._qualitys || {},
      albumId: musicInfo.meta?.albumId,
    },
  };
}
