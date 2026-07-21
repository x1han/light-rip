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

# tomllib is stdlib from Python 3.11; tomli is the backport. tomli_w
# is required for writing because Python < 3.11 has no stdlib TOML
# writer. ImportError is mapped to a clear error message at the call
# site so the user knows to `pip install tomli-w`.
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[import-untyped,no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    import tomli_w  # type: ignore[import-not-found]
except ImportError:
    tomli_w = None  # type: ignore[assignment]

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


class ConfigTomlError(Exception):
    """Raised when ``config.toml`` cannot be safely edited.

    The installer maps this to ``error=invalid_runtime_config`` with
    exit code 2. Use this for parse errors, non-UTF-8 content, non-dict
    root, and missing ``tomli_w`` dependency — every case where the
    file is structurally wrong rather than the system being unable to
    write to disk.
    """


def _is_truthy_hooks(value: object) -> bool:
    """Match Codex's own acceptance of ``[features] hooks`` values.

    Codex treats both the TOML boolean ``true`` and the case-
    insensitive string ``"true"`` as enabled (its schema is more
    permissive than TOML's strict type system). Match that here so a
    user who hand-edited ``hooks = "true"`` is reported as installed
    and so the installer does not rewrite their config.

    Reject any other non-empty string (``"false"``, ``"0"``, …) —
    ``bool("false")`` is True in Python because the string is
    non-empty, which would otherwise be a silent false positive.
    """
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() == "true":
        return True
    return False


def check_hooks_feature(config_path: Path) -> tuple[bool, str | None]:
    """Decide whether ``[features] hooks = true`` needs to be written.

    Returns ``(needs_write, error_message)``:

      * ``(True, None)`` — file does not exist (caller should write a
        fresh ``[features]`` block) OR file exists but
        ``[features] hooks`` is not already truthy per
        ``_is_truthy_hooks``.
      * ``(False, None)`` — file already has ``[features] hooks`` set
        to a truthy value (idempotent: caller should NOT rewrite, to
        preserve the file's existing comments and formatting
        byte-equal).
      * ``(_, "<detail>")`` — file is unreadable / unparseable /
        non-UTF-8 / non-dict root. Caller should report
        ``error=invalid_runtime_config``.

    ``OSError`` propagates to the caller (mapped to
    ``permission_or_io_error``).
    """
    if not config_path.exists():
        return True, None

    if tomllib is None:
        return False, (
            "tomllib/tomli not installed; cannot read config.toml safely. "
            "Install with: pip install tomli"
        )

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (UnicodeDecodeError, ValueError) as exc:
        # ``UnicodeDecodeError`` is a subclass of ``ValueError``; so is
        # ``tomllib.TOMLDecodeError``. Surface them uniformly.
        return False, f"parse error: {exc}"

    if not isinstance(data, dict):
        return False, (
            f"root is not a table (got {type(data).__name__})"
        )

    features = data.get("features")
    if isinstance(features, dict) and _is_truthy_hooks(features.get("hooks")):
        return False, None  # idempotent — preserve file byte-equal

    return True, None


def write_hooks_feature(config_path: Path) -> None:
    """Read ``config.toml``, set ``[features] hooks = true``, write back.

    Caller must have already backed up ``config_path`` (via
    ``backup_file``). This function reads + mutates + writes; if any
    step fails it propagates ``OSError``. Raises ``ConfigTomlError`` if
    ``tomli_w`` is missing — the user needs ``pip install tomli-w``.

    Known regression: ``tomli_w.dump`` does NOT preserve TOML comments
    or original formatting. We only reach this code path when the
    existing ``[features] hooks`` value differs from ``True``; if the
    file already has ``hooks = true``, ``check_hooks_feature`` returns
    ``(False, None)`` and this function is not called, so comments are
    preserved.
    """
    if tomli_w is None:
        raise ConfigTomlError(
            "tomli_w not installed; cannot write config.toml. "
            "Install with: pip install tomli-w"
        )

    if not config_path.exists():
        atomic_write_text(config_path, "[features]\nhooks = true\n")
        return

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    if not isinstance(data, dict):
        # Caller should have caught this via ``check_hooks_feature``;
        # defensive guard.
        raise ConfigTomlError(
            f"config.toml root is not a table (got {type(data).__name__})"
        )

    features = data.get("features")
    if not isinstance(features, dict):
        features = {}
        data["features"] = features
    features["hooks"] = True

    with open(config_path, "wb") as f:
        tomli_w.dump(data, f)


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

    # Step 4: enable [features] hooks = true in config.toml, applying
    # the same parse-before-backup invariant as hooks.json. We
    # check first, then back up only when a write is actually
    # needed. Idempotent re-installs (hooks already true) make no
    # backup and no write — the file stays byte-equal so its
    # comments and formatting are preserved.
    if not args.no_enable_feature:
        try:
            needs_write, parse_err = check_hooks_feature(config_path)
        except OSError as exc:
            print(json.dumps({
                "error": "permission_or_io_error",
                "stage": "load",
                "target": "config",
                "hooks_path": str(hooks_path),
                "config_path": str(config_path),
                "hooks_backup": str(hooks_bak) if hooks_bak else None,
                "detail": str(exc),
                "hint": ("check that config.toml is readable; this is a "
                         "filesystem problem, not a config error"),
            }, ensure_ascii=True), file=sys.stderr)
            return 2

        if parse_err:
            print(json.dumps({
                "error": "invalid_runtime_config",
                "stage": "load",
                "target": "config",
                "hooks_path": str(hooks_path),
                "config_path": str(config_path),
                "hooks_backup": str(hooks_bak) if hooks_bak else None,
                "detail": parse_err,
                "hint": ("fix or remove the corrupt config.toml first; do "
                         "NOT pass --force-backup because no good backup "
                         "exists yet"),
            }, ensure_ascii=True), file=sys.stderr)
            return 2

        if needs_write:
            # Parse-before-backup: source has been validated; only now
            # do we create a recovery snapshot.
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
                    "hint": ("pass --force-backup to overwrite, or rename "
                             "the existing backup (recovery paths printed "
                             "in the error key are NOT shell-escaped; quote "
                             "them with your shell of choice when copying)"),
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
                    "hint": ("check that the parent directory is writable; "
                             "this is a filesystem problem, not a same-day "
                             "collision"),
                }, ensure_ascii=True), file=sys.stderr)
                return 2

            try:
                write_hooks_feature(config_path)
            except ConfigTomlError as exc:
                # Typically: tomli_w is not installed, or the source
                # became non-dict between check and write. Map to
                # invalid_runtime_config so callers see one envelope
                # for all config-shape problems.
                print(json.dumps({
                    "error": "invalid_runtime_config",
                    "stage": "write",
                    "target": "config",
                    "hooks_path": str(hooks_path),
                    "config_path": str(config_path),
                    "hooks_backup": str(hooks_bak) if hooks_bak else None,
                    "config_backup": str(config_bak) if config_bak else None,
                    "detail": str(exc),
                    "hint": ("install tomli_w with `pip install tomli-w`, "
                             "or fix the config.toml shape"),
                }, ensure_ascii=True), file=sys.stderr)
                return 2
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
                             "above; the reported paths are NOT shell-"
                             "escaped — quote them with your shell of "
                             "choice when copying"),
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