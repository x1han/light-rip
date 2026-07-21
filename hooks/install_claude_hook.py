#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from installer_common import (
    BackupCollisionError,
    atomic_write_json,
    backup_file,
)


NAMESPACE = "light-rip-reminder"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


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
    parser.add_argument("--force-backup", action="store_true",
                        help="Overwrite an existing <settings>.bak-YYYY-MM-DD "
                             "backup instead of aborting with exit 2.")
    parser.add_argument("--strict-verify", action="store_true",
                        help="Exit with the verifier's code if verify_install.py "
                             "reports a problem. Default OFF for backward "
                             "compat; will flip to ON in a future release.")
    args = parser.parse_args()

    settings_path = args.settings_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    try:
        hooks_bak = backup_file(settings_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "settings_path": str(settings_path),
            "backup_path": exc.backup_path,
            "hint": "pass --force-backup to overwrite, or rename the existing backup",
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    settings = load_json(settings_path)
    upsert(settings, build_group(python_exe, hook_script))
    try:
        atomic_write_json(settings_path, settings)
    except Exception as exc:
        print(json.dumps({
            "error": "write_failed",
            "settings_path": str(settings_path),
            "backup_path": str(hooks_bak) if hooks_bak else None,
            "detail": str(exc),
            "hint": "restore from backup with: cp '<backup_path>' '<settings_path>'",
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    print(json.dumps({
        "settings_path": str(settings_path),
        "hook": NAMESPACE,
        "backup_path": str(hooks_bak) if hooks_bak else None,
    }, ensure_ascii=True))
    sys.stdout.flush()

    # Always run the runtime-agnostic verifier after install. With
    # --strict-verify, the verifier's exit code propagates to this
    # installer's exit code. Without it (default), verify is
    # informational only — backward compat for users who may have
    # transient runtime environments where the verifier cannot reach
    # reminder.md (e.g. running from a moved worktree).
    verify_path = Path(__file__).resolve().parent / "verify_install.py"
    if not verify_path.is_file():
        return 0
    print("\n--- verify_install.py ---")
    sys.stdout.flush()
    try:
        completed = subprocess.run(
            [sys.executable, str(verify_path),
             "--runtime", "claude",
             "--claude-settings", str(settings_path),
             "--json"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        print(f"verify failed to launch: {exc}", file=sys.stderr)
        return 1 if args.strict_verify else 0
    if completed.stdout:
        # human-readable on top, JSON tail from the verifier
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        msg = (f"verify_install.py reported FAIL "
               f"(exit {completed.returncode}); "
               f"install at {settings_path} may be incomplete")
        print(msg, file=sys.stderr)
        return completed.returncode if args.strict_verify else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
