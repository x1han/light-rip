#!/usr/bin/env python3
"""Install the Light RIP reminder as a Codex UserPromptSubmit hook.

Writes two files in ``$CODEX_HOME`` (default ``~/.codex``):

  * ``hooks.json``  — JSON entry under ``hooks.UserPromptSubmit``
                       identified by ``metadata.hook_namespace ==
                       NAMESPACE``. Merged with the existing file
                       via the shared ``upsert_light_rip`` helper.
  * ``config.toml`` — ``[features] hooks = true`` flag inserted if
                       not already present. Done via regex line
                       surgery (no TOML parser dependency; mirrors
                       the install path Codex itself uses).

Order of operations (matching install_claude_hook.py):

  1. Parse the existing ``hooks.json`` with ``load_json_safe``
     FIRST. Abort with ``error=invalid_runtime_config`` if it is
     corrupt or non-object. Do NOT create any recovery snapshot
     from known-bad content.
  2. Back up ``hooks.json`` (or skip if absent), then ``config.toml``
     (or skip if absent). Collision on either aborts the whole
     install so a partial backup never looks good.
  3. Upsert + atomic-write ``hooks.json``.
  4. Enable the feature in ``config.toml`` via regex surgery.

Exit codes:
  0 — install + verifier both pass (or verifier passed under
      ``--no-strict-verify``).
  1 — write succeeded but verifier failed; surfaced as a warning
      unless ``--strict-verify`` is on (default).
  2 — install failed: invalid_runtime_config, backup_collision,
      permission_or_io_error, write_failed, or strict-mode
      verification failure.

``OSError`` on backup or write is reported separately as
``permission_or_io_error`` so the caller can distinguish "you have a
choice (force or rename)" from "I literally cannot write here".
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from installer_common import (
    BackupCollisionError,
    NAMESPACE,
    atomic_write_json,
    atomic_write_text,
    backup_file,
    load_json_safe,
    upsert_light_rip,
)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def ensure_hooks_feature(config_path: Path) -> None:
    """Ensure ``[features] hooks = true`` exists in ``config.toml``.

    Uses regex line surgery rather than a TOML parser so this works
    on any Python version. Raises ``OSError`` on read or write
    failure; the function does not validate the file's overall
    shape — it appends or rewrites the ``[features]`` block and
    leaves other content alone.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        atomic_write_text(config_path, "[features]\nhooks = true\n")
        return

    try:
        raw = config_path.read_text(encoding="utf-8-sig")
    except OSError:
        raise
    lines = raw.splitlines()
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Light RIP reminder as a Codex "
                    "UserPromptSubmit hook."
    )
    parser.add_argument("--hooks-file", type=Path,
                        default=codex_home() / "hooks.json")
    parser.add_argument("--config-file", type=Path,
                        default=codex_home() / "config.toml")
    parser.add_argument("--no-enable-feature", action="store_true",
                        help="Do not edit config.toml to enable Codex hooks.")
    parser.add_argument(
        "--force-backup", action="store_true",
        help="Overwrite an existing <path>.bak-YYYY-MM-DD backup "
             "instead of aborting with error=backup_collision.",
    )
    verify_group = parser.add_mutually_exclusive_group()
    verify_group.add_argument(
        "--strict-verify", dest="strict_verify",
        action="store_true", default=True,
        help="(default) Exit with the verifier's code if verify_install.py "
             "reports a problem. Treats missing verify_install.py as a "
             "verification failure.",
    )
    verify_group.add_argument(
        "--no-strict-verify", dest="strict_verify",
        action="store_false",
        help="Run the verifier but ignore its exit code (and tolerate a "
             "missing verify_install.py).",
    )
    args = parser.parse_args()

    hooks_path = args.hooks_file.expanduser().resolve()
    config_path = args.config_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse hooks.json FIRST. Abort before any backup if it
    # is corrupt — a recovery snapshot from known-bad content would
    # later be overwritten by --force-backup into a "good" backup
    # that is actually corrupt.
    existing, load_err = load_json_safe(hooks_path)
    if load_err and load_err != "file_not_found":
        print(json.dumps({
            "error": "invalid_runtime_config",
            "stage": "load",
            "hooks_path": str(hooks_path),
            "detail": load_err,
            "hint": ("fix or remove the corrupt hooks.json first; do NOT "
                     "pass --force-backup because no good backup exists yet"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    if existing is None:
        existing = {}

    # Step 2: back up BOTH target files. Collision on either aborts
    # the whole install so a partial backup never looks good.
    hooks_bak = None
    config_bak = None
    try:
        hooks_bak = backup_file(hooks_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "target": "hooks",
            "hooks_path": str(hooks_path),
            "backup_path": exc.backup_path,
            "hint": ("pass --force-backup to overwrite, or rename the "
                     "existing backup (recovery paths printed in the "
                     "error key are NOT shell-escaped; quote them with "
                     "your shell of choice when copying)"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({
            "error": "permission_or_io_error",
            "stage": "backup",
            "target": "hooks",
            "hooks_path": str(hooks_path),
            "detail": str(exc),
            "hint": ("check that the parent directory is writable; this "
                     "is a filesystem problem, not a same-day collision"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    try:
        config_bak = backup_file(config_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "target": "config",
            "hooks_path": str(hooks_path),
            "config_path": str(config_path),
            "backup_path": exc.backup_path,
            "hooks_backup": str(hooks_bak) if hooks_bak else None,
            "hint": ("pass --force-backup to overwrite, or rename the "
                     "existing backup (recovery paths printed in the "
                     "error key are NOT shell-escaped; quote them with "
                     "your shell of choice when copying)"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({
            "error": "permission_or_io_error",
            "stage": "backup",
            "target": "config",
            "hooks_path": str(hooks_path),
            "config_path": str(config_path),
            "hooks_backup": str(hooks_bak) if hooks_bak else None,
            "detail": str(exc),
            "hint": ("check that the parent directory is writable; this "
                     "is a filesystem problem, not a same-day collision"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    # Step 3: upsert + atomic write hooks.json.
    upsert_light_rip(existing, build_group(python_exe, hook_script))
    try:
        atomic_write_json(hooks_path, existing)
    except OSError as exc:
        print(json.dumps({
            "error": "permission_or_io_error",
            "stage": "write",
            "target": "hooks",
            "hooks_path": str(hooks_path),
            "config_path": str(config_path),
            "hooks_backup": str(hooks_bak) if hooks_bak else None,
            "config_backup": str(config_bak) if config_bak else None,
            "detail": str(exc),
            "hint": ("restore manually from the hooks_backup path above; "
                     "the reported paths are NOT shell-escaped — quote "
                     "them with your shell of choice when copying"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    # Step 4: enable the feature in config.toml.
    if not args.no_enable_feature:
        try:
            ensure_hooks_feature(config_path)
        except OSError as exc:
            print(json.dumps({
                "error": "permission_or_io_error",
                "stage": "write",
                "target": "config",
                "hooks_path": str(hooks_path),
                "config_path": str(config_path),
                "hooks_backup": str(hooks_bak) if hooks_bak else None,
                "config_backup": str(config_bak) if config_bak else None,
                "detail": str(exc),
                "hint": ("restore manually from the config_backup path "
                         "above; the reported paths are NOT shell-escaped "
                     "— quote them with your shell of choice when copying"),
            }, ensure_ascii=True), file=sys.stderr)
            return 2

    # Print the success record BEFORE running the verifier so the
    # record survives even if the verifier fails afterwards.
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

    # Always run the runtime-agnostic verifier after install. Under
    # --strict-verify (the default), missing verify_install.py and
    # non-zero verifier exit codes propagate; under
    # --no-strict-verify, the verifier is informational only.
    verify_path = Path(__file__).resolve().parent / "verify_install.py"
    if not verify_path.is_file():
        if args.strict_verify:
            print(json.dumps({
                "error": "verify_missing",
                "hooks_path": str(hooks_path),
                "expected": str(verify_path),
                "hint": ("verify_install.py is missing from the install; "
                         "the install itself succeeded, but the script-"
                         "side smoke check could not run. Re-clone the "
                         "skill or restore verify_install.py."),
            }, ensure_ascii=True), file=sys.stderr)
            return 2
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