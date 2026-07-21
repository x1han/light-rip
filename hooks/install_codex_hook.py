#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from installer_common import (
    BackupCollisionError,
    atomic_write_json,
    atomic_write_text,
    backup_file,
)


NAMESPACE = "light-rip-reminder"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ensure_hooks_feature(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        atomic_write_text(config_path, "[features]\nhooks = true\n")
        return

    lines = config_path.read_text(encoding="utf-8-sig").splitlines()
    features_start = None
    next_section = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[features]":
            features_start = index
            continue
        if features_start is not None and index > features_start and stripped.startswith("[") and stripped.endswith("]"):
            next_section = index
            break

    if features_start is None:
        prefix = lines + ([] if not lines or lines[-1] == "" else [""])
        prefix.extend(["[features]", "hooks = true"])
        atomic_write_text(config_path, "\n".join(prefix) + "\n")
        return

    for index in range(features_start + 1, next_section):
        if lines[index].strip().startswith("hooks"):
            lines[index] = "hooks = true"
            atomic_write_text(config_path, "\n".join(lines) + "\n")
            return

    lines.insert(next_section, "hooks = true")
    atomic_write_text(config_path, "\n".join(lines) + "\n")


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
                "statusMessage": "Checking whether Light RIP applies...",
            }
        ],
    }


def upsert(hooks_config: dict, group: dict) -> None:
    hooks_root = hooks_config.setdefault("hooks", {})
    event_list = hooks_root.setdefault("UserPromptSubmit", [])
    for index, existing in enumerate(event_list):
        metadata = existing.get("metadata") if isinstance(existing, dict) else None
        if isinstance(metadata, dict) and metadata.get("hook_namespace") == NAMESPACE:
            event_list[index] = group
            return
    event_list.append(group)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Light RIP reminder as a Codex UserPromptSubmit hook.")
    parser.add_argument("--hooks-file", type=Path, default=codex_home() / "hooks.json")
    parser.add_argument("--config-file", type=Path, default=codex_home() / "config.toml")
    parser.add_argument("--no-enable-feature", action="store_true", help="Do not edit config.toml to enable Codex hooks.")
    parser.add_argument("--force-backup", action="store_true",
                        help="Overwrite an existing <path>.bak-YYYY-MM-DD "
                             "backup instead of aborting with exit 2.")
    parser.add_argument("--strict-verify", action="store_true",
                        help="Exit with the verifier's code if verify_install.py "
                             "reports a problem. Default OFF for backward "
                             "compat; will flip to ON in a future release.")
    args = parser.parse_args()

    hooks_path = args.hooks_file.expanduser().resolve()
    config_path = args.config_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Back up BOTH target files before any write. Collision on either
    # aborts the whole install so a partial backup never looks good.
    try:
        hooks_bak = backup_file(hooks_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "target": "hooks",
            "hooks_path": str(hooks_path),
            "backup_path": exc.backup_path,
            "hint": "pass --force-backup to overwrite, or rename the existing backup",
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    try:
        config_bak = backup_file(config_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "target": "config",
            "config_path": str(config_path),
            "backup_path": exc.backup_path,
            "hooks_backup": str(hooks_bak) if hooks_bak else None,
            "hint": "pass --force-backup to overwrite, or rename the existing backup",
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    hooks_config = load_json(hooks_path)
    upsert(hooks_config, build_group(python_exe, hook_script))
    try:
        atomic_write_json(hooks_path, hooks_config)
    except Exception as exc:
        print(json.dumps({
            "error": "write_failed",
            "target": "hooks",
            "hooks_path": str(hooks_path),
            "config_path": str(config_path),
            "hooks_backup": str(hooks_bak) if hooks_bak else None,
            "config_backup": str(config_bak) if config_bak else None,
            "detail": str(exc),
            "hint": "restore from backup with: cp '<backup>' '<target>'",
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    if not args.no_enable_feature:
        try:
            ensure_hooks_feature(config_path)
        except Exception as exc:
            print(json.dumps({
                "error": "write_failed",
                "target": "config",
                "hooks_path": str(hooks_path),
                "config_path": str(config_path),
                "hooks_backup": str(hooks_bak) if hooks_bak else None,
                "config_backup": str(config_bak) if config_bak else None,
                "detail": str(exc),
                "hint": "restore from backup with: cp '<backup>' '<target>'",
            }, ensure_ascii=True), file=sys.stderr)
            return 2

    print(json.dumps({
        "hooks_path": str(hooks_path),
        "config_path": str(config_path),
        "hook": NAMESPACE,
        "backups": {
            "hooks": str(hooks_bak) if hooks_bak else None,
            "config": str(config_bak) if config_bak else None,
        },
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
             "--runtime", "codex",
             "--codex-hooks", str(hooks_path),
             "--codex-config", str(config_path),
             "--json"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        print(f"verify failed to launch: {exc}", file=sys.stderr)
        return 1 if args.strict_verify else 0
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        msg = (f"verify_install.py reported FAIL "
               f"(exit {completed.returncode}); "
               f"install at {hooks_path} may be incomplete")
        print(msg, file=sys.stderr)
        return completed.returncode if args.strict_verify else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
