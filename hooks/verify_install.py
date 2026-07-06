#!/usr/bin/env python3
"""Verify the Light RIP install — runtime-agnostic.

This script does not enumerate agent runtimes. It performs two
runtime-independent checks that are valid for every install path:

  1. `reminder.md` is readable at the expected skill-root location.
  2. `light_rip_reminder.py` can start, read the reminder, and emit
     stdout containing the literal marker "Evidence Before Claims"
     (a substring of `reminder.md`). This proves the script-side
     wiring is sound; it does NOT prove any runtime will actually
     invoke the hook on real prompts.

Run this after any installer (Codex / Claude Code dedicated
installers, or after an agent follows the general installer's
prompt). It takes no flags — there is no agent name to specify.

Exit codes:
  0 — both checks passed
  1 — one check failed
  2 — setup is wrong (e.g. reminder.md missing entirely)

Output:
  - default: human-readable multi-line summary
  - --json : single JSON object on stdout
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REMINDER_MARKER = "Evidence Before Claims"


# ---------- path resolution ----------

def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def reminder_md_path() -> Path:
    return skill_root() / "reminder.md"


def reminder_script_path() -> Path:
    return skill_root() / "hooks" / "light_rip_reminder.py"


# ---------- check primitives ----------

def check(label: str, ok: bool, detail: str = "") -> dict:
    return {"name": label, "ok": bool(ok), "detail": detail}


def aggregate(checks: list[dict]) -> bool:
    return all(c["ok"] for c in checks)


def check_reminder_md_readable() -> dict:
    path = reminder_md_path()
    if not path.is_file():
        return check("reminder.md exists", False, f"missing at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return check("reminder.md exists", False, f"unreadable: {exc}")
    if REMINDER_MARKER not in text:
        return check(
            "reminder.md readable",
            False,
            f"{path} readable but missing marker {REMINDER_MARKER!r}",
        )
    return check("reminder.md readable", True,
                 f"{path} ({len(text)} bytes)")


def check_reminder_script_spawn() -> dict:
    """Spawn light_rip_reminder.py with a simulated stdin and verify
    that the script exits 0 and that stdout contains the reminder
    marker. This is independent of any runtime's envelope shape."""
    script = reminder_script_path()
    if not script.is_file():
        return check("reminder script exists", False, f"missing at {script}")

    payload = '{"input":{"prompt":"hi"},"output":{}}'
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--format", "harness"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return check("reminder script runnable", False,
                     f"`python {script}` timed out after 10s")
    except OSError as exc:
        return check("reminder script runnable", False,
                     f"spawn failed: {exc}")

    if completed.returncode != 0:
        return check(
            "reminder script runnable",
            False,
            f"exit code {completed.returncode}; stderr: {completed.stderr.strip()[:200]}",
        )
    stdout = completed.stdout
    if not stdout.strip():
        return check("reminder script runnable", False,
                     "stdout was empty")
    if REMINDER_MARKER not in stdout:
        return check(
            "reminder script runnable",
            False,
            f"stdout did not contain marker {REMINDER_MARKER!r}; "
            f"first 200 chars: {stdout[:200]!r}",
        )
    return check(
        "reminder script runnable",
        True,
        f"`{script.name}` exited 0, stdout {len(stdout)} bytes, marker present",
    )


def run_checks() -> list[dict]:
    return [
        check_reminder_md_readable(),
        check_reminder_script_spawn(),
    ]


# ---------- output ----------

def render_human(checks: list[dict]) -> str:
    ok = aggregate(checks)
    header = f"Light RIP install verification — {'PASS' if ok else 'FAIL'}"
    lines = [header, ""]
    for c in checks:
        marker = "OK  " if c["ok"] else "FAIL"
        detail = f" — {c['detail']}" if c["detail"] else ""
        lines.append(f"  [{marker}] {c['name']}{detail}")
    return "\n".join(lines) + "\n"


def render_json(checks: list[dict]) -> str:
    return json.dumps(
        {"ok": aggregate(checks), "checks": checks},
        indent=2, ensure_ascii=True,
    )


# ---------- entry point ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Light RIP install. Runtime-agnostic: "
                    "checks that reminder.md is readable and that the "
                    "reminder script can start and emit its reminder content.",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable text.")
    args = parser.parse_args()

    # Setup-level pre-check: if reminder.md is missing entirely, the
    # script-side wiring cannot work — bail with code 2.
    if not reminder_md_path().is_file():
        msg = f"setup wrong: {reminder_md_path()} missing"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 2

    checks = run_checks()
    if args.json:
        print(render_json(checks))
    else:
        print(render_human(checks), end="")
    return 0 if aggregate(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())