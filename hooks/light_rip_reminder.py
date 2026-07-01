#!/usr/bin/env python3
"""Shared Light RIP reminder, multi-runtime adapter.

Reads `reminder.md` next to the skill and emits the JSON envelope that
each supported agent runtime expects from a UserPromptSubmit hook:

  - harness   (default; Claude Code / Codex) — keep the user prompt,
              inject the reminder as `hookSpecificOutput.additionalContext`.
  - mavis     (Mavis) — rewrite the `prompt` field, prepending the
              reminder above a separator so the agent still sees the
              original user text.

The reminder file is the single source of truth. Each runtime has a
thin installer that registers this script as its UserPromptSubmit hook
with the right --format flag. Adding a new runtime = adding one entry
to FORMATS.

Hook contract (script type, all runtimes):
  - The runtime passes {"input": ..., "output": ...} as JSON on stdin.
  - The script prints a JSON object on stdout; the runtime merges
    printed fields into the `output` envelope.
  - Empty / invalid stdout is a silent no-op (original output kept).
  - Non-zero exit / timeout is isolated and logged; later hooks still
    run. We fail open: missing `reminder.md` does not block the prompt.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable


# ---------- reminder loading ----------

def load_payload() -> dict:
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        try:
            return json.loads(raw.decode(errors="replace"))
        except json.JSONDecodeError:
            return {}
    except json.JSONDecodeError:
        return {}


def load_reminder() -> str:
    return (Path(__file__).resolve().parents[1] / "reminder.md").read_text(encoding="utf-8")


def _original_prompt(payload: dict) -> str:
    return (payload.get("input") or {}).get("prompt", "")


# ---------- format adapters ----------

def _format_harness(payload: dict, reminder: str) -> dict:
    """Claude Code / Codex style — inject reminder as additional context."""
    return {
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reminder,
        },
    }


def _format_mavis(payload: dict, reminder: str) -> dict:
    """Mavis style — the only injection point is the rewritten `prompt`."""
    original = _original_prompt(payload)
    rewritten = (
        f"{reminder.rstrip()}\n\n---\n\nUser prompt follows:\n\n{original}"
        if original
        else reminder
    )
    return {
        "prompt": rewritten,
        "metadata": {
            "light_rip": True,
            "reminder_event": "UserPromptSubmit",
        },
    }


# Dispatch table. New runtimes plug in here.
FORMATS: dict[str, Callable[[dict, str], dict]] = {
    "harness": _format_harness,
    "mavis": _format_mavis,
}


# ---------- entry point ----------

def build_output(fmt: str, payload: dict, reminder: str) -> dict:
    if fmt not in FORMATS:
        # Unknown format = fail open, do not rewrite.
        return {"metadata": {"light_rip_error": f"unknown_format: {fmt}"}}
    return FORMATS[fmt](payload, reminder)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject the Light RIP reminder into a UserPromptSubmit hook payload."
    )
    parser.add_argument(
        "--format",
        choices=sorted(FORMATS.keys()),
        default="harness",
        help="Agent runtime output format (default: harness for Claude Code / Codex back-compat).",
    )
    args = parser.parse_args()

    payload = load_payload()
    try:
        reminder = load_reminder()
    except OSError as exc:
        # Fail open: missing reminder must not block the prompt.
        print(json.dumps({"metadata": {"light_rip_error": f"reminder_unreadable: {exc}"}}))
        return 0

    print(json.dumps(build_output(args.format, payload, reminder), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
