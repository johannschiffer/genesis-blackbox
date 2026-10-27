"""
Genesis Blackbox: Embedded Local Timeline & Autopsy Web Server
Zero external dependencies. Powered by Python standard library http.server.
"""

import os
import sys
import json
import urllib.parse
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional

from ..recorder.wal_ring import WalRingBuffer
from ..ast_engine.graph import CodebaseGraph
from ..compiler.autopsy import CausalCrashCompiler


EMBEDDED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Genesis Blackbox: Flight Recorder & Causal Autopsy</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --text-muted: #8b949e;
      --accent: #58a6ff;
      --danger: #f85149;
      --success: #3fb950;
      --warning: #d29922;
      --purple: #bc8cff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 20px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand h1 {
      font-size: 1.25rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
    .badge-live {
      background: rgba(63, 185, 80, 0.15);
      color: var(--success);
      border: 1px solid rgba(63, 185, 80, 0.4);
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .crash-banner {
      background: rgba(248, 81, 73, 0.1);
      border: 1px solid rgba(248, 81, 73, 0.3);
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 24px;
    }
    .crash-banner.healthy {
      background: rgba(63, 185, 80, 0.1);
      border-color: rgba(63, 185, 80, 0.3);
    }
    .crash-banner h2 {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 24px;
    }
    @media (min-width: 1024px) {
      .grid { grid-template-columns: 3fr 2fr; }
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }
    .card-header {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      font-size: 0.9rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .timeline-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }
    .timeline-table th {
      text-align: left;
      padding: 10px 14px;
      background: rgba(255, 255, 255, 0.02);
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }
    .timeline-table td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    .tag {
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .tag-http { background: rgba(88, 166, 255, 0.2); color: var(--accent); }
    .tag-sql { background: rgba(63, 185, 80, 0.2); color: var(--success); }
    .tag-exc { background: rgba(248, 81, 73, 0.2); color: var(--danger); }
    .tag-sys { background: rgba(188, 140, 255, 0.2); color: var(--purple); }
    pre {
      background: #090d13;
      padding: 12px;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.82rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .autopsy-content {
      padding: 16px;
      max-height: 600px;
      overflow-y: auto;
    }
    .refresh-btn {
      background: var(--border);
      color: var(--text);
      border: none;
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.8rem;
    }
    .refresh-btn:hover { background: #3c444d; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>Genesis Blackbox</h1>
      <span class="badge-live">Flight Recorder Live</span>
    </div>
    <div>
      <button class="refresh-btn" onclick="fetchState()">Refresh Feed</button>
    </div>
  </header>

  <div id="crash-banner" class="crash-banner">
    <h2 id="crash-title">Analyzing Execution State...</h2>
    <p id="crash-desc" style="color: var(--text-muted); font-size: 0.88rem;"></p>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-header">
        <span>Execution Timeline (Latest Transactions)</span>
        <span id="timeline-count" style="font-size: 0.75rem; color: var(--text-muted);">0 events</span>
      </div>
      <table class="timeline-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Source</th>
            <th>Duration</th>
            <th>Summary / Payload</th>
          </tr>
        </thead>
        <tbody id="timeline-body">
          <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading timeline...</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-header">
        <span>Causal Crash Autopsy (.blackbox.md)</span>
        <button class="refresh-btn" style="padding: 2px 8px;" onclick="copyAutopsy()">Copy</button>
      </div>
      <div class="autopsy-content">
        <pre id="autopsy-text">Compiling autopsy report...</pre>
      </div>
    </div>
  </div>

  <script>
    async function fetchState() {
      try {
        const [timeRes, autoRes] = await Promise.all([
          fetch('/api/timeline?limit=30'),
          fetch('/api/autopsy')
        ]);
        const timeData = await timeRes.json();
        const autoData = await autoRes.json();

        // Render Autopsy
        document.getElementById('autopsy-text').innerText = autoData.autopsy_markdown || 'No report available.';

        // Render Banner
        const banner = document.getElementById('crash-banner');
        if (autoData.has_crash) {
          banner.className = 'crash-banner';
          document.getElementById('crash-title').innerText = '🚨 Crash Detected: ' + (autoData.exception_type || 'Exception');
          document.getElementById('crash-desc').innerText = (autoData.exception_message || '') + ' in ' + (autoData.culprit_file || 'unknown') + ':' + (autoData.culprit_line || '');
        } else {
          banner.className = 'crash-banner healthy';
          document.getElementById('crash-title').innerText = 'Operational: Normal Execution State';
          document.getElementById('crash-desc').innerText = 'No unhandled exceptions detected in the circular flight recorder WAL.';
        }

        // Render Timeline
        const events = timeData.events || [];
        document.getElementById('timeline-count').innerText = events.length + ' events';
        const tbody = document.getElementById('timeline-body');
        if (events.length === 0) {
          tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-muted);">No events recorded in WAL.</td></tr>';
          return;
        }

        tbody.innerHTML = events.map(e => {
          let tagClass = 'tag-sys';
          if (e.event_type.startsWith('HTTP')) tagClass = 'tag-http';
          else if (e.event_type.startsWith('SQL')) tagClass = 'tag-sql';
          else if (e.event_type === 'EXCEPTION') tagClass = 'tag-exc';

          const dur = e.duration_ms ? e.duration_ms.toFixed(2) + 'ms' : '-';
          let payloadStr = typeof e.payload === 'object' ? JSON.stringify(e.payload) : String(e.payload);
          if (payloadStr.length > 90) payloadStr = payloadStr.substring(0, 87) + '...';

          return `
            <tr>
              <td><span class="tag ${tagClass}">${e.event_type}</span></td>
              <td style="color:var(--text-muted);">${e.source}</td>
              <td style="font-family:ui-monospace, monospace;">${dur}</td>
              <td style="font-family:ui-monospace, monospace;">${payloadStr}</td>
            </tr>
          `;
        }).join('');
      } catch (err) {
        console.error('Error fetching state:', err);
      }
    }

    function copyAutopsy() {
      const text = document.getElementById('autopsy-text').innerText;
      navigator.clipboard.writeText(text);
      alert('Autopsy capsule copied to clipboard.');
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
        # Silence default standard HTTP request logs to keep terminal output clean
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
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not Found"}).encode("utf-8"))

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
        self.end_headers()
        self.wfile.write(data)

    def _handle_api_timeline(self, query):
        limit = 50
        if "limit" in query:
            try:
                limit = int(query["limit"][0])
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
            "version": "0.2.0",
            "db_path": self.server.db_path
        }
        self._send_json(resp_data)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
