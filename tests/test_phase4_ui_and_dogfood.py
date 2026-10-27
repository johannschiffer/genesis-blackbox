"""
Unit and integration tests for Genesis Blackbox Phase 4:
UI Server Endpoints, Realistic Dogfood Crash Simulation, and CLI Entrypoint Resolution.
"""

import os
import sys
import json
import socket
import threading
import urllib.request
import tempfile
import importlib.util
import pytest

from blackbox.ui.server import create_ui_server
from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.cli import main as cli_main

# Dynamically import run_dogfood_simulation from examples
dogfood_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples", "dogfood_crash.py"))
spec = importlib.util.spec_from_file_location("dogfood_crash", dogfood_path)
dogfood_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dogfood_module)
run_dogfood_simulation = dogfood_module.run_dogfood_simulation


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_ui_server():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ui_wal.sqlite")
        ring = WalRingBuffer(db_path=db_path)
        ring.record(event_type="HTTP_REQ", payload={"method": "GET", "path": "/health"}, source="test_suite")
        ring.record(event_type="SQL_QUERY", payload={"query": "SELECT 1"}, source="test_suite")

        port = get_free_port()
        server = create_ui_server(host="127.0.0.1", port=port, db_path=db_path)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield f"http://127.0.0.1:{port}", db_path

        server.shutdown()
        server.server_close()


def test_ui_server_http_endpoints(running_ui_server):
    base_url, db_path = running_ui_server

    # 1. Test HTML frontend root
    with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        assert "Genesis Blackbox" in html
        assert "Flight Recorder" in html

    # 2. Test /api/timeline
    with urllib.request.urlopen(f"{base_url}/api/timeline", timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "total_events" in data
        assert data["total_events"] >= 2
        assert len(data["events"]) >= 2

    # 3. Test /api/autopsy
    with urllib.request.urlopen(f"{base_url}/api/autopsy", timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "autopsy_markdown" in data
        assert "has_crash" in data

    # 4. Test /api/status
    with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["status"] == "online"
        assert data["db_path"] == db_path


def test_dogfood_crash_simulation():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "dogfood.sqlite")
        out_file = os.path.join(tmpdir, "dogfood_autopsy.md")

        capsule = run_dogfood_simulation(db_path=db_path, output_file=out_file)

        assert os.path.isfile(out_file)
        assert len(capsule) > 500
        assert "InsufficientFundsError" in capsule
        assert "deduct_credits" in capsule
        assert "/api/v1/orders/checkout" in capsule
        assert "user_accounts" in capsule


def test_cli_help_and_subcommands(capsys):
    # 1. Test blackbox --help
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "Genesis Blackbox" in captured.out
    assert "record" in captured.out
    assert "autopsy" in captured.out
    assert "blast-radius" in captured.out
    assert "ui" in captured.out

    # 2. Test subcommands help
    for sub in ["record", "autopsy", "blast-radius", "ui", "timeline"]:
        with pytest.raises(SystemExit) as exc:
            cli_main([sub, "--help"])
        assert exc.value.code == 0
