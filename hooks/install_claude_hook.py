#!/usr/bin/env python3
"""Install the Light RIP reminder as a Claude Code UserPromptSubmit hook.

Writes (or merges) an entry into ``~/.claude/settings.json`` whose
``hooks.UserPromptSubmit`` list contains a single Light RIP entry
identified by ``metadata.hook_namespace == NAMESPACE``. The shared
``installer_common`` helpers handle backup (date-stamped, abort on
collision), atomic write (temp + fsync + rename), and dedup-all
upsert behavior.

Order of operations matters:

  1. Parse the existing config with ``load_json_safe`` BEFORE
     touching any backup. If the file is corrupt, we abort with
     ``error=invalid_runtime_config`` and do NOT create a recovery
     snapshot from known-bad content.
  2. Only when the existing config is usable (either a valid dict or
     absent) do we call ``backup_file``. The backup is the user's
     recovery point; it must never point at corrupt content.
  3. Upsert the new group via ``upsert_light_rip`` (removes ALL
     existing entries in our namespace, not just the first).
  4. Write atomically via ``atomic_write_json``.

Exit codes:
  0 — install + verifier both pass (or verifier passed under
      ``--no-strict-verify``).
  1 — write succeeded but verifier failed; surfaced as a warning,
      not a hard failure, unless ``--strict-verify`` is on (default).
  2 — install failed: invalid_runtime_config, backup_collision,
      permission_or_io_error, write_failed, or strict-mode
      verification failure.

Backups are written to ``<settings>.bak-YYYY-MM-DD`` (UTC). A
collision with an existing same-day backup aborts with
``error=backup_collision``; pass ``--force-backup`` to overwrite or
rename the existing backup manually. ``OSError`` on backup or write
is reported separately as ``permission_or_io_error`` so the caller
can distinguish "you have a choice" from "I literally cannot write
here".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from installer_common import (
    BackupCollisionError,
    NAMESPACE,
    atomic_write_json,
    backup_file,
    load_json_safe,
    upsert_light_rip,
)


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Light RIP reminder as a Claude Code "
                    "UserPromptSubmit hook."
    )
    parser.add_argument(
        "--settings-file", type=Path,
        default=default_settings_path(),
    )
    parser.add_argument(
        "--force-backup", action="store_true",
        help="Overwrite an existing <settings>.bak-YYYY-MM-DD backup "
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
             "missing verify_install.py). Useful for transient runtime "
             "environments where the verifier cannot reach reminder.md.",
    )
    args = parser.parse_args()

    settings_path = args.settings_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse the existing config FIRST. If it is corrupt or
    # non-object, abort BEFORE touching any backup — we must never
    # create a recovery snapshot from known-bad content.
    existing, load_err = load_json_safe(settings_path)
    if load_err and load_err != "file_not_found":
        print(json.dumps({
            "error": "invalid_runtime_config",
            "stage": "load",
            "settings_path": str(settings_path),
            "detail": load_err,
            "hint": ("fix or remove the corrupt config first; do NOT pass "
                     "--force-backup because no good backup exists yet"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    if existing is None:
        existing = {}

    # Step 2: back up the (known-good) source, or skip if absent.
    try:
        backup_path = backup_file(settings_path, force=args.force_backup)
    except BackupCollisionError as exc:
        print(json.dumps({
            "error": "backup_collision",
            "settings_path": str(settings_path),
            "backup_path": exc.backup_path,
            "hint": ("pass --force-backup to overwrite, or rename the "
                     "existing backup (recovery paths printed in the "
                     "error key are NOT shell-escaped; quote them with "
                     "your shell of choice when copying)"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2
    except OSError as exc:
        # Distinct error key from backup_collision: here we literally
        # cannot write to the parent directory, so force-backup would
        # not help.
        print(json.dumps({
            "error": "permission_or_io_error",
            "stage": "backup",
            "settings_path": str(settings_path),
            "detail": str(exc),
            "hint": ("check that the parent directory is writable; this "
                     "is a filesystem problem, not a same-day collision"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    # Step 3: upsert. Caller-provided group must set its own
    # metadata.hook_namespace so future upserts can dedupe it.
    upsert_light_rip(existing, build_group(python_exe, hook_script))

    # Step 4: atomic write. A crash mid-write leaves the previous
    # config intact (atomic_write_json stages in a sibling temp file,
    # fsyncs, then os.replace's over the destination).
    try:
        atomic_write_json(settings_path, existing)
    except OSError as exc:
        print(json.dumps({
            "error": "permission_or_io_error",
            "stage": "write",
            "settings_path": str(settings_path),
            "backup_path": str(backup_path) if backup_path else None,
            "detail": str(exc),
            "hint": ("restore manually from the backup path above; the "
                     "reported paths are NOT shell-escaped — quote them "
                     "with your shell of choice when copying"),
        }, ensure_ascii=True), file=sys.stderr)
        return 2

    # Print the success record BEFORE running the verifier so the
    # record survives even if the verifier fails afterwards.
    print(json.dumps({
        "settings_path": str(settings_path),
        "hook": NAMESPACE,
        "backup_path": str(backup_path) if backup_path else None,
    }, ensure_ascii=True))
    sys.stdout.flush()

    # Always run the runtime-agnostic verifier after install. Under
    # --strict-verify (the default), missing verify_install.py and
    # non-zero verifier exit codes propagate; under --no-strict-verify,
    # the verifier is informational only.
    verify_path = Path(__file__).resolve().parent / "verify_install.py"
    if not verify_path.is_file():
        if args.strict_verify:
            print(json.dumps({
                "error": "verify_missing",
                "settings_path": str(settings_path),
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
             "--runtime", "claude",
             "--claude-settings", str(settings_path),
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
               f"install at {settings_path} may be incomplete")
        print(msg, file=sys.stderr)
        return completed.returncode if args.strict_verify else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())