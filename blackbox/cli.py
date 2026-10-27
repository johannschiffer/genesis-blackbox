"""
Genesis Blackbox CLI Interface
Usage:
  blackbox record -- <command>
  blackbox autopsy [--db <wal_path>] [--output .blackbox.md]
  blackbox blast-radius <file_path> [--symbol <func_or_class>]
  blackbox timeline [--limit 20] [--db <wal_path>]
"""

import os
import sys
import json
import argparse
from typing import List, Optional
from .recorder.wal_ring import WalRingBuffer
from .recorder.runner import run_target
from .ast_engine.graph import CodebaseGraph
from .compiler.autopsy import CausalCrashCompiler


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="blackbox",
        description="Genesis Blackbox: Flight Recorder & AST Blast-Radius Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: record
    rec_parser = subparsers.add_parser("record", help="Execute command or record event into flight recorder")
    rec_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL ring-buffer database")
    rec_parser.add_argument("--type", help="Manual event type (HTTP_REQ, SQL_QUERY, LOG, etc.)")
    rec_parser.add_argument("--payload", help="JSON payload string for manual recording")
    rec_parser.add_argument("--source", default="cli", help="Source identifier")
    rec_parser.add_argument("target_command", nargs=argparse.REMAINDER, help="Target command to execute with flight recorder")

    # Subcommand: autopsy
    auto_parser = subparsers.add_parser("autopsy", help="Compile crash autopsy capsule (.blackbox.md)")
    auto_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL database")
    auto_parser.add_argument("--output", default=".blackbox.md", help="Output file path (default: .blackbox.md)")

    # Subcommand: blast-radius
    blast_parser = subparsers.add_parser("blast-radius", help="Calculate blast radius of a symbol or file")
    blast_parser.add_argument("file_path", nargs="?", default="", help="Source python file to analyze")
    blast_parser.add_argument("--file", help="Source python file (alternative to positional)")
    blast_parser.add_argument("--symbol", help="Target function or class name")
    blast_parser.add_argument("--function", help="Target function name (alias for --symbol)")

    # Subcommand: timeline
    time_parser = subparsers.add_parser("timeline", help="Display recent flight recorder timeline")
    time_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL database")
    time_parser.add_argument("--limit", type=int, default=20, help="Maximum events to display")

    # Subcommand: ui
    ui_parser = subparsers.add_parser("ui", help="Launch local timeline & crash autopsy web visualizer")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    ui_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL database (default: .blackbox/wal.sqlite)")
    ui_parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")

    args = parser.parse_args(argv)

    if args.command == "record":
        cmd = args.target_command
        # Filter out leading '--' if passed
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]

        # Case 1: Target command execution
        if cmd:
            return run_target(cmd, db_path=args.db)

        # Case 2: Manual event payload recording
        if args.type and args.payload:
            ring = WalRingBuffer(db_path=args.db)
            try:
                payload = json.loads(args.payload)
            except Exception:
                payload = {"raw": args.payload}
            eid = ring.record(event_type=args.type, payload=payload, source=args.source)
            print(f"Recorded event #{eid} into circular WAL ring-buffer ({args.db}).")
            return 0

        print("[Genesis-Blackbox] Error: Specify either a target command or --type and --payload.", file=sys.stderr)
        return 1

    elif args.command == "autopsy":
        ring = WalRingBuffer(db_path=args.db)
        graph = CodebaseGraph()
        compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
        capsule = compiler.compile()

        if args.output and args.output != "-":
            os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(capsule)
            print(f"[Genesis-Blackbox] Autopsy capsule successfully written to {args.output}")
        else:
            print(capsule)
        return 0

    elif args.command == "blast-radius":
        target_file = args.file_path or args.file
        if not target_file:
            print("[Genesis-Blackbox] Error: Must specify target file path.", file=sys.stderr)
            return 1

        target_symbol = args.symbol or args.function
        graph = CodebaseGraph()
        if not graph.parse_file(target_file):
            print(f"[Genesis-Blackbox] Error: Could not read or parse {target_file}", file=sys.stderr)
            return 1

        if target_symbol:
            radius = graph.get_blast_radius(target_symbol, file_path=target_file)
        else:
            # Report all functions in the file
            nodes = [n for n, d in graph.nodes.items() if d.get("filename") == target_file]
            radius = {
                "file": target_file,
                "symbols_count": len(nodes),
                "symbols": nodes,
                "call_dependencies": {n: list(graph.nodes[n].get("calls", [])) for n in nodes}
            }

        print(json.dumps(radius, indent=2))
        return 0

    elif args.command == "timeline":
        ring = WalRingBuffer(db_path=args.db)
        events = ring.query_recent(limit=args.limit)
        print(f"--- Flight Recorder Timeline ({len(events)} events) [{args.db}] ---")
        for e in reversed(events):
            dur = f" ({e['duration_ms']:.2f}ms)" if e.get("duration_ms") is not None else ""
            print(f"#{e['id']} [{e['event_type']}] from {e['source']}{dur}: {e['payload']}")
        return 0

    elif args.command == "ui":
        from .ui.server import start_ui_server
        start_ui_server(
            host=args.host,
            port=args.port,
            db_path=args.db,
            open_browser=not args.no_browser,
            run_forever=True
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
