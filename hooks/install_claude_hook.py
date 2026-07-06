#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


NAMESPACE = "light-rip-reminder"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def default_settings_path() -> Path:
    home = Path.home()
    return home / ".claude" / "settings.json"


def build_group(python_exe: Path, hook_script: Path) -> dict:
    return {
        "metadata": {
            "workflow": NAMESPACE,
            "hook_role": "UserPromptSubmit",
            "hook_namespace": NAMESPACE,
        },
        "hooks": [
            {
                "type": "command",
                "command": f"\"{python_exe}\" \"{hook_script}\"",
                "timeout": 5,
                "statusMessage": "Checking whether Light RIP applies...",
            }
        ],
    }


def upsert(settings: dict, group: dict) -> None:
    hooks_root = settings.setdefault("hooks", {})
    event_list = hooks_root.setdefault("UserPromptSubmit", [])
    for index, existing in enumerate(event_list):
        metadata = existing.get("metadata") if isinstance(existing, dict) else None
        if isinstance(metadata, dict) and metadata.get("hook_namespace") == NAMESPACE:
            event_list[index] = group
            return
    event_list.append(group)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Light RIP reminder as a Claude Code UserPromptSubmit hook.")
    parser.add_argument("--settings-file", type=Path, default=default_settings_path())
    args = parser.parse_args()

    settings_path = args.settings_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    settings = load_json(settings_path)
    upsert(settings, build_group(python_exe, hook_script))
    write_json(settings_path, settings)
    print(json.dumps({"settings_path": str(settings_path), "hook": NAMESPACE}, ensure_ascii=True))
    sys.stdout.flush()

    # Always run the runtime-agnostic verifier after install. Its
    # result is informational — the install itself has already
    # landed; verify only checks that reminder.md is readable and
    # the reminder script can spawn successfully. It does not gate
    # the install exit code, because the install can be valid even
    # if a transient runtime environment prevents the verifier from
    # finding reminder.md (e.g. running from a moved worktree).
    verify_path = Path(__file__).resolve().parent / "verify_install.py"
    if verify_path.is_file():
        print("\n--- verify_install.py ---")
        sys.stdout.flush()
        try:
            subprocess.run(
                [sys.executable, str(verify_path)],
                check=False,
            )
        except OSError as exc:
            print(f"verify failed to launch: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
