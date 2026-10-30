"""
Genesis Blackbox: Model Context Protocol (MCP) Server
Exposes runtime flight recorder and blast-radius diagnostic tools to AI coding agents over stdio.
"""

import os
import sys
import json
import logging
import argparse
from contextlib import contextmanager
from typing import Dict, Any, List, Optional
from . import __version__
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
        self.version = __version__
        self._init_tools()

    def close(self) -> None:
        """Releases the default WAL connection."""
        self.wal_ring.close()

    @contextmanager
    def _resolve_ring(self, db_path: Optional[str]):
        target = db_path or self.default_db_path
        if os.path.abspath(target) == os.path.abspath(self.default_db_path) or target == self.default_db_path:
            yield self.wal_ring
        else:
            temp_ring = WalRingBuffer(db_path=target)
            try:
                yield temp_ring
            finally:
                temp_ring.close()

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
                        "event_type": {"type": "string", "description": "Filter by event type (HTTP_REQ, SQL_QUERY, EXCEPTION, etc.)"},
                        "db_path": {"type": "string", "description": "Optional path to WAL SQLite database"}
                    }
                },
                "handler": lambda args: self.query_execution_timeline(
                    limit=args.get("limit", 50),
                    event_type=args.get("event_type"),
                    db_path=args.get("db_path")
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
                        "source": {"type": "string", "description": "Source module name (default: 'agent')"},
                        "db_path": {"type": "string", "description": "Optional path to WAL SQLite database"}
                    },
                    "required": ["event_type", "payload"]
                },
                "handler": lambda args: self.record_event(
                    event_type=args["event_type"],
                    payload=args["payload"],
                    source=args.get("source", "agent"),
                    db_path=args.get("db_path")
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
                            "description": "Path to reproduction test file inside the repo (default: auto-detect test_blackbox_reproduce.mjs/.py)"
                        }
                    },
                    "required": ["patch_diff"]
                },
                "handler": lambda args: self.test_candidate_patch(
                    patch_diff=args["patch_diff"],
                    test_path=args.get("test_path")
                )
            },
            "compare_execution_runs": {
                "name": "compare_execution_runs",
                "description": "Compares the last passing execution run against the latest failing run stored in WAL, pinpointing exact divergence in SQL queries, HTTP payloads, and variable states.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        },
                        "baseline_run_id": {
                            "type": "string",
                            "description": "Optional explicit baseline healthy run ID"
                        },
                        "failing_run_id": {
                            "type": "string",
                            "description": "Optional explicit failing run ID"
                        },
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "json"],
                            "description": "Output format: 'markdown' (default) or 'json'",
                            "default": "markdown"
                        }
                    }
                },
                "handler": lambda args: self.compare_execution_runs(
                    db_path=args.get("db_path", self.default_db_path),
                    baseline_run_id=args.get("baseline_run_id"),
                    failing_run_id=args.get("failing_run_id"),
                    output_format=args.get("format", "markdown")
                )
            },
            "get_repair_prompt": {
                "name": "get_repair_prompt",
                "description": "Synthesizes an all-in-one AI agent repair prompt packet combining crash autopsy, polyglot reproduction tests, AST blast-radius, and execution diffs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        },
                        "repo_root": {
                            "type": "string",
                            "description": "Project root directory (default: .)",
                            "default": "."
                        }
                    }
                },
                "handler": lambda args: self.get_repair_prompt(
                    db_path=args.get("db_path", self.default_db_path),
                    repo_root=args.get("repo_root", ".")
                )
            },
            "create_crash_bundle": {
                "name": "create_crash_bundle",
                "description": "Snapshots active WAL database and packages an incident archive (.zip) with autopsy, diff, reproducer test, and SHA-256 system manifest.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "output_zip": {
                            "type": "string",
                            "description": "Path to output zip archive (default: crash_bundle.zip)",
                            "default": "crash_bundle.zip"
                        },
                        "db_path": {
                            "type": "string",
                            "description": "Path to WAL SQLite database (default: .blackbox/wal.sqlite)",
                            "default": ".blackbox/wal.sqlite"
                        }
                    }
                },
                "handler": lambda args: self.create_crash_bundle(
                    output_zip=args.get("output_zip", "crash_bundle.zip"),
                    db_path=args.get("db_path", self.default_db_path)
                )
            },
            "inspect_crash_bundle": {
                "name": "inspect_crash_bundle",
                "description": "Inspects a shareable crash incident archive (.zip) and verifies SHA-256 member checksum integrity without extraction.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "zip_path": {
                            "type": "string",
                            "description": "Path to crash bundle .zip file"
                        }
                    },
                    "required": ["zip_path"]
                },
                "handler": lambda args: self.inspect_crash_bundle(
                    zip_path=args["zip_path"]
                )
            }
        }
        self.tools["blackbox_get_crash_autopsy"] = self.tools["get_crash_autopsy"]
        self.tools["blackbox_blast_radius"] = self.tools["get_blast_radius"]
        self.tools["blackbox_query_timeline"] = self.tools["query_execution_timeline"]
        self.tools["blackbox_record_event"] = self.tools["record_event"]
        self.tools["blackbox_generate_reproduction_test"] = self.tools["generate_reproduction_test"]
        self.tools["blackbox_verify_crash_resolution"] = self.tools["verify_crash_resolution"]
        self.tools["blackbox_test_candidate_patch"] = self.tools["test_candidate_patch"]
        self.tools["blackbox_compare_execution_runs"] = self.tools["compare_execution_runs"]
        self.tools["blackbox_get_repair_prompt"] = self.tools["get_repair_prompt"]
        self.tools["blackbox_create_crash_bundle"] = self.tools["create_crash_bundle"]
        self.tools["blackbox_inspect_crash_bundle"] = self.tools["inspect_crash_bundle"]

    def generate_reproduction_test(self, db_path: str = ".blackbox/wal.sqlite") -> str:
        """Synthesizes and returns standalone Python reproduction test script from WAL database."""
        with self._resolve_ring(db_path) as ring:
            reproducer = CrashReproducer(wal_ring=ring)
            return reproducer.generate()

    def verify_crash_resolution(
        self,
        test_path: str = "test_blackbox_reproduce.py",
        db_path: str = ".blackbox/wal.sqlite",
        receipt_path: str = ".blackbox-resolved.md"
    ) -> Dict[str, Any]:
        """Runs the reproduction test and issues a resolution receipt if the crash is fixed."""
        with self._resolve_ring(db_path) as ring:
            verifier = CausalVerifier(wal_ring=ring, db_path=db_path, receipt_path=receipt_path)
            target_test = test_path
            if target_test == "test_blackbox_reproduce.py" and not os.path.isfile(target_test):
                if os.path.isfile("test_blackbox_reproduce.mjs"):
                    target_test = "test_blackbox_reproduce.mjs"
            return verifier.verify(test_path=target_test)

    def test_candidate_patch(
        self,
        patch_diff: str,
        test_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Validates a candidate patch diff in an isolated environment with guaranteed rollback."""
        validator = PatchValidator(repo_root=".", db_path=self.default_db_path)
        return validator.evaluate_patch(patch_str=patch_diff, test_path=test_path)

    def compare_execution_runs(
        self,
        db_path: str = ".blackbox/wal.sqlite",
        baseline_run_id: Optional[str] = None,
        failing_run_id: Optional[str] = None,
        output_format: str = "markdown"
    ) -> Any:
        """Compares baseline healthy execution run against latest failing run."""
        from .compiler.state_differ import StateDiffer
        with self._resolve_ring(db_path) as ring:
            differ = StateDiffer(wal_ring=ring)
            diff_res = differ.diff(baseline_run_id=baseline_run_id, failing_run_id=failing_run_id)
            if output_format == "json":
                return diff_res
            return differ.compile_markdown(diff_res)

    def get_repair_prompt(
        self,
        db_path: str = ".blackbox/wal.sqlite",
        repo_root: str = "."
    ) -> str:
        """Synthesizes and returns complete AI repair prompt packet from WAL database."""
        from .compiler.prompt_synth import PromptSynthesizer
        with self._resolve_ring(db_path) as ring:
            synth = PromptSynthesizer(wal_ring=ring, repo_root=repo_root, db_path=db_path)
            return synth.synthesize()

    def create_crash_bundle(
        self,
        output_zip: str = "crash_bundle.zip",
        db_path: str = ".blackbox/wal.sqlite"
    ) -> Dict[str, Any]:
        """Creates a portable incident zip bundle."""
        from .recorder.bundle import CrashBundler
        bundler = CrashBundler(db_path=db_path, repo_root=".")
        return bundler.create_bundle(output_zip=output_zip, db_path=db_path)

    def inspect_crash_bundle(
        self,
        zip_path: str
    ) -> Dict[str, Any]:
        """Inspects and verifies the integrity of an incident zip bundle."""
        from .recorder.bundle import CrashBundler
        bundler = CrashBundler()
        return bundler.inspect_bundle(zip_path=zip_path)

    def get_crash_autopsy(self, db_path: str = ".blackbox/wal.sqlite") -> str:
        """Compiles and returns .blackbox.md autopsy markdown from WAL database."""
        with self._resolve_ring(db_path) as ring:
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
        event_type: Optional[str] = None,
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Queries recent flight recorder events with optional event_type filtering."""
        with self._resolve_ring(db_path) as ring:
            events = ring.query_recent(limit=limit, event_type=event_type)
        return {
            "total_events": len(events),
            "filter": {"event_type": event_type, "limit": limit},
            "events": events
        }

    def record_event(
        self,
        event_type: str,
        payload: Any,
        source: str = "agent",
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Records an arbitrary event directly into WAL ring-buffer."""
        with self._resolve_ring(db_path) as ring:
            eid = ring.record(event_type=event_type, payload=payload, source=source)
        return {"status": "recorded", "event_id": eid}

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(req, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}
            }

        method = req.get("method", "")
        if "id" not in req or str(method).startswith("notifications/"):
            return None

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
        elif method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "tools/list":
            distinct = [
                "get_crash_autopsy",
                "generate_reproduction_test",
                "verify_crash_resolution",
                "test_candidate_patch",
                "get_blast_radius",
                "query_execution_timeline",
                "record_event",
                "compare_execution_runs",
                "get_repair_prompt",
                "create_crash_bundle",
                "inspect_crash_bundle"
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
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name in self.tools:
                try:
                    res = self.tools[name]["handler"](args)
                    text_content = res if isinstance(res, str) else json.dumps(res, indent=2, default=str)
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
            except json.JSONDecodeError as e:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"}
                }
                print(json.dumps(err_resp), flush=True)
                continue

            try:
                resp = self.handle_request(req)
                if resp is not None:
                    print(json.dumps(resp), flush=True)
            except Exception as e:
                logger.error(f"Error handling request: {e}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackbox-mcp",
        description="Genesis Blackbox Model Context Protocol (MCP) stdio server"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"genesis-blackbox {__version__}"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("BLACKBOX_DB_PATH", ".blackbox/wal.sqlite"),
        help="Path to SQLite WAL ring-buffer database (default: .blackbox/wal.sqlite)"
    )
    args = parser.parse_args(argv)
    server = BlackboxMCPServer(db_path=args.db)
    try:
        server.run_stdio()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
