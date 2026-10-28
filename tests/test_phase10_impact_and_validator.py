"""
Genesis Blackbox: Phase 10 Unit Tests
Validates the AST Impact Test Selector and MCP Candidate Patch Validator.
"""

import os
import sys
import ast
import subprocess
import pytest
from pathlib import Path

from blackbox.ast_engine.impact import ImpactTestSelector
from blackbox.compiler.patch_validator import PatchValidator
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.cli import main as cli_main


def _setup_git_repo(repo_path: Path):
    """Initializes a clean git repository for testing patches."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)


def test_ast_impact_mapping(tmp_path: Path):
    """Verifies that modified source files correctly identify impacted test suites via AST."""
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    math_file = core_dir / "math_utils.py"
    math_file.write_text(
        "def compute_discount(price: float, rate: float) -> float:\n"
        "    return price * (1.0 - rate)\n"
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    # Test file that directly calls the function
    test_math = tests_dir / "test_math_utils.py"
    test_math.write_text(
        "from core.math_utils import compute_discount\n\n"
        "def test_discount():\n"
        "    assert compute_discount(100.0, 0.1) == 90.0\n"
    )

    # Test file that does NOT call or import math_utils
    test_other = tests_dir / "test_other.py"
    test_other.write_text(
        "def test_unrelated():\n"
        "    assert 1 + 1 == 2\n"
    )

    selector = ImpactTestSelector(root_dir=str(tmp_path))
    impacted = selector.find_impacted_tests(files=[str(math_file)], diff=False)

    impacted_names = [os.path.basename(t) for t in impacted]
    assert "test_math_utils.py" in impacted_names
    assert "test_other.py" not in impacted_names


def test_patch_validator_valid_patch(tmp_path: Path):
    """Verifies that evaluate_patch validates a working patch and guarantees clean rollback."""
    _setup_git_repo(tmp_path)

    calc_file = tmp_path / "calc.py"
    calc_file.write_text(
        "def safe_divide(a, b):\n"
        "    return a / b\n"
    )

    test_file = tmp_path / "test_calc.py"
    test_file.write_text(
        "import calc\n"
        "def test_safe_divide():\n"
        "    assert calc.safe_divide(10, 2) == 5\n"
        "    assert calc.safe_divide(10, 0) == 0\n"
    )

    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True)

    # Candidate patch fixing zero division
    valid_patch = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def safe_divide(a, b):\n"
        "+    if b == 0:\n"
        "+        return 0\n"
        "     return a / b\n"
    )

    validator = PatchValidator(repo_root=str(tmp_path))
    assert validator.validate_patch_dry_run(valid_patch) is True

    result = validator.evaluate_patch(valid_patch, test_path="test_calc.py")
    assert result["success"] is True
    assert result["syntax_valid"] is True
    assert result["reproducer_passed"] is True
    assert result["regressions_count"] == 0

    # Ensure rollback occurred and working tree is unmodified
    current_content = calc_file.read_text(encoding="utf-8")
    assert "if b == 0:" not in current_content


def test_patch_validator_invalid_syntax_patch(tmp_path: Path):
    """Verifies that evaluate_patch rejects patches with syntax errors and rolls back safely."""
    _setup_git_repo(tmp_path)

    app_file = tmp_path / "app.py"
    app_file.write_text(
        "def process():\n"
        "    return 42\n"
    )
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    # Syntax error patch
    broken_patch = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def process():\n"
        "+    return ???syntax_error!!!\n"
        "     return 42\n"
    )

    validator = PatchValidator(repo_root=str(tmp_path))
    result = validator.evaluate_patch(broken_patch)

    assert result["success"] is False
    assert result["syntax_valid"] is False
    assert "SyntaxError" in result["output"]

    # Verify workspace rollback
    current_content = app_file.read_text(encoding="utf-8")
    assert "???syntax_error" not in current_content


def test_patch_validator_dry_run_rejection(tmp_path: Path):
    """Verifies dry-run rejection for corrupted or unapplicable diff strings."""
    _setup_git_repo(tmp_path)
    validator = PatchValidator(repo_root=str(tmp_path))

    garbage_patch = "Random text that is not a valid git diff format."
    assert validator.validate_patch_dry_run(garbage_patch) is False

    result = validator.evaluate_patch(garbage_patch)
    assert result["success"] is False
    assert "failed 'git apply --check'" in result["output"]


def test_cli_test_impact_subcommand(tmp_path: Path, capsys):
    """Verifies CLI execution of 'blackbox test-impact'."""
    pkg_file = tmp_path / "engine.py"
    pkg_file.write_text("def start_engine(): pass\n")
    test_file = tmp_path / "test_engine.py"
    test_file.write_text("import engine\ndef test_eng(): engine.start_engine()\n")

    exit_code = cli_main(["test-impact", "--files", str(pkg_file), "--root", str(tmp_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Impacted tests" in captured.out
    assert "test_engine.py" in captured.out


def test_mcp_test_candidate_patch_tool(tmp_path: Path):
    """Verifies the MCP tool 'test_candidate_patch' interface."""
    _setup_git_repo(tmp_path)
    src_file = tmp_path / "service.py"
    src_file.write_text("def ping(): return 'pong'\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    server = BlackboxMCPServer(db_path=str(tmp_path / "wal.sqlite"))

    # Test invoking the tool via handle_request
    request = {
        "jsonrpc": "2.0",
        "id": "req-phase10-1",
        "method": "tools/call",
        "params": {
            "name": "test_candidate_patch",
            "arguments": {
                "patch_diff": "invalid patch diff that fails check",
                "test_path": "non_existent_test.py"
            }
        }
    }
    response = server.handle_request(request)
    assert response["id"] == "req-phase10-1"
    content = response["result"]["content"][0]["text"]
    assert "failed 'git apply --check'" in content
