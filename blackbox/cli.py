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
from .compiler.reproducer import CrashReproducer
from .compiler.verifier import CausalVerifier




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

    # Subcommand: reproduce
    rep_parser = subparsers.add_parser("reproduce", help="Synthesize standalone crash reproduction test script")
    rep_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL database")
    rep_parser.add_argument("--output", default="test_reproduce.py", help="Output test file path (default: test_reproduce.py)")

    # Subcommand: init-rules
    rules_parser = subparsers.add_parser("init-rules", help="Initialize agent instructions template (Cursor, Claude, Windsurf)")
    rules_parser.add_argument("--type", choices=["cursor", "claude", "windsurf"], default="cursor", help="Target agent rule type")
    rules_parser.add_argument("--output", help="Custom destination path")

    # Subcommand: verify
    ver_parser = subparsers.add_parser("verify", help="Execute reproduction test and generate resolution verification receipt")
    ver_parser.add_argument("--test", default="test_blackbox_reproduce.py", help="Path to reproduction test file (default: test_blackbox_reproduce.py)")
    ver_parser.add_argument("--receipt", default=".blackbox-resolved.md", help="Path to resolution receipt markdown file (default: .blackbox-resolved.md)")
    ver_parser.add_argument("--db", default=".blackbox/wal.sqlite", help="Path to WAL database")

    # Subcommand: ci-summary
    ci_parser = subparsers.add_parser("ci-summary", help="Export failure autopsy as GitHub Actions Step Summary")
    ci_parser.add_argument("--autopsy", default=".blackbox.md", help="Path to autopsy capsule file (default: .blackbox.md)")
    ci_parser.add_argument("--output", help="Output summary file path (default: $GITHUB_STEP_SUMMARY or stdout)")

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

    elif args.command == "reproduce":
        ring = WalRingBuffer(db_path=args.db)
        reproducer = CrashReproducer(wal_ring=ring)
        code = reproducer.generate()

        if args.output and args.output != "-":
            out_path = os.path.abspath(args.output)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(code)
            print(f"[Genesis-Blackbox] Reproduction test written to {args.output}")
        else:
            print(code)
        return 0

    elif args.command == "init-rules":
        template_dir = os.path.join(os.path.dirname(__file__), "templates", "rules")
        target_map = {
            "cursor": ("cursorrules.template", ".cursorrules"),
            "claude": ("claude_md.template", "CLAUDE.md"),
            "windsurf": ("windsurf.template", ".windsurfrules"),
        }
        template_name, default_dest = target_map.get(args.type, ("cursorrules.template", ".cursorrules"))
        src_path = os.path.join(template_dir, template_name)
        dest_path = args.output or default_dest

        if not os.path.isfile(src_path):
            print(f"[Genesis-Blackbox] Error: Template not found at {src_path}", file=sys.stderr)
            return 1

        with open(src_path, "r", encoding="utf-8") as f_in:
            content = f_in.read()

        out_path = os.path.abspath(dest_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f_out:
            f_out.write(content)

        print(f"[Genesis-Blackbox] Agent rules ({args.type}) initialized at {dest_path}")
        return 0

    elif args.command == "verify":
        ring = WalRingBuffer(db_path=args.db)
        verifier = CausalVerifier(wal_ring=ring, db_path=args.db, receipt_path=args.receipt)
        result = verifier.verify(test_path=args.test)

        if result.get("resolved"):
            dur = result.get("duration_ms", 0)
            print(f"[Genesis-Blackbox] Verification PASSED ({dur:.2f}ms).")
            print(f"[Genesis-Blackbox] Resolution receipt written to {result.get('receipt_path')}")
            return 0
        else:
            print(f"[Genesis-Blackbox] Verification FAILED (exit code {result.get('exit_code')}).", file=sys.stderr)
            if result.get("output"):
                print(result.get("output"), file=sys.stderr)
            return 1

    elif args.command == "ci-summary":
        from .ci.github_summary import export_github_step_summary
        summary_md = export_github_step_summary(autopsy_path=args.autopsy, summary_file=args.output)
        if not args.output and not os.environ.get("GITHUB_STEP_SUMMARY"):
            print(summary_md)
        return 0

    return 0




if __name__ == "__main__":
    sys.exit(main())
