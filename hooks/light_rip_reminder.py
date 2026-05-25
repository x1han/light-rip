#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def read_prompt(payload: dict) -> str:
    prompt = payload.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def likely_code_request(prompt: str) -> bool:
    text = prompt.lower()
    keywords = [
        "code",
        "coding",
        "implement",
        "implementation",
        "bug",
        "fix",
        "refactor",
        "feature",
        "test",
        "tests",
        "edit",
        "modify",
        "change",
        "patch",
        "文件",
        "代码",
        "实现",
        "修复",
        "改",
        "重构",
        "测试",
    ]
    return any(keyword in text for keyword in keywords)


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
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    event_name = payload.get("hook_event_name") or payload.get("hookEventName") or "UserPromptSubmit"
    prompt = read_prompt(payload)

    if not likely_code_request(prompt):
        print(json.dumps({"continue": True, "suppressOutput": True}, ensure_ascii=True))
        return 0

    print(json.dumps(build_output(str(event_name), load_reminder()), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
