#!/usr/bin/env python3
"""Shared Light RIP reminder, multi-runtime adapter.

Reads `reminder.md` next to the skill and emits the JSON envelope that
each supported agent runtime expects from its prompt/invocation hook:

  - harness     (default; Claude Code / Codex) — keep the user prompt,
                inject the reminder as `hookSpecificOutput.additionalContext`.
  - antigravity Antigravity `PreInvocation` / `SessionStart` — inject the
                reminder as a `systemMessage` step in `injectSteps`.

The reminder file is the single source of truth. Each runtime has a
thin installer that registers this script as its hook.
Adding a new runtime = adding one entry to FORMATS.

Hook contract (command type, all runtimes):
  - The runtime passes a JSON payload on stdin.
  - The script prints a JSON object on stdout.
  - Empty / invalid stdout is a silent no-op.
  - Non-zero exit / timeout is isolated by the runtime. We fail open:
    missing `reminder.md` does not block the prompt.

Probe mode:
  - When env var `LIGHT_RIP_PROBE=1` is set, the script writes a
    single JSON line to **stderr** containing timestamp, argv,
    --format, and the first 200 bytes of stdout. Stdout is unchanged.
    Use this to audit whether the runtime actually invoked the script
    and what envelope shape was emitted. stderr does not interfere
    with the runtime's stdout parser.
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
    """Return the user's prompt text from a runtime payload, defensively.

    Currently no adapter reads this (harness injects via
    `additionalContext` and antigravity via `injectSteps`), but
    `load_payload()` may legitimately return non-dict values when the
    runtime passes a list, string, or other JSON top-level. Guard against
    both shapes (non-dict payload, non-dict `input` field) so a
    malformed runtime envelope does not crash the hook.
    """
    if not isinstance(payload, dict):
        return ""
    inner = payload.get("input")
    if not isinstance(inner, dict):
        return ""
    raw = inner.get("prompt", "")
    return raw if isinstance(raw, str) else ""


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


def _format_antigravity(payload: dict, reminder: str) -> dict:
    """Antigravity PreInvocation / SessionStart envelope.

    Antigravity expects `injectSteps` containing a nested
    `{"systemMessage": {"systemMessage": "<text>"}}` object and an
    optional `terminationBehavior` (`""` proceeds to the model call).
    """
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": reminder,
                }
            }
        ],
        "terminationBehavior": "",
    }


# Dispatch table. New runtimes plug in here.
FORMATS: dict[str, Callable[[dict, str], dict]] = {
    "harness": _format_harness,
    "antigravity": _format_antigravity,
}


# ---------- entry point ----------

def build_output(fmt: str, payload: dict, reminder: str) -> dict:
    if fmt not in FORMATS:
        # Unknown format = fail open, do not rewrite.
        return {"metadata": {"light_rip_error": f"unknown_format: {fmt}"}}
    return FORMATS[fmt](payload, reminder)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject the Light RIP reminder into a prompt/invocation hook payload."
    )
    parser.add_argument(
        "--format",
        choices=sorted(FORMATS.keys()),
        default="harness",
        help="Agent runtime output format. `harness` (default) for Claude Code / Codex; `antigravity` for Antigravity PreInvocation.",
    )
    args = parser.parse_args()

    payload = load_payload()
    try:
        reminder = load_reminder()
    except (OSError, UnicodeDecodeError) as exc:
        # Fail open: missing or non-UTF-8 reminder must not block the prompt.
        # UnicodeDecodeError is a ValueError subclass, not OSError, so it
        # needs to be listed explicitly to honor the fail-open contract.
        if args.format == "antigravity":
            envelope = {"injectSteps": [], "terminationBehavior": ""}
        else:
            envelope = {"metadata": {"light_rip_error": f"reminder_unreadable: {exc}"}}
        stdout_text = json.dumps(envelope, ensure_ascii=True)
        sys.stdout.write(stdout_text + "\n")
        sys.stdout.flush()
        _maybe_probe(args.format, stdout_text)
        return 0

    envelope = build_output(args.format, payload, reminder)
    stdout_text = json.dumps(envelope, ensure_ascii=True)
    sys.stdout.write(stdout_text + "\n")
    sys.stdout.flush()
    _maybe_probe(args.format, stdout_text)
    return 0


def _maybe_probe(fmt: str, stdout_text: str) -> None:
    """Write a single JSON line to stderr iff LIGHT_RIP_PROBE=1.

    Stderr is used so the runtime's stdout parser is untouched. The
    line captures argv-equivalent info and the first 200 bytes of the
    actual stdout we just emitted, giving us an audit trail when a
    hook is registered but does not appear to fire.
    """
    import os
    import datetime
    if os.environ.get("LIGHT_RIP_PROBE") != "1":
        return
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "argv": sys.argv[1:],
        "format": fmt,
        "stdout_first_200_bytes": stdout_text[:200],
        "stdout_length": len(stdout_text),
    }
    print(json.dumps(record, ensure_ascii=True), file=sys.stderr)
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
