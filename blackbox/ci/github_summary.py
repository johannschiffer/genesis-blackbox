"""
Genesis Blackbox: GitHub Actions Step Summary Compiler
Formats runtime flight recorder autopsies into rich, collapsible GitHub Step Summaries.
"""

import os
import sys
import re
from typing import Optional, Dict, List


def export_github_step_summary(
    autopsy_path: str = ".blackbox.md",
    summary_file: Optional[str] = None
) -> str:
    """
    Transforms a .blackbox.md autopsy capsule into a structured GitHub Actions Step Summary.
    Appends the formatted Markdown to $GITHUB_STEP_SUMMARY or the specified summary_file.

    Parameters:
        autopsy_path: Path to the .blackbox.md autopsy capsule.
        summary_file: Target file path to write or append (overrides GITHUB_STEP_SUMMARY env var).

    Returns:
        The generated GitHub Markdown summary string.
    """
    content = ""
    if os.path.exists(autopsy_path):
        try:
            with open(autopsy_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            content = ""

    if not content:
        summary_md = (
            "# 🛩️ Genesis Blackbox: Execution Status\n\n"
            "> [!NOTE]\n"
            f"> No failure autopsy capsule was located at `{autopsy_path}`. "
            "All test suites executed cleanly or flight recorder was not engaged.\n"
        )
    else:
        summary_md = _format_autopsy_for_github(content, autopsy_path)

    # Determine output file: explicit argument wins, followed by GITHUB_STEP_SUMMARY environment variable
    target_path = summary_file or os.environ.get("GITHUB_STEP_SUMMARY")
    if target_path:
        try:
            target_dir = os.path.dirname(os.path.abspath(target_path))
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            with open(target_path, "a", encoding="utf-8") as f_out:
                f_out.write(summary_md + "\n\n")
        except Exception as e:
            print(f"[Genesis-Blackbox] Warning: Could not write step summary to {target_path}: {e}", file=sys.stderr)

    return summary_md


def _format_autopsy_for_github(raw_autopsy: str, autopsy_path: str) -> str:
    """Parses .blackbox.md markdown sections into collapsible GitHub summary blocks."""
    lines = raw_autopsy.splitlines()

    title = "Causal Crash Autopsy"
    for line in lines:
        if line.startswith("# BLACKBOX CAUSAL AUTOPSY:"):
            title = line.replace("# BLACKBOX CAUSAL AUTOPSY:", "").strip()
            break
        elif line.startswith("# "):
            title = line.replace("# ", "").strip()
            break

    # Extract sections by heading
    sections: Dict[str, List[str]] = {}
    current_section = "header"
    sections[current_section] = []

    for line in lines:
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections[current_section] = []
        else:
            sections[current_section].append(line)

    summary_blocks = []
    summary_blocks.append(f"# 🛩️ Genesis Blackbox: {title}\n")
    summary_blocks.append("> [!CAUTION]\n> **Automated Failure Capture Active**: Deep stack frames, local variables, and preceding events recorded.\n")

    # 1. Crash Overview
    overview_lines = []
    for sec_name, sec_lines in sections.items():
        if "Crash Overview" in sec_name:
            overview_lines = [l for l in sec_lines if l.strip()]
            break

    if overview_lines:
        summary_blocks.append("### 📋 Executive Incident Summary\n")
        summary_blocks.append("\n".join(overview_lines) + "\n")

    # 2. Temporal Causal Context (Pre-Crash Events)
    events_lines = []
    for sec_name, sec_lines in sections.items():
        if "Temporal Causal Context" in sec_name:
            events_lines = sec_lines
            break

    if events_lines:
        summary_blocks.append("<details open>")
        summary_blocks.append("<summary><b>⏱️ Temporal Causal Context (Pre-Crash Flight Events)</b></summary>\n")
        summary_blocks.append("\n".join(events_lines).strip())
        summary_blocks.append("\n</details>\n")

    # 3. Inspected Call Stack & Locals
    stack_lines = []
    for sec_name, sec_lines in sections.items():
        if "Inspected Call Stack" in sec_name:
            stack_lines = sec_lines
            break

    if stack_lines:
        summary_blocks.append("<details>")
        summary_blocks.append("<summary><b>🔍 Inspected Call Stack & Local Variable Scopes</b></summary>\n")
        summary_blocks.append("\n".join(stack_lines).strip())
        summary_blocks.append("\n</details>\n")

    # 4. AST Blast Radius
    blast_lines = []
    for sec_name, sec_lines in sections.items():
        if "AST Blast Radius" in sec_name:
            blast_lines = sec_lines
            break

    if blast_lines:
        summary_blocks.append("<details open>")
        summary_blocks.append("<summary><b>💥 AST Blast Radius & Dependency Impact</b></summary>\n")
        summary_blocks.append("\n".join(blast_lines).strip())
        summary_blocks.append("\n</details>\n")

    # 5. Suggested Fix Mandate
    fix_lines = []
    for sec_name, sec_lines in sections.items():
        if "Suggested Fix Mandate" in sec_name:
            fix_lines = sec_lines
            break

    if fix_lines:
        summary_blocks.append("<details open>")
        summary_blocks.append("<summary><b>🛠️ Suggested Autonomous Fix Mandate</b></summary>\n")
        summary_blocks.append("\n".join(fix_lines).strip())
        summary_blocks.append("\n</details>\n")

    # Source capsule reference
    summary_blocks.append(f"\n*Source capsule: `{autopsy_path}` | Engine: Genesis Blackbox Flight Recorder*")

    return "\n".join(summary_blocks)
