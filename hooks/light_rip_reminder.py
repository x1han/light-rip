#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_payload() -> dict:
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            return json.loads(raw.decode(errors="replace"))
        except json.JSONDecodeError:
            return {}


def load_reminder() -> str:
    return (Path(__file__).resolve().parents[1] / "reminder.md").read_text(encoding="utf-8")


def build_output(event_name: str, reminder: str) -> dict:
    return {
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": reminder,
        },
    }


def main() -> int:
    payload = load_payload()
    event_name = payload.get("hook_event_name") or payload.get("hookEventName") or "UserPromptSubmit"
    print(json.dumps(build_output(str(event_name), load_reminder()), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
