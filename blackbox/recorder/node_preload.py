"""
Genesis Blackbox: Node.js Runtime Preload Interceptor
Generates zero-dependency Node.js preload hook for active runtime instrumentation
of HTTP transactions, fetch calls, unhandled rejections, and uncaught exceptions.
"""

import os
import json
from typing import Dict, Any, List, Optional
from ..recorder.wal_ring import WalRingBuffer


NODE_PRELOAD_CODE = r"""
const fs = require('fs');
const http = require('http');
const https = require('https');
const { fileURLToPath } = require('url');
const { performance } = require('perf_hooks');

const spoolPath = process.env.BLACKBOX_NODE_SPOOL;

function writeSpool(event) {
  if (!spoolPath) return;
  try {
    const line = JSON.stringify(event) + '\n';
    fs.appendFileSync(spoolPath, line, { encoding: 'utf8' });
  } catch (_) {}
}

const SENSITIVE_KEYS = new Set([
  'authorization', 'cookie', 'set-cookie', 'token', 'api_key', 'apikey',
  'key', 'secret', 'password', 'private', 'credential', 'access_token'
]);

function isSensitiveKey(key) {
  const lk = String(key || '').toLowerCase();
  if (SENSITIVE_KEYS.has(lk)) return true;
  for (const k of SENSITIVE_KEYS) {
    if (lk.includes(k)) return true;
  }
  return false;
}

function sanitizeHeaders(headers) {
  if (!headers) return {};
  const out = {};
  try {
    // Check tuple arrays before .forEach because Array.prototype.forEach passes numeric indices as the second argument.
    if (Array.isArray(headers)) {
      for (const entry of headers) {
        if (Array.isArray(entry) && entry.length >= 2) {
          const key = String(entry[0]);
          out[key] = isSensitiveKey(key) ? '******** [REDACTED]' : String(entry[1]);
        }
      }
    } else if (typeof headers.forEach === 'function') {
      headers.forEach((val, key) => {
        out[key] = isSensitiveKey(key) ? '******** [REDACTED]' : String(val);
      });
    } else if (typeof headers === 'object') {
      for (const [key, val] of Object.entries(headers)) {
        out[key] = isSensitiveKey(key) ? '******** [REDACTED]' : String(val);
      }
    }
  } catch (_) {}
  return out;
}

function sanitizeUrl(rawUrl) {
  if (!rawUrl) return '';
  try {
    const strUrl = String(rawUrl);
    const isFull = strUrl.startsWith('http://') || strUrl.startsWith('https://');
    const u = new URL(strUrl, 'http://localhost');
    if (u.username) u.username = 'REDACTED';
    if (u.password) u.password = 'REDACTED';
    for (const k of Array.from(u.searchParams.keys())) {
      if (isSensitiveKey(k)) {
        u.searchParams.set(k, 'REDACTED');
      }
    }
    return isFull ? u.toString() : (u.pathname + u.search);
  } catch (_) {
    return String(rawUrl);
  }
}

if (typeof globalThis.fetch === 'function') {
  const origFetch = globalThis.fetch;
  globalThis.fetch = async function(...args) {
    const start = performance.now();
    const input = args[0];
    const init = args[1] || {};
    let method = (init.method || (typeof input === 'object' && input && input.method) || 'GET').toUpperCase();
    let urlStr = typeof input === 'string' ? input : (input && input.url ? input.url : String(input));
    urlStr = sanitizeUrl(urlStr);
    const headers = sanitizeHeaders(init.headers || (input && input.headers));

    try {
      const response = await origFetch.apply(this, args);
      const duration = performance.now() - start;
      writeSpool({
        timestamp: Date.now() / 1000,
        event_type: 'HTTP_CLIENT',
        source: 'node_fetch',
        status: response.ok ? 'ok' : 'error',
        duration_ms: duration,
        payload: {
          method,
          url: urlStr,
          status_code: response.status,
          headers
        }
      });
      return response;
    } catch (err) {
      const duration = performance.now() - start;
      writeSpool({
        timestamp: Date.now() / 1000,
        event_type: 'HTTP_CLIENT',
        source: 'node_fetch',
        status: 'error',
        duration_ms: duration,
        payload: {
          method,
          url: urlStr,
          error: String(err && err.message ? err.message : err),
          headers
        }
      });
      throw err;
    }
  };
}

function resolveRequestMeta(modName, args) {
  let first = args[0];
  let second = args[1];
  if (typeof first === 'function') {
    first = {};
    second = {};
  } else if (typeof second === 'function') {
    second = {};
  }
  const isUrlInput = (typeof first === 'string') || (first instanceof URL);
  const options = (!isUrlInput && typeof first === 'object' && first !== null) ? first : (typeof second === 'object' && second !== null ? second : {});
  const method = String(options.method || 'GET').toUpperCase();
  const headers = sanitizeHeaders(options.headers);

  let rawUrl = '';
  if (isUrlInput) {
    try {
      const parsed = first instanceof URL ? new URL(first.toString()) : new URL(first);
      if (options.path) {
        const temp = new URL(options.path, parsed.origin);
        parsed.pathname = temp.pathname;
        parsed.search = temp.search;
      }
      rawUrl = sanitizeUrl(parsed.toString());
    } catch (_) {
      rawUrl = sanitizeUrl(String(first));
    }
  } else {
    const proto = (options.protocol ? String(options.protocol).replace(/:$/, '') : modName);
    const host = options.hostname || options.host || 'localhost';
    const hasPortInHost = String(host).includes(':');
    const portPart = (options.port && !hasPortInHost) ? `:${options.port}` : '';
    const authPart = options.auth ? 'REDACTED:REDACTED@' : '';
    const path = options.path || '/';
    rawUrl = sanitizeUrl(`${proto}://${authPart}${host}${portPart}${path}`);
  }
  return { method, rawUrl, headers };
}

function attachClientListeners(req, modName, meta, start) {
  if (!req || req._blackboxAttached) return req;
  req._blackboxAttached = true;
  req.on('response', (res) => {
    const duration = performance.now() - start;
    writeSpool({
      timestamp: Date.now() / 1000,
      event_type: 'HTTP_CLIENT',
      source: `node_${modName}`,
      status: (res.statusCode >= 400) ? 'error' : 'ok',
      duration_ms: duration,
      payload: {
        method: meta.method,
        url: meta.rawUrl,
        status_code: res.statusCode,
        headers: meta.headers
      }
    });
  });
  req.on('error', (err) => {
    const duration = performance.now() - start;
    writeSpool({
      timestamp: Date.now() / 1000,
      event_type: 'HTTP_CLIENT',
      source: `node_${modName}`,
      status: 'error',
      duration_ms: duration,
      payload: {
        method: meta.method,
        url: meta.rawUrl,
        error: String(err && err.message ? err.message : err),
        headers: meta.headers
      }
    });
  });
  return req;
}

function wrapClientRequest(mod, modName) {
  if (!mod || !mod.request) return;
  const origReq = mod.request;
  mod.request = function(...args) {
    const start = performance.now();
    const meta = resolveRequestMeta(modName, args);
    const req = origReq.apply(this, args);
    return attachClientListeners(req, modName, meta, start);
  };
  if (typeof mod.get === 'function') {
    const origGet = mod.get;
    mod.get = function(...args) {
      const start = performance.now();
      const meta = resolveRequestMeta(modName, args);
      const req = origGet.apply(this, args);
      return attachClientListeners(req, modName, meta, start);
    };
  }
}

try {
  wrapClientRequest(http, 'http');
  wrapClientRequest(https, 'https');
} catch (_) {}

function normalizeFrameFile(rawFile) {
  if (!rawFile) return '';
  let cleaned = rawFile.trim();
  if (cleaned.startsWith('file://')) {
    try {
      cleaned = fileURLToPath(cleaned);
    } catch (_) {}
  }
  return cleaned;
}

function extractFrames(stackStr) {
  if (!stackStr) return [];
  const lines = stackStr.split('\n');
  const frames = [];
  // Match colons inside file:// URLs and Windows drive paths up to the trailing :line:col pair.
  const frameRegex = /^\s*at\s+(?:(?<func>[^(\n]+?)\s+\((?<file1>.+?):(?<line1>\d+):(?<col1>\d+)\)|(?<file2>.+?):(?<line2>\d+):(?<col2>\d+))\s*$/;
  for (const line of lines) {
    const m = frameRegex.exec(line);
    if (m && m.groups) {
      let func = (m.groups.func || '<anonymous>').trim();
      if (func.startsWith('async ')) {
        func = func.slice(6).trim();
      }
      const file = normalizeFrameFile(m.groups.file1 || m.groups.file2 || '');
      const lineno = parseInt(m.groups.line1 || m.groups.line2 || '1', 10);
      const colno = parseInt(m.groups.col1 || m.groups.col2 || '1', 10);
      frames.push({ function: func, filename: file, lineno, colno });
    }
  }
  return frames;
}

function recordException(err, contextType) {
  const excType = err && err.name ? err.name : (err && err.constructor && err.constructor.name ? err.constructor.name : 'Error');
  const excMsg = err && err.message ? err.message : String(err || '');
  const stack = err && err.stack ? err.stack : '';
  const frames = extractFrames(stack);

  const userFrames = frames.filter(f => !f.filename.startsWith('node:') && !f.filename.includes('node_modules') && !f.filename.startsWith('internal/'));
  const rootCause = userFrames.length > 0 ? { ...userFrames[0] } : (frames.length > 0 ? { ...frames[0] } : { function: 'unknown', filename: '', lineno: 0 });

  rootCause.locals = {};
  const propMatch = /reading ['"]([a-zA-Z0-9_$]+)['"]/.exec(excMsg);
  if (propMatch) {
    rootCause.locals[propMatch[1]] = null;
  }

  if (rootCause.filename && fs.existsSync(rootCause.filename)) {
    try {
      const src = fs.readFileSync(rootCause.filename, 'utf8');
      const lines = src.split('\n');
      if (rootCause.lineno > 0 && rootCause.lineno <= lines.length) {
        rootCause.code_line = lines[rootCause.lineno - 1].trim();
      }
    } catch (_) {}
  }

  writeSpool({
    timestamp: Date.now() / 1000,
    event_type: 'EXCEPTION',
    source: `node_${contextType}`,
    status: 'error',
    payload: {
      runtime: 'node',
      exception_type: excType,
      exception_message: excMsg,
      context: contextType,
      root_cause: rootCause,
      frames: frames,
      raw_stderr: stack
    }
  });
}

function normalizeError(err) {
  if (err instanceof Error) return err;
  let msg;
  if (err === null) {
    msg = 'null';
  } else if (err === undefined) {
    msg = 'undefined';
  } else if (typeof err === 'object') {
    try {
      msg = JSON.stringify(err);
    } catch (_) {
      msg = Object.prototype.toString.call(err);
    }
  } else {
    msg = String(err);
  }
  const normalized = new Error(msg);
  normalized.name = (err && err.constructor && err.constructor.name && err.constructor.name !== 'Object') ? err.constructor.name : 'Error';
  return normalized;
}

process.on('uncaughtException', (err) => {
  recordException(normalizeError(err), 'uncaughtException');
  if (process.listenerCount('uncaughtException') <= 1) {
    console.error(err);
    process.exit(1);
  }
});

process.on('unhandledRejection', (reason) => {
  recordException(normalizeError(reason), 'unhandledRejection');
  if (process.listenerCount('unhandledRejection') <= 1) {
    console.error('UnhandledPromiseRejection:', reason);
    process.exit(1);
  }
});
"""


def write_node_preload_script(target_dir: str) -> str:
    """Writes the zero-dependency Node.js preload CJS script into the target directory."""
    os.makedirs(target_dir, exist_ok=True)
    preload_path = os.path.join(target_dir, "blackbox_preload.cjs")
    with open(preload_path, "w", encoding="utf-8") as f:
        f.write(NODE_PRELOAD_CODE.strip())
    return preload_path


def ingest_node_spool(spool_path: Optional[str], wal_ring: WalRingBuffer) -> int:
    """Reads JSONL events recorded by Node.js preload hook and ingests them into WalRingBuffer."""
    if not spool_path or not os.path.isfile(spool_path):
        return 0

    count = 0
    try:
        with open(spool_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    wal_ring.record(
                        event_type=event.get("event_type", "LOG"),
                        payload=event.get("payload", {}),
                        source=event.get("source", "node"),
                        duration_ms=event.get("duration_ms"),
                        status=event.get("status", "ok"),
                        timestamp=event.get("timestamp")
                    )
                    count += 1
                except Exception:
                    pass
        try:
            os.remove(spool_path)
        except Exception:
            pass
    except Exception:
        pass
    return count
