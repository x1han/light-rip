#!/usr/bin/env python3
"""Shared Light RIP reminder, multi-runtime adapter.

Reads `reminder.md` next to the skill and emits the JSON envelope that
each supported agent runtime expects from a UserPromptSubmit hook:

  - harness   (default; Claude Code / Codex) — keep the user prompt,
              inject the reminder as `hookSpecificOutput.additionalContext`.
  - zcode     (ZCode) — strict-schema envelope. Emits ONLY the
              documented `additionalContext` field; extras like
              `continue` / `suppressOutput` / `hookSpecificOutput` are
              dropped by ZCode's strict validator.

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

    Currently no adapter reads this (harness/zcode inject via
    `additionalContext`), but `load_payload()` may legitimately return
    non-dict values when the runtime passes a list, string, or other
    JSON top-level. Guard against both shapes (non-dict payload,
    non-dict `input` field) so a malformed runtime envelope does not
    crash the hook.
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


def _format_zcode(payload: dict, reminder: str) -> dict:
    """ZCode strict-schema envelope.

    ZCode validates the hook stdout against a strict JSON schema and
    drops the entire entry on any extra top-level key (per the
    `diagnosing-hooks` skill shipped with ZCode 0.1.0+). The only
    documented text-injection field is `additionalContext`. We emit
    exactly that and nothing else — no `continue`, no
    `suppressOutput`, no `hookSpecificOutput` wrapper.

    This shape is the **empirically untested minimum**. If ZCode
    actually requires additional fields (e.g. an explicit event name
    wrapper), the next stage of investigation will surface that and
    this function will be amended.
    """
    return {"additionalContext": reminder}


# Dispatch table. New runtimes plug in here.
FORMATS: dict[str, Callable[[dict, str], dict]] = {
    "harness": _format_harness,
    "zcode": _format_zcode,
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
        help="Agent runtime output format (default: harness for Claude Code / Codex back-compat; zcode for ZCode strict-schema).",
    )
    args = parser.parse_args()

    payload = load_payload()
    try:
        reminder = load_reminder()
    except (OSError, UnicodeDecodeError) as exc:
        # Fail open: missing or non-UTF-8 reminder must not block the prompt.
        # UnicodeDecodeError is a ValueError subclass, not OSError, so it
        # needs to be listed explicitly to honor the fail-open contract.
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
