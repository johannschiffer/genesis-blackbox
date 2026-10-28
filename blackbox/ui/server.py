"""
Genesis Blackbox: Embedded Local Timeline & Autopsy Web Server
Zero external dependencies. Powered by Python standard library http.server.
"""

import os
import json
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional

from .. import __version__
from ..recorder.wal_ring import WalRingBuffer
from ..ast_engine.graph import CodebaseGraph
from ..compiler.autopsy import CausalCrashCompiler


EMBEDDED_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Genesis Blackbox: Flight Recorder &amp; Causal Autopsy</title>
  <style>
    :root,
    [data-theme="dark"] {
      --bg: #0d1117;
      --surface: #151b23;
      --surface-subtle: #1b222c;
      --code-bg: #090d13;
      --border: #30363d;
      --text: #e6edf3;
      --text-muted: #9da7b3;
      --accent: #79c0ff;
      --btn-bg: #212830;
      --btn-hover: #2d3642;
      --danger-bg: #2d1215;
      --danger-border: #8e2a32;
      --danger-text: #ffa198;
      --ok-bg: #0f2419;
      --ok-border: #26663b;
      --ok-text: #56d364;
      --tag-http-bg: #12263f;
      --tag-http-text: #79c0ff;
      --tag-sql-bg: #0f2419;
      --tag-sql-text: #56d364;
      --tag-exc-bg: #2d1215;
      --tag-exc-text: #ffa198;
      --tag-sys-bg: #241b35;
      --tag-sys-text: #d2a8ff;
    }

    [data-theme="light"] {
      --bg: #f6f8fa;
      --surface: #ffffff;
      --surface-subtle: #f0f3f6;
      --code-bg: #f0f3f6;
      --border: #d0d7de;
      --text: #1f2328;
      --text-muted: #57606a;
      --accent: #0969da;
      --btn-bg: #f0f3f6;
      --btn-hover: #e4e9ee;
      --danger-bg: #ffebe9;
      --danger-border: #f5b3b0;
      --danger-text: #a40e26;
      --ok-bg: #dafbe1;
      --ok-border: #82e29b;
      --ok-text: #116329;
      --tag-http-bg: #ddf4ff;
      --tag-http-text: #0550ae;
      --tag-sql-bg: #dafbe1;
      --tag-sql-text: #116329;
      --tag-exc-bg: #ffebe9;
      --tag-exc-text: #a40e26;
      --tag-sys-bg: #fbefff;
      --tag-sys-text: #6639ba;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 16px;
      min-height: 100vh;
      overflow-x: hidden;
    }

    @media (min-width: 768px) {
      body { padding: 24px; }
    }

    .shell {
      max-width: 1440px;
      margin: 0 auto;
    }

    header {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }

    .brand {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }

    .brand h1 {
      font-size: 1.25rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }

    .badge-live {
      background: var(--ok-bg);
      color: var(--ok-text);
      border: 1px solid var(--ok-border);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .action-btn {
      background: var(--btn-bg);
      color: var(--text);
      border: 1px solid var(--border);
      min-height: 44px;
      min-width: 44px;
      padding: 8px 14px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 500;
      transition: background-color 120ms ease;
    }

    .action-btn:hover {
      background: var(--btn-hover);
    }

    .action-btn:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }

    @media (prefers-reduced-motion: reduce) {
      .action-btn { transition: none; }
    }

    .crash-banner {
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 20px;
    }

    .crash-banner.healthy {
      background: var(--ok-bg);
      border-color: var(--ok-border);
    }

    .crash-banner h2 {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--danger-text);
    }

    .crash-banner.healthy h2 {
      color: var(--ok-text);
    }

    .crash-desc {
      color: var(--text);
      font-size: 0.88rem;
      overflow-wrap: anywhere;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 20px;
    }

    @media (min-width: 1024px) {
      .grid { grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); }
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      min-width: 0;
    }

    .card-header {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      font-size: 0.92rem;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }

    .meta-count {
      font-size: 0.8rem;
      font-weight: 500;
      color: var(--text-muted);
    }

    .timeline-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      table-layout: fixed;
    }

    .timeline-table th {
      text-align: left;
      padding: 10px 14px;
      background: var(--surface-subtle);
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      font-weight: 600;
    }

    .timeline-table th:nth-child(1) { width: 130px; }
    .timeline-table th:nth-child(2) { width: 120px; }
    .timeline-table th:nth-child(3) { width: 95px; }

    .timeline-table td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      overflow-wrap: anywhere;
    }

    @media (max-width: 767px) {
      .timeline-table thead {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        border: 0;
      }
      .timeline-table,
      .timeline-table tbody,
      .timeline-table tr,
      .timeline-table td {
        display: block;
        width: 100%;
      }
      .timeline-table tr {
        padding: 12px 14px;
        border-bottom: 1px solid var(--border);
      }
      .timeline-table td {
        padding: 4px 0;
        border-bottom: none;
      }
      .timeline-table td[data-label]::before {
        content: attr(data-label) ": ";
        font-weight: 600;
        color: var(--text-muted);
      }
    }

    .empty-cell {
      text-align: center;
      color: var(--text-muted);
      padding: 24px 16px !important;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }

    .muted {
      color: var(--text-muted);
    }

    .tag {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }

    .tag-http { background: var(--tag-http-bg); color: var(--tag-http-text); }
    .tag-sql { background: var(--tag-sql-bg); color: var(--tag-sql-text); }
    .tag-exc { background: var(--tag-exc-bg); color: var(--tag-exc-text); }
    .tag-sys { background: var(--tag-sys-bg); color: var(--tag-sys-text); }

    pre {
      background: var(--code-bg);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 12px;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.82rem;
      overflow-x: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .autopsy-content {
      padding: 16px;
      max-height: 600px;
      overflow-y: auto;
    }

    .sr-status {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-right: 6px;
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <h1>Genesis Blackbox</h1>
        <span id="conn-badge" class="badge-live">Flight Recorder Live</span>
      </div>
      <div class="header-actions">
        <button id="theme-btn" type="button" class="action-btn" aria-pressed="false" onclick="toggleTheme()">Light Theme</button>
        <button id="refresh-btn" type="button" class="action-btn" onclick="fetchState()">Refresh Feed</button>
      </div>
    </header>

    <main>
      <section id="crash-banner" class="crash-banner healthy" aria-live="polite">
        <h2 id="crash-title">Loading Flight Recorder State...</h2>
        <p id="crash-desc" class="crash-desc">Connecting to local SQLite WAL ring-buffer.</p>
      </section>

      <div class="grid">
        <section class="card" aria-labelledby="timeline-heading">
          <div class="card-header">
            <span id="timeline-heading">Execution Timeline (Latest Transactions)</span>
            <span id="timeline-count" class="meta-count">0 events</span>
          </div>
          <table class="timeline-table">
            <thead>
              <tr>
                <th scope="col">Type</th>
                <th scope="col">Source</th>
                <th scope="col">Duration</th>
                <th scope="col">Summary / Payload</th>
              </tr>
            </thead>
            <tbody id="timeline-body">
              <tr><td colspan="4" class="empty-cell">Loading timeline...</td></tr>
            </tbody>
          </table>
        </section>

        <section class="card" aria-labelledby="autopsy-heading">
          <div class="card-header">
            <span id="autopsy-heading">Causal Crash Autopsy (.blackbox.md)</span>
            <div>
              <span id="copy-status" class="sr-status" role="status" aria-live="polite"></span>
              <button id="copy-btn" type="button" class="action-btn" onclick="copyAutopsy()">Copy Autopsy</button>
            </div>
          </div>
          <div class="autopsy-content">
            <pre id="autopsy-text">Compiling autopsy report...</pre>
          </div>
        </section>
      </div>
    </main>
  </div>

  <script>
    (function initTheme() {
      let saved = null;
      try { saved = localStorage.getItem('blackbox-theme'); } catch (_) {}
      const initial = saved || 'dark';
      applyTheme(initial);
    })();

    function applyTheme(theme) {
      const normalized = theme === 'light' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', normalized);
      const btn = document.getElementById('theme-btn');
      if (btn) {
        const isLight = normalized === 'light';
        btn.setAttribute('aria-pressed', isLight ? 'true' : 'false');
        btn.textContent = isLight ? 'Dark Theme' : 'Light Theme';
      }
    }

    function toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = current === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      try { localStorage.setItem('blackbox-theme', next); } catch (_) {}
    }

    function classifyTag(eventType) {
      const upper = String(eventType || '').toUpperCase();
      if (upper.startsWith('HTTP')) return 'tag-http';
      if (upper.startsWith('SQL')) return 'tag-sql';
      if (upper === 'EXCEPTION' || upper.includes('CRASH') || upper.includes('ERROR')) return 'tag-exc';
      return 'tag-sys';
    }

    function renderEmptyRow(tbody, message) {
      tbody.replaceChildren();
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.className = 'empty-cell';
      td.textContent = message;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }

    async function fetchState() {
      const banner = document.getElementById('crash-banner');
      const badge = document.getElementById('conn-badge');
      const tbody = document.getElementById('timeline-body');
      try {
        const [timeRes, autoRes] = await Promise.all([
          fetch('/api/timeline?limit=30'),
          fetch('/api/autopsy')
        ]);
        if (!timeRes.ok || !autoRes.ok) {
          throw new Error('HTTP ' + timeRes.status + '/' + autoRes.status);
        }
        const timeData = await timeRes.json();
        const autoData = await autoRes.json();

        badge.textContent = 'Flight Recorder Live';
        document.getElementById('autopsy-text').textContent = autoData.autopsy_markdown || 'No report available.';

        if (autoData.has_crash) {
          banner.className = 'crash-banner';
          document.getElementById('crash-title').textContent = 'Crash Detected: ' + (autoData.exception_type || 'Exception');
          document.getElementById('crash-desc').textContent =
            (autoData.exception_message || '') +
            ' in ' + (autoData.culprit_file || 'unknown') +
            ':' + (autoData.culprit_line || '');
        } else {
          banner.className = 'crash-banner healthy';
          document.getElementById('crash-title').textContent = 'Operational: Normal Execution State';
          document.getElementById('crash-desc').textContent = 'No unhandled exceptions detected in the circular flight recorder WAL.';
        }

        const events = Array.isArray(timeData.events) ? timeData.events : [];
        document.getElementById('timeline-count').textContent = events.length + (events.length === 1 ? ' event' : ' events');
        if (events.length === 0) {
          renderEmptyRow(tbody, 'No events recorded in WAL.');
          return;
        }

        const fragment = document.createDocumentFragment();
        for (const e of events) {
          const tr = document.createElement('tr');

          const tdType = document.createElement('td');
          tdType.setAttribute('data-label', 'Type');
          const spanTag = document.createElement('span');
          spanTag.className = 'tag ' + classifyTag(e.event_type);
          spanTag.textContent = String(e.event_type || 'EVENT');
          tdType.appendChild(spanTag);

          const tdSource = document.createElement('td');
          tdSource.setAttribute('data-label', 'Source');
          tdSource.className = 'muted';
          tdSource.textContent = String(e.source || '-');

          const tdDur = document.createElement('td');
          tdDur.setAttribute('data-label', 'Duration');
          tdDur.className = 'mono';
          tdDur.textContent = (typeof e.duration_ms === 'number') ? e.duration_ms.toFixed(2) + 'ms' : '-';

          const tdPayload = document.createElement('td');
          tdPayload.setAttribute('data-label', 'Payload');
          tdPayload.className = 'mono';
          let payloadStr = (e.payload !== null && typeof e.payload === 'object')
            ? JSON.stringify(e.payload)
            : String(e.payload ?? '');
          if (payloadStr.length > 110) {
            payloadStr = payloadStr.substring(0, 107) + '...';
          }
          tdPayload.textContent = payloadStr;

          tr.appendChild(tdType);
          tr.appendChild(tdSource);
          tr.appendChild(tdDur);
          tr.appendChild(tdPayload);
          fragment.appendChild(tr);
        }
        tbody.replaceChildren(fragment);
      } catch (err) {
        badge.textContent = 'Connection Offline';
        banner.className = 'crash-banner';
        document.getElementById('crash-title').textContent = 'Connection Error: Local WAL Server Unreachable';
        document.getElementById('crash-desc').textContent = 'Could not fetch /api/timeline or /api/autopsy. Retrying automatically every 3 seconds.';
      }
    }

    async function copyAutopsy() {
      const text = document.getElementById('autopsy-text').textContent || '';
      const statusEl = document.getElementById('copy-status');
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        statusEl.textContent = 'Copied';
      } catch (_) {
        statusEl.textContent = 'Copy unavailable';
      }
      setTimeout(() => { statusEl.textContent = ''; }, 2500);
    }

    fetchState();
    setInterval(fetchState, 3000);
  </script>
</body>
</html>
"""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class BlackboxUIRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    @property
    def wal_ring(self) -> WalRingBuffer:
        return self.server.wal_ring

    @property
    def ast_graph(self) -> CodebaseGraph:
        return self.server.ast_graph

    @property
    def compiler(self) -> CausalCrashCompiler:
        return self.server.compiler

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/timeline":
            self._handle_api_timeline(query)
        elif path == "/api/autopsy":
            self._handle_api_autopsy()
        elif path == "/api/blast-radius":
            self._handle_api_blast_radius(query)
        elif path == "/api/status":
            self._handle_api_status()
        else:
            self._send_json({"error": "Not Found"}, status=404)

    def _serve_html(self):
        static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
        content = EMBEDDED_HTML
        if os.path.isfile(static_file):
            try:
                with open(static_file, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                content = EMBEDDED_HTML

        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _handle_api_timeline(self, query):
        limit = 50
        if "limit" in query:
            try:
                limit = max(1, min(500, int(query["limit"][0])))
            except ValueError:
                limit = 50
        event_type = query.get("event_type", [None])[0]

        events = self.wal_ring.query_recent(limit=limit, event_type=event_type)
        resp_data = {
            "total_events": len(events),
            "filter": {"limit": limit, "event_type": event_type},
            "events": events
        }
        self._send_json(resp_data)

    def _handle_api_autopsy(self):
        recent_exceptions = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        has_crash = len(recent_exceptions) > 0
        autopsy_text = self.compiler.compile()

        exc_info = {}
        if has_crash:
            payload = recent_exceptions[0].get("payload", {})
            if isinstance(payload, dict):
                exc_info = {
                    "exception_type": payload.get("exception_type"),
                    "exception_message": payload.get("exception_message"),
                    "culprit_file": (payload.get("root_cause") or {}).get("filename"),
                    "culprit_line": (payload.get("root_cause") or {}).get("lineno")
                }

        resp_data = {
            "has_crash": has_crash,
            "autopsy_markdown": autopsy_text,
            **exc_info
        }
        self._send_json(resp_data)

    def _handle_api_blast_radius(self, query):
        target_file = query.get("file", [None])[0]
        symbol = query.get("symbol", [None])[0]

        if not target_file:
            self._send_json({"error": "Missing 'file' parameter"}, status=400)
            return

        if not os.path.isfile(target_file):
            self._send_json({"error": f"File not found: {target_file}"}, status=404)
            return

        self.ast_graph.parse_file(target_file)
        if symbol:
            res = self.ast_graph.get_blast_radius(symbol, file_path=target_file)
        else:
            nodes = [n for n, d in self.ast_graph.nodes.items() if d.get("filename") == target_file]
            res = {
                "file": target_file,
                "symbols_count": len(nodes),
                "symbols": nodes,
                "dependencies": {n: list(self.ast_graph.nodes[n].get("calls", [])) for n in nodes}
            }
        self._send_json(res)

    def _handle_api_status(self):
        resp_data = {
            "status": "online",
            "version": __version__,
            "db_path": self.server.db_path
        }
        self._send_json(resp_data)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin", "")
        if origin:
            parsed_origin = urllib.parse.urlparse(origin)
            if parsed_origin.hostname in ("127.0.0.1", "localhost", "::1"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)


def create_ui_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = ".blackbox/wal.sqlite"
) -> ThreadedHTTPServer:
    """Creates and configures the embedded Blackbox UI HTTP server."""
    server = ThreadedHTTPServer((host, port), BlackboxUIRequestHandler)
    server.db_path = db_path
    server.wal_ring = WalRingBuffer(db_path=db_path)
    server.ast_graph = CodebaseGraph()
    server.compiler = CausalCrashCompiler(server.wal_ring, server.ast_graph)
    return server


def start_ui_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = ".blackbox/wal.sqlite",
    open_browser: bool = False,
    run_forever: bool = True
) -> ThreadedHTTPServer:
    """Launches the Blackbox UI server."""
    server = create_ui_server(host=host, port=port, db_path=db_path)
    url = f"http://{host}:{port}/"
    print(f"[Genesis-Blackbox] Local Timeline Web UI running at {url}")
    print(f"[Genesis-Blackbox] Flight Recorder WAL connected: {db_path}")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if run_forever:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[Genesis-Blackbox] Shutting down UI server.")
            server.server_close()

    return server
