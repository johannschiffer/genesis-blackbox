"""
Genesis Blackbox: Model Context Protocol (MCP) Server
Exposes runtime flight recorder and blast-radius diagnostic tools to AI coding agents over stdio.
"""

import sys
import json
import logging
from typing import Dict, Any, List, Optional
from .recorder.wal_ring import WalRingBuffer
from .ast_engine.graph import CodebaseGraph
from .compiler.autopsy import CausalCrashCompiler

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("BlackboxMCPServer")


class BlackboxMCPServer:
    def __init__(self, db_path: str = "/tmp/genesis_blackbox.db"):
        self.wal_ring = WalRingBuffer(db_path=db_path)
        self.ast_graph = CodebaseGraph()
        self.compiler = CausalCrashCompiler(self.wal_ring, self.ast_graph)
        self.name = "genesis-blackbox"
        self.version = "0.1.0"
        self._init_tools()

    def _init_tools(self):
        self.tools = {
            "blackbox_record_event": {
                "name": "blackbox_record_event",
                "description": "Records an execution event, HTTP transaction, or SQL query into the circular WAL flight recorder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string", "description": "Type of event: HTTP_REQ, SQL_QUERY, LOG, etc."},
                        "payload": {"type": "object", "description": "JSON event payload"},
                        "source": {"type": "string", "description": "Source module name (default: 'agent')"}
                    },
                    "required": ["event_type", "payload"]
                },
                "handler": self._tool_record_event
            },
            "blackbox_query_timeline": {
                "name": "blackbox_query_timeline",
                "description": "Retrieves the recent execution flight recorder timeline leading up to an anomaly or crash.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Maximum events to fetch (default: 50)"}
                    }
                },
                "handler": self._tool_query_timeline
            },
            "blackbox_blast_radius": {
                "name": "blackbox_blast_radius",
                "description": "Calculates upstream callers and downstream callee blast radius for a target function.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_function": {"type": "string", "description": "Function name to calculate blast radius for"}
                    },
                    "required": ["target_function"]
                },
                "handler": self._tool_blast_radius
            }
        }

    def _tool_record_event(self, args: Dict[str, Any]) -> Dict[str, Any]:
        eid = self.wal_ring.record(
            event_type=args["event_type"],
            payload=args["payload"],
            source=args.get("source", "agent")
        )
        return {"status": "recorded", "event_id": eid}

    def _tool_query_timeline(self, args: Dict[str, Any]) -> Dict[str, Any]:
        limit = args.get("limit", 50)
        events = self.wal_ring.query_recent(limit=limit)
        return {"total_events": len(events), "events": events}

    def _tool_blast_radius(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = args["target_function"]
        radius = self.ast_graph.get_blast_radius(target)
        return radius

    def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        method = req.get("method")
        req_id = req.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version}
                }
            }
        elif method == "tools/list":
            tool_list = [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in self.tools.values()
            ]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_list}}
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name in self.tools:
                try:
                    res = self.tools[name]["handler"](args)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(res, indent=2)}]
                        }
                    }
                except Exception as e:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": str(e)}
                    }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool '{name}' not found."}
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found."}
        }

    def run_stdio(self):
        print(f"[Genesis-Blackbox] MCP Server v{self.version} started over stdio.", file=sys.stderr)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                print(json.dumps(resp), flush=True)
            except Exception as e:
                logger.error(f"Error handling request: {e}")


def main():
    server = BlackboxMCPServer()
    server.run_stdio()


if __name__ == "__main__":
    main()
