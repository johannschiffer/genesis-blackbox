"""
Genesis Blackbox: Model Context Protocol (MCP) Server
Exposes runtime flight recorder and blast-radius diagnostic tools to AI coding agents over stdio.
"""

import os
import sys
import json
import logging
from typing import Dict, Any, List, Optional
from .recorder.wal_ring import WalRingBuffer
from .ast_engine.graph import CodebaseGraph
from .compiler.autopsy import CausalCrashCompiler
from .compiler.reproducer import CrashReproducer
from .compiler.verifier import CausalVerifier
from .compiler.patch_validator import PatchValidator



logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("BlackboxMCPServer")


class BlackboxMCPServer:
    def __init__(self, db_path: str = ".blackbox/wal.sqlite"):
        self.default_db_path = db_path
        self.wal_ring = WalRingBuffer(db_path=db_path)
        self.ast_graph = CodebaseGraph()
        self.compiler = CausalCrashCompiler(self.wal_ring, self.ast_graph)
        self.name = "genesis-blackbox"
        self.version = "0.1.4"
        self._init_tools()


    def _init_tools(self):
        self.tools = {
            "get_crash_autopsy": {
                "name": "get_crash_autopsy",
                "description": "Returns compiled .blackbox.md deterministic autopsy document from latest recorded crash in WAL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        }
                    }
                },
                "handler": lambda args: self.get_crash_autopsy(
                    db_path=args.get("db_path", self.default_db_path)
                )
            },
            "get_blast_radius": {
                "name": "get_blast_radius",
                "description": "Calculates upstream callers and downstream callee dependencies for a target file and symbol.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Source python file to analyze"},
                        "symbol": {"type": "string", "description": "Target function or class name (optional)"}
                    },
                    "required": ["file_path"]
                },
                "handler": lambda args: self.get_blast_radius(
                    file_path=args["file_path"],
                    symbol=args.get("symbol")
                )
            },
            "query_execution_timeline": {
                "name": "query_execution_timeline",
                "description": "Queries high-resolution execution timeline of HTTP, SQL, and exception events in WAL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Maximum events to fetch (default: 50)"},
                        "event_type": {"type": "string", "description": "Filter by event type (HTTP_REQ, SQL_QUERY, EXCEPTION, etc.)"}
                    }
                },
                "handler": lambda args: self.query_execution_timeline(
                    limit=args.get("limit", 50),
                    event_type=args.get("event_type")
                )
            },
            "record_event": {
                "name": "record_event",
                "description": "Records an execution event, HTTP transaction, or SQL query into the circular WAL flight recorder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string", "description": "Event type: HTTP_REQ, SQL_QUERY, LOG, etc."},
                        "payload": {"type": "object", "description": "JSON event payload"},
                        "source": {"type": "string", "description": "Source module name (default: 'agent')"}
                    },
                    "required": ["event_type", "payload"]
                },
                "handler": lambda args: self.record_event(
                    event_type=args["event_type"],
                    payload=args["payload"],
                    source=args.get("source", "agent")
                )
            },
            "generate_reproduction_test": {
                "name": "generate_reproduction_test",
                "description": "Synthesizes a standalone, executable Python reproduction test (pytest) from the latest recorded crash in WAL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        }
                    }
                },
                "handler": lambda args: self.generate_reproduction_test(
                    db_path=args.get("db_path", self.default_db_path)
                )
            },
            "verify_crash_resolution": {
                "name": "verify_crash_resolution",
                "description": "Executes the synthesized reproduction test to verify if the code fix resolves the crash without regression, emitting a resolution receipt upon success.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "test_path": {
                            "type": "string",
                            "description": "Path to reproduction test file (default: test_blackbox_reproduce.py)",
                            "default": "test_blackbox_reproduce.py"
                        },
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        },
                        "receipt_path": {
                            "type": "string",
                            "description": "Path to output resolution receipt markdown (default: .blackbox-resolved.md)",
                            "default": ".blackbox-resolved.md"
                        }
                    }
                },
                "handler": lambda args: self.verify_crash_resolution(
                    test_path=args.get("test_path", "test_blackbox_reproduce.py"),
                    db_path=args.get("db_path", self.default_db_path),
                    receipt_path=args.get("receipt_path", ".blackbox-resolved.md")
                )
            },
            "test_candidate_patch": {
                "name": "test_candidate_patch",
                "description": "Applies a candidate patch in unified git diff format temporarily, runs reproduction tests and AST impact tests, guarantees full rollback, and returns verification status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patch_diff": {
                            "type": "string",
                            "description": "Unified git diff of the candidate fix"
                        },
                        "test_path": {
                            "type": "string",
                            "description": "Path to reproduction test file (default: test_blackbox_reproduce.py)",
                            "default": "test_blackbox_reproduce.py"
                        }
                    },
                    "required": ["patch_diff"]
                },
                "handler": lambda args: self.test_candidate_patch(
                    patch_diff=args["patch_diff"],
                    test_path=args.get("test_path", "test_blackbox_reproduce.py")
                )
            }
        }
        # Register aliases for backwards compatibility
        self.tools["blackbox_get_crash_autopsy"] = self.tools["get_crash_autopsy"]
        self.tools["blackbox_blast_radius"] = self.tools["get_blast_radius"]
        self.tools["blackbox_query_timeline"] = self.tools["query_execution_timeline"]
        self.tools["blackbox_record_event"] = self.tools["record_event"]
        self.tools["blackbox_generate_reproduction_test"] = self.tools["generate_reproduction_test"]
        self.tools["blackbox_verify_crash_resolution"] = self.tools["verify_crash_resolution"]
        self.tools["blackbox_test_candidate_patch"] = self.tools["test_candidate_patch"]


    def generate_reproduction_test(self, db_path: str = ".blackbox/wal.sqlite") -> str:
        """Synthesizes and returns standalone Python reproduction test script from WAL database."""
        ring = self.wal_ring if db_path == self.default_db_path else WalRingBuffer(db_path=db_path)
        reproducer = CrashReproducer(wal_ring=ring)
        return reproducer.generate()

    def verify_crash_resolution(
        self,
        test_path: str = "test_blackbox_reproduce.py",
        db_path: str = ".blackbox/wal.sqlite",
        receipt_path: str = ".blackbox-resolved.md"
    ) -> Dict[str, Any]:
        """Runs the reproduction test and issues a resolution receipt if the crash is fixed."""
        ring = self.wal_ring if db_path == self.default_db_path else WalRingBuffer(db_path=db_path)
        verifier = CausalVerifier(wal_ring=ring, db_path=db_path, receipt_path=receipt_path)
        return verifier.verify(test_path=test_path)

    def test_candidate_patch(
        self,
        patch_diff: str,
        test_path: str = "test_blackbox_reproduce.py"
    ) -> Dict[str, Any]:
        """Validates a candidate patch diff in an isolated environment with guaranteed rollback."""
        validator = PatchValidator(repo_root=".", db_path=self.default_db_path)
        return validator.evaluate_patch(patch_str=patch_diff, test_path=test_path)


    def get_crash_autopsy(self, db_path: str = ".blackbox/wal.sqlite") -> str:

        """Compiles and returns .blackbox.md autopsy markdown from WAL database."""
        ring = self.wal_ring if db_path == self.default_db_path else WalRingBuffer(db_path=db_path)
        compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=self.ast_graph)
        return compiler.compile()

    def get_blast_radius(self, file_path: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Returns upstream and downstream blast radius for a symbol or file."""
        if not os.path.isfile(file_path):
            return {"error": f"File not found: {file_path}"}

        self.ast_graph.parse_file(file_path)
        if symbol:
            return self.ast_graph.get_blast_radius(symbol, file_path=file_path)

        nodes = [n for n, d in self.ast_graph.nodes.items() if d.get("filename") == file_path]
        return {
            "file": file_path,
            "symbols_count": len(nodes),
            "symbols": nodes,
            "dependencies": {n: list(self.ast_graph.nodes[n].get("calls", [])) for n in nodes}
        }

    def query_execution_timeline(
        self,
        limit: int = 50,
        event_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries recent flight recorder events with optional event_type filtering."""
        events = self.wal_ring.query_recent(limit=limit, event_type=event_type)
        return {
            "total_events": len(events),
            "filter": {"event_type": event_type, "limit": limit},
            "events": events
        }

    def record_event(
        self,
        event_type: str,
        payload: Any,
        source: str = "agent"
    ) -> Dict[str, Any]:
        """Records an arbitrary event directly into WAL ring-buffer."""
        eid = self.wal_ring.record(event_type=event_type, payload=payload, source=source)
        return {"status": "recorded", "event_id": eid}

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
            # List distinct primary tools
            distinct = [
                "get_crash_autopsy",
                "generate_reproduction_test",
                "verify_crash_resolution",
                "test_candidate_patch",
                "get_blast_radius",
                "query_execution_timeline",
                "record_event"
            ]

            tool_list = [
                {
                    "name": self.tools[k]["name"],
                    "description": self.tools[k]["description"],
                    "inputSchema": self.tools[k]["inputSchema"]
                }
                for k in distinct
            ]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_list}}
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name in self.tools:
                try:
                    res = self.tools[name]["handler"](args)
                    text_content = res if isinstance(res, str) else json.dumps(res, indent=2)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": text_content}]
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
