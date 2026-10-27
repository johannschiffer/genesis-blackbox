"""
Genesis Blackbox CLI Interface
Usage:
  blackbox record --type SQL_QUERY --payload '{"query": "SELECT * FROM users"}'
  blackbox timeline --limit 20
  blackbox blast-radius --function handle_request --file main.py
"""

import sys
import json
import argparse
from .recorder.wal_ring import WalRingBuffer
from .ast_engine.graph import CodebaseGraph


def main():
    parser = argparse.ArgumentParser(
        description="Genesis Blackbox: Flight Recorder & AST Blast-Radius Engine",
        epilog="100% Free & Open Source (MIT). Support development on Ko-fi: https://ko-fi.com/johannschiffer"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: record
    rec_parser = subparsers.add_parser("record", help="Record an event into the flight recorder")
    rec_parser.add_argument("--type", required=True, help="Event type (HTTP_REQ, SQL_QUERY, LOG, etc.)")
    rec_parser.add_argument("--payload", required=True, help="JSON payload string")
    rec_parser.add_argument("--source", default="cli", help="Source identifier")

    # Subcommand: timeline
    time_parser = subparsers.add_parser("timeline", help="Display recent flight recorder timeline")
    time_parser.add_argument("--limit", type=int, default=20, help="Maximum events to display")

    # Subcommand: blast-radius
    blast_parser = subparsers.add_parser("blast-radius", help="Calculate blast radius of a function")
    blast_parser.add_argument("--function", required=True, help="Target function name")
    blast_parser.add_argument("--file", required=True, help="Source python file to analyze")

    args = parser.parse_args()
    ring = WalRingBuffer(db_path="/tmp/genesis_blackbox.db")

    if args.command == "record":
        try:
            payload = json.loads(args.payload)
        except Exception:
            payload = {"raw": args.payload}
        eid = ring.record(event_type=args.type, payload=payload, source=args.source)
        print(f"Recorded event #{eid} into circular WAL ring-buffer.")

    elif args.command == "timeline":
        events = ring.query_recent(limit=args.limit)
        print(f"--- Flight Recorder Timeline ({len(events)} events) ---")
        for e in reversed(events):
            print(f"#{e['id']} [{e['event_type']}] from {e['source']}: {e['payload']}")

    elif args.command == "blast-radius":
        graph = CodebaseGraph()
        with open(args.file, "r") as f:
            code = f.read()
        graph.parse_source(code, filename=args.file)
        radius = graph.get_blast_radius(args.function)
        print(json.dumps(radius, indent=2))


if __name__ == "__main__":
    main()
