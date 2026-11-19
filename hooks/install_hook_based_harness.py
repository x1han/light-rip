#!/usr/bin/env python3
"""Install the Light RIP reminder hook on supported agent runtimes.

Single dispatcher taking ``--runtime {claude,codex,antigravity}``.

Per-runtime behavior:

  * ``claude``      — merges one entry into ``~/.claude/settings.json``
                      under ``hooks.UserPromptSubmit`` (JSON hooks
                      schema).
  * ``codex``       — writes ``~/.codex/hooks.json`` (JSON hooks schema)
                      AND enables ``[features] hooks = true`` in
                      ``~/.codex/config.toml`` (TOML, via ``tomllib`` +
                      ``tomli_w``; see ``Dependencies`` below).
  * ``antigravity`` — merges the ``light-rip-reminder`` entry into
                      ``~/.gemini/config/hooks.json`` under
                      ``PreInvocation`` (Antigravity lifecycle hooks
                      schema, invoking ``light_rip_reminder.py --format
                      antigravity``).

All three branches share the same invariant set:

  1. Parse the existing config with ``load_json_safe`` (or
     ``check_hooks_feature`` for TOML) BEFORE touching any backup.
     Corrupt or non-object content aborts with
     ``error=invalid_runtime_config`` BEFORE any recovery snapshot
     is taken — we must never create a recovery snapshot from
     known-bad content.
  2. Back up the (known-good) source via ``backup_file``
     (``<path>.bak-YYYY-MM-DD``, UTC). A same-day collision aborts
     with ``error=backup_collision`` unless ``--force-backup`` is
     passed. ``OSError`` on backup is reported separately as
     ``permission_or_io_error`` so the caller can distinguish "you
     have a choice (force or rename)" from "I literally cannot
     write here".
  3. Idempotent upsert. Re-install on an already-correct config
     leaves existing entries intact and avoids unnecessary rewrites.
  4. Atomic write via ``atomic_write_json`` (temp file in the
     target directory, fsync, ``os.replace`` over destination).
  5. Run ``verify_install.py`` as the final step. Under
     ``--strict-verify`` (default), missing verifier or non-zero
     verifier exit codes propagate. Under ``--no-strict-verify``, the
     verifier is informational only.

Exit codes:
  0 — install + verifier both pass (or verifier passed under
      ``--no-strict-verify``).
  1 — write succeeded but verifier failed; surfaced as a warning,
      not a hard failure, unless ``--strict-verify`` is on (default).
  2 — install failed: invalid_runtime_config, backup_collision,
      permission_or_io_error, write_failed, flag_not_applicable_for_runtime,
      or strict-mode verification failure.

Dependencies (Codex branch only):
  Python 3.11+ ships ``tomllib`` (read). ``tomli_w`` is a third-party
  package required for writing because Python < 3.11 has no stdlib
  TOML writer and ``tomli_w`` is the de-facto choice. Install with:
      pip install tomli-w
  On Python >= 3.11 you also need ``tomllib`` (stdlib — no install
  needed) and on older Pythons you need ``tomli`` as the backport.
  The Claude and Antigravity branches need no extra dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# tomllib is stdlib from Python 3.11; tomli is the backport.
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
    NAMESPACE,
    BackupCollisionError,
    atomic_write_json,
    atomic_write_text,
    backup_file,
    load_json_safe,
    upsert_light_rip,
)


# ---------------------------------------------------------------------------
# Path defaults (per runtime)
# ---------------------------------------------------------------------------

def default_claude_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def default_codex_hooks() -> Path:
    return codex_home() / "hooks.json"


def default_codex_config() -> Path:
    return codex_home() / "config.toml"


def default_antigravity_hooks() -> Path:
    return Path.home() / ".gemini" / "config" / "hooks.json"


# ---------------------------------------------------------------------------
# Hook-group builders (JSON hooks schema for Claude + Codex + Antigravity)
# ---------------------------------------------------------------------------

def build_claude_group(python_exe: Path, hook_script: Path) -> dict:
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


def build_codex_group(python_exe: Path, hook_script: Path) -> dict:
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


def build_antigravity_hook(python_exe: Path, hook_script: Path) -> dict:
    """Build the Antigravity hooks.json entry under top-level key NAMESPACE."""
    return {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": f"\"{python_exe}\" \"{hook_script}\" --format antigravity",
                "timeout": 10,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Codex config.toml machinery (parse-before-backup + idempotent skip)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Error envelope helper + flag/runtime mismatch validation
# ---------------------------------------------------------------------------

_FLAG_OWNER = {
    "hooks_file": ("--hooks-file", "Codex", ("codex",)),
    "config_file": ("--config-file", "Codex", ("codex",)),
    "settings_file": ("--settings-file", "Claude", ("claude",)),
    "antigravity_hooks": ("--antigravity-hooks", "Antigravity", ("antigravity",)),
}


def _emit_error(error: str, *, code: int = 2, **fields) -> int:
    """Write a JSON error envelope to stderr and return the exit code.

    Use as ``return _emit_error("backup_collision", settings_path=...)``.
    The ``error`` field becomes the JSON ``error`` key; all keyword
    fields are merged into the envelope. The default code is 2
    (install failure).

    Wire shape: one JSON object per line, ``ensure_ascii=True``, on
    stderr. Matches every envelope in the pre-unification installers
    (Claude/Codex) — this is a pure refactor, no envelope shape change.
    """
    payload = {"error": error, **fields}
    print(json.dumps(payload, ensure_ascii=True), file=sys.stderr)
    return code


def _reject(flag: str, runtime: str, hint: str) -> int:
    return _emit_error(
        "flag_not_applicable_for_runtime",
        flag=flag, runtime=runtime, hint=hint,
    )


def _validate_args(args: argparse.Namespace) -> int | None:
    """Validate flag/runtime match. Returns None on success or exit code.

    Path flags use sentinel ``None`` defaults so we can detect "user
    explicitly passed X under the wrong runtime". The runtime-correct
    defaults are filled in here.
    """
    runtime = args.runtime
    for attr, (flag, owner_name, allowed) in _FLAG_OWNER.items():
        if runtime not in allowed and getattr(args, attr, None) is not None:
            return _reject(
                flag, runtime,
                f"{flag} is {owner_name}-only; omit it under --runtime {runtime}",
            )
    if runtime == "claude":
        if args.settings_file is None:
            args.settings_file = default_claude_settings()
    elif runtime == "codex":
        if args.hooks_file is None:
            args.hooks_file = default_codex_hooks()
        if args.config_file is None:
            args.config_file = default_codex_config()
    elif runtime == "antigravity":
        if args.antigravity_hooks is None:
            args.antigravity_hooks = default_antigravity_hooks()
    return None


# ---------------------------------------------------------------------------
# Shared verifier invocation
# ---------------------------------------------------------------------------

def _run_verifier(args: argparse.Namespace, *, verify_argv_extra: list[str]) -> int:
    """Invoke ``verify_install.py --runtime <args.runtime> ... --json``.

    Centralises the launch + stream-forward + strict-verify branching
    shared by all three install branches.
    """
    verify_path = Path(__file__).resolve().parent / "verify_install.py"
    if not verify_path.is_file():
        if args.strict_verify:
            return _emit_error(
                "verify_missing",
                expected=str(verify_path),
                hint=("verify_install.py is missing from the install; "
                      "the install itself succeeded, but the verifier "
                      "could not run. Re-clone the skill or restore "
                      "verify_install.py."),
            )
        return 0
    print("\n--- verify_install.py ---")
    sys.stdout.flush()
    try:
        completed = subprocess.run(
            [sys.executable, str(verify_path),
             "--runtime", args.runtime,
             "--json",
             *verify_argv_extra],
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
               f"(exit {completed.returncode})")
        print(msg, file=sys.stderr)
        return completed.returncode if args.strict_verify else 0
    return 0


# ---------------------------------------------------------------------------
# Per-runtime branch functions
# ---------------------------------------------------------------------------

def install_claude(args: argparse.Namespace) -> int:
    """Install the Light RIP reminder into ``~/.claude/settings.json``."""
    settings_path: Path = args.settings_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse the existing config FIRST. If it is corrupt or
    # non-object, abort BEFORE touching any backup.
    existing, load_err = load_json_safe(settings_path)
    if load_err and load_err != "file_not_found":
        return _emit_error(
            "invalid_runtime_config", stage="load",
            settings_path=str(settings_path),
            detail=load_err,
            hint=("fix or remove the corrupt config first; do NOT pass "
                  "--force-backup because no good backup exists yet"),
        )
    if existing is None:
        existing = {}

    # Step 2: back up the (known-good) source.
    backup_path: Path | None = None
    try:
        backup_path = backup_file(settings_path, force=args.force_backup)
    except BackupCollisionError as exc:
        return _emit_error(
            "backup_collision",
            settings_path=str(settings_path),
            backup_path=exc.backup_path,
            hint=("pass --force-backup to overwrite, or rename the "
                  "existing backup (recovery paths printed in the "
                  "error key are NOT shell-escaped; quote them with "
                  "your shell of choice when copying)"),
        )
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="backup",
            settings_path=str(settings_path),
            detail=str(exc),
            hint=("check that the parent directory is writable; this "
                  "is a filesystem problem, not a same-day collision"),
        )

    # Step 3: upsert. Caller-provided group sets its own
    # metadata.hook_namespace so future upserts can dedupe it.
    upsert_light_rip(existing, build_claude_group(python_exe, hook_script))

    # Step 4: atomic write.
    try:
        atomic_write_json(settings_path, existing)
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="write",
            settings_path=str(settings_path),
            backup_path=str(backup_path) if backup_path else None,
            detail=str(exc),
            hint=("restore manually from the backup path above; the "
                  "reported paths are NOT shell-escaped — quote them "
                  "with your shell of choice when copying"),
        )

    # Step 5: success record BEFORE the verifier so the record
    # survives even if the verifier fails afterwards.
    print(json.dumps({
        "settings_path": str(settings_path),
        "hook": NAMESPACE,
        "backup_path": str(backup_path) if backup_path else None,
    }, ensure_ascii=True))
    sys.stdout.flush()

    # Step 6: verifier.
    return _run_verifier(
        args,
        verify_argv_extra=["--claude-settings", str(settings_path)],
    )


def install_codex(args: argparse.Namespace) -> int:
    """Install the Light RIP reminder into ``~/.codex/hooks.json`` AND
    enable ``[features] hooks = true`` in ``~/.codex/config.toml``."""
    hooks_path: Path = args.hooks_file.expanduser().resolve()
    config_path: Path = args.config_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse hooks.json FIRST. Abort before any backup if it
    # is corrupt.
    existing, load_err = load_json_safe(hooks_path)
    if load_err and load_err != "file_not_found":
        return _emit_error(
            "invalid_runtime_config", stage="load",
            hooks_path=str(hooks_path),
            detail=load_err,
            hint=("fix or remove the corrupt hooks.json first; do NOT "
                  "pass --force-backup because no good backup exists yet"),
        )
    if existing is None:
        existing = {}

    # Step 2: back up BOTH target files. Collision on either aborts
    # the whole install so a partial backup never looks good.
    hooks_bak: Path | None = None
    config_bak: Path | None = None
    try:
        hooks_bak = backup_file(hooks_path, force=args.force_backup)
    except BackupCollisionError as exc:
        return _emit_error(
            "backup_collision", target="hooks",
            hooks_path=str(hooks_path),
            backup_path=exc.backup_path,
            hint=("pass --force-backup to overwrite, or rename the "
                  "existing backup (recovery paths printed in the "
                  "error key are NOT shell-escaped; quote them with "
                  "your shell of choice when copying)"),
        )
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="backup", target="hooks",
            hooks_path=str(hooks_path),
            detail=str(exc),
            hint=("check that the parent directory is writable; this "
                  "is a filesystem problem, not a same-day collision"),
        )

    # Step 3: upsert + atomic write hooks.json.
    upsert_light_rip(existing, build_codex_group(python_exe, hook_script))
    try:
        atomic_write_json(hooks_path, existing)
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="write", target="hooks",
            hooks_path=str(hooks_path),
            config_path=str(config_path),
            hooks_backup=str(hooks_bak) if hooks_bak else None,
            config_backup=str(config_bak) if config_bak else None,
            detail=str(exc),
            hint=("restore manually from the hooks_backup path above; "
                  "the reported paths are NOT shell-escaped — quote "
                  "them with your shell of choice when copying"),
        )

    # Step 4: enable [features] hooks = true in config.toml.
    # Parse-before-backup; idempotent re-installs (hooks already true)
    # make no backup and no write — the file stays byte-equal.
    try:
        needs_write, parse_err = check_hooks_feature(config_path)
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="load", target="config",
            hooks_path=str(hooks_path),
            config_path=str(config_path),
            hooks_backup=str(hooks_bak) if hooks_bak else None,
            detail=str(exc),
            hint=("check that config.toml is readable; this is a "
                  "filesystem problem, not a config error"),
        )

    if parse_err:
        return _emit_error(
            "invalid_runtime_config", stage="load", target="config",
            hooks_path=str(hooks_path),
            config_path=str(config_path),
            hooks_backup=str(hooks_bak) if hooks_bak else None,
            detail=parse_err,
            hint=("fix or remove the corrupt config.toml first; do "
                  "NOT pass --force-backup because no good backup "
                  "exists yet"),
        )

    if needs_write:
        # Parse-before-backup: source has been validated; only now
        # do we create a recovery snapshot.
        try:
            config_bak = backup_file(config_path, force=args.force_backup)
        except BackupCollisionError as exc:
            return _emit_error(
                "backup_collision", target="config",
                hooks_path=str(hooks_path),
                config_path=str(config_path),
                backup_path=exc.backup_path,
                hooks_backup=str(hooks_bak) if hooks_bak else None,
                hint=("pass --force-backup to overwrite, or rename "
                      "the existing backup (recovery paths printed "
                      "in the error key are NOT shell-escaped; quote "
                      "them with your shell of choice when copying)"),
            )
        except OSError as exc:
            return _emit_error(
                "permission_or_io_error", stage="backup", target="config",
                hooks_path=str(hooks_path),
                config_path=str(config_path),
                hooks_backup=str(hooks_bak) if hooks_bak else None,
                detail=str(exc),
                hint=("check that the parent directory is writable; "
                      "this is a filesystem problem, not a same-day "
                      "collision"),
            )

        try:
            write_hooks_feature(config_path)
        except ConfigTomlError as exc:
            return _emit_error(
                "invalid_runtime_config", stage="write", target="config",
                hooks_path=str(hooks_path),
                config_path=str(config_path),
                hooks_backup=str(hooks_bak) if hooks_bak else None,
                config_backup=str(config_bak) if config_bak else None,
                detail=str(exc),
                hint=("install tomli_w with `pip install tomli-w`, "
                      "or fix the config.toml shape"),
            )
        except OSError as exc:
            return _emit_error(
                "permission_or_io_error", stage="write", target="config",
                hooks_path=str(hooks_path),
                config_path=str(config_path),
                hooks_backup=str(hooks_bak) if hooks_bak else None,
                config_backup=str(config_bak) if config_bak else None,
                detail=str(exc),
                hint=("restore manually from the config_backup path "
                      "above; the reported paths are NOT shell-"
                      "escaped — quote them with your shell of "
                      "choice when copying"),
            )

    # Step 5: success record BEFORE the verifier.
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

    # Step 6: verifier.
    return _run_verifier(
        args,
        verify_argv_extra=["--codex-hooks", str(hooks_path),
                           "--codex-config", str(config_path)],
    )


def install_antigravity(args: argparse.Namespace) -> int:
    """Install the Light RIP reminder into ``~/.gemini/config/hooks.json``."""
    hooks_path: Path = args.antigravity_hooks.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse-before-backup.
    existing, load_err = load_json_safe(hooks_path)
    if load_err and load_err != "file_not_found":
        return _emit_error(
            "invalid_runtime_config", stage="load",
            hooks_path=str(hooks_path),
            detail=load_err,
            hint=("fix or remove the corrupt hooks.json first; do NOT pass "
                  "--force-backup because no good backup exists yet"),
        )
    if existing is None:
        existing = {}

    # Step 2: back up the (known-good) source.
    backup_path: Path | None = None
    try:
        backup_path = backup_file(hooks_path, force=args.force_backup)
    except BackupCollisionError as exc:
        return _emit_error(
            "backup_collision",
            hooks_path=str(hooks_path),
            backup_path=exc.backup_path,
            hint=("pass --force-backup to overwrite, or rename the "
                  "existing backup (recovery paths printed in the "
                  "error key are NOT shell-escaped; quote them with "
                  "your shell of choice when copying)"),
        )
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="backup",
            hooks_path=str(hooks_path),
            detail=str(exc),
            hint=("check that the parent directory is writable; this "
                  "is a filesystem problem, not a same-day collision"),
        )

    # Step 3: upsert under top-level hook name NAMESPACE, preserving other hooks.
    existing[NAMESPACE] = build_antigravity_hook(python_exe, hook_script)

    # Step 4: atomic write.
    try:
        atomic_write_json(hooks_path, existing)
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="write",
            hooks_path=str(hooks_path),
            backup_path=str(backup_path) if backup_path else None,
            detail=str(exc),
            hint=("restore manually from the backup path above; the "
                  "reported paths are NOT shell-escaped — quote them "
                  "with your shell of choice when copying"),
        )

    # Step 5: success record BEFORE the verifier.
    print(json.dumps({
        "hooks_path": str(hooks_path),
        "hook": NAMESPACE,
        "backup_path": str(backup_path) if backup_path else None,
    }, ensure_ascii=True))
    sys.stdout.flush()

    # Step 6: verifier.
    return _run_verifier(
        args,
        verify_argv_extra=["--antigravity-hooks", str(hooks_path)],
    )


# Dispatch table.
INSTALLERS = {
    "claude":      install_claude,
    "codex":       install_codex,
    "antigravity": install_antigravity,
}


# ---------------------------------------------------------------------------
# Self-test runner
# ---------------------------------------------------------------------------

# Result tracking for the in-process self-test runner.
class _SelfTest:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed: list[tuple[str, str]] = []  # (contract, detail)
        self.failed: list[tuple[str, str]] = []  # (contract, detail)

    def _emit(self, label: str, ok: bool, detail: str) -> None:
        prefix = "PASS" if ok else "FAIL"
        if ok:
            print(f"{prefix}: {label}")
            self.passed.append((label, detail))
        else:
            print(f"{prefix}: {label} — {detail}")
            self.failed.append((label, detail))

    def assert_eq(self, contract: str, actual, expected) -> bool:
        ok = (actual == expected)
        detail = f"got {actual!r}, expected {expected!r}" if not ok else (
            f"{actual!r}"
        )
        self._emit(contract, ok, detail)
        return ok

    def assert_true(self, contract: str, cond: bool, detail: str = "") -> bool:
        self._emit(contract, bool(cond), detail if not cond else "")
        return bool(cond)

    def assert_raises(self, contract: str, exc_type, fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
        except exc_type:
            self._emit(contract, True, "")
            return True
        except Exception as exc:
            self._emit(contract, False, f"raised {type(exc).__name__}: {exc}")
            return False
        self._emit(contract, False, "no exception raised")
        return False

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}", file=sys.stderr)


def _run_subprocess(cmd: list[str], *, expect_rc: int = 0,
                    check_json_field: tuple[str, str | bool] | None = None,
                    ) -> tuple[int, str, str, dict | None]:
    """Run a subprocess and (optionally) parse its last stderr line as JSON.

    Returns ``(returncode, stdout, stderr, json_field_dict_or_None)``.
    ``check_json_field`` is a ``(key, expected_value)`` tuple; when
    provided, the last stderr line is parsed as JSON and the
    ``expected_value`` is compared against the value at ``key``.
    """
    completed = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
    )
    json_field: dict | None = None
    if check_json_field is not None:
        try:
            json_field = json.loads(
                completed.stderr.strip().splitlines()[-1]
            )
        except Exception:
            json_field = None
    return completed.returncode, completed.stdout, completed.stderr, json_field


def _self_test_installer_common(t: _SelfTest) -> None:
    """in-process checks for hooks/installer_common.py.

    No subprocess needed: the helper is importable in-process and
    fully exercised by direct calls.
    """
    t.log("load_json_safe: missing file")
    r, e = load_json_safe(Path(tempfile.gettempdir()) / "no-such-light-rip-file.json")
    t.assert_eq("installer_common.load_json_safe missing file",
                (r, e), (None, "file_not_found"))

    t.log("load_json_safe: array root")
    with tempfile.TemporaryDirectory() as td:
        arr = Path(td) / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        r, e = load_json_safe(arr)
        t.assert_eq("installer_common.load_json_safe non-object root",
                    (r, e), (None, "not_an_object"))

    t.log("load_json_safe: valid dict")
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.json"
        good.write_text('{"a": 1}', encoding="utf-8")
        r, e = load_json_safe(good)
        t.assert_eq("installer_common.load_json_safe valid dict",
                    e, "")
        t.assert_eq("installer_common.load_json_safe valid dict (parsed)",
                    isinstance(r, dict) and r.get("a") == 1, True)

    t.log("upsert_light_rip: dedup all matching entries")
    cfg = {"hooks": {"UserPromptSubmit": [
        {"metadata": {"hook_namespace": NAMESPACE}, "hooks": [{"old": 1}]},
        {"metadata": {"hook_namespace": NAMESPACE}, "hooks": [{"old": 2}]},
        {"metadata": {"hook_namespace": NAMESPACE}, "hooks": [{"old": 3}]},
    ]}}
    new_group = {"metadata": {"hook_namespace": NAMESPACE},
                 "hooks": [{"new": True}]}
    upsert_light_rip(cfg, new_group)
    ups_list = cfg["hooks"]["UserPromptSubmit"]
    t.assert_eq("upsert_light_rip dedups ALL matching entries (count=1)",
                len(ups_list), 1)
    t.assert_eq("upsert_light_rip replaces content with new group",
                ups_list[0]["hooks"], [{"new": True}])

    t.log("upsert_light_rip: preserves non-matching entries")
    cfg2 = {"hooks": {"UserPromptSubmit": [
        {"metadata": {"hook_namespace": "other-tool"},
         "hooks": [{"keep": True}]},
        {"metadata": {"hook_namespace": NAMESPACE},
         "hooks": [{"old": True}]},
    ]}}
    upsert_light_rip(cfg2, new_group)
    ups_list2 = cfg2["hooks"]["UserPromptSubmit"]
    t.assert_eq("upsert_light_rip preserves non-matching (count=2)",
                len(ups_list2), 2)
    t.assert_eq("upsert_light_rip keeps other-tool entry first",
                ups_list2[0]["metadata"]["hook_namespace"], "other-tool")
    t.assert_eq("upsert_light_rip ours appended last",
                ups_list2[1]["metadata"]["hook_namespace"], NAMESPACE)


def _self_test_parse_before_backup(t: _SelfTest, hooks_dir: Path) -> None:
    """install on a corrupt Claude settings.json -> exit 2, error=invalid_runtime_config,
    no .bak-* created (parse-before-backup contract)."""
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-parse-") as td:
        corrupt = Path(td) / "settings.json"
        corrupt.write_text("{ this is not valid json", encoding="utf-8")
        baks_before = list(Path(td).glob("settings.json.bak-*"))
        rc, _, _, j = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "claude",
             "--settings-file", str(corrupt),
             "--no-strict-verify"],
            expect_rc=2,
            check_json_field=("error", "invalid_runtime_config"),
        )
        t.assert_eq("parse-before-backup corrupt returns exit 2", rc, 2)
        if j is not None:
            t.assert_eq("parse-before-backup error=invalid_runtime_config",
                        j.get("error"), "invalid_runtime_config")
        baks_after = list(Path(td).glob("settings.json.bak-*"))
        t.assert_eq("parse-before-backup no .bak-YYYY-MM-DD created",
                    len(baks_after), len(baks_before))


def _self_test_backup_collision(t: _SelfTest, hooks_dir: Path) -> None:
    """install twice on same day without --force-backup -> exit 2 + error=backup_collision on the second run."""
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-collision-") as td:
        col_target = Path(td) / "settings.json"
        col_target.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": []}}),
            encoding="utf-8",
        )
        # First install creates a backup.
        rc1, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "claude",
             "--settings-file", str(col_target),
             "--no-strict-verify"],
        )
        t.assert_eq("backup-collision: first install ok", rc1, 0)
        baks = list(Path(td).glob("settings.json.bak-*"))
        t.assert_true("backup-collision: first install created .bak-*",
                      len(baks) >= 1,
                      f"got {len(baks)}")
        # Second install without --force-backup -> collision.
        rc2, _, _, j2 = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "claude",
             "--settings-file", str(col_target),
             "--no-strict-verify"],
            expect_rc=2,
            check_json_field=("error", "backup_collision"),
        )
        t.assert_eq("backup-collision: second install returns 2", rc2, 2)
        if j2 is not None:
            t.assert_eq("backup-collision: error=backup_collision",
                        j2.get("error"), "backup_collision")
            t.assert_true("backup-collision: backup_path present for decision",
                          "backup_path" in j2,
                          f"keys: {sorted(j2.keys())}")


def _self_test_codex_toml_byte_equal(t: _SelfTest, hooks_dir: Path) -> None:
    """Codex TOML byte-equal preservation (PR-D core).

    install -> install again -> second install is byte-equal,
    no new backup, [mcp]/[plugins] preserved.
    Also covers hooks = "true" (quoted-string form preserved).
    """
    # Variant A: hooks = true (TOML bool form) — must be byte-equal on re-install.
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-prd-bool-") as td:
        cfg1 = Path(td) / "config.toml"
        cfg1_original = (
            "# my custom settings\n"
            "[features]\n"
            "hooks = true  # already enabled\n"
            "[mcp]\n"
            'servers = ["foo"]\n'
        )
        cfg1.write_text(cfg1_original, encoding="utf-8")
        hooks1 = Path(td) / "hooks.json"
        hooks1.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        # First install (writes hooks.json + already-true config.toml = no-op).
        rc1, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "codex",
             "--hooks-file", str(hooks1),
             "--config-file", str(cfg1),
             "--no-strict-verify"],
        )
        t.assert_eq("PR-D bool: first install ok", rc1, 0)
        t.assert_eq("PR-D bool: re-install leaves config.toml byte-equal",
                    cfg1.read_text(encoding="utf-8"), cfg1_original)
        baks = list(Path(td).glob("config.toml.bak-*"))
        t.assert_eq("PR-D bool: re-install creates no new backup",
                    len(baks), 0)

        # Second install is the actual re-install: still no backup,
        # file still byte-equal. (Use --force-backup so the hooks.json
        # backup-collision doesn't shadow the config.toml invariant we
        # are actually testing — note that hooks.json IS rewritten each
        # install; what we care about is that config.toml stays byte-
        # equal and produces no new backup of its own.)
        rc2, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "codex",
             "--hooks-file", str(hooks1),
             "--config-file", str(cfg1),
             "--force-backup",
             "--no-strict-verify"],
        )
        t.assert_eq("PR-D bool: second install ok", rc2, 0)
        baks2 = list(Path(td).glob("config.toml.bak-*"))
        t.assert_eq("PR-D bool: after re-install still no backup",
                    len(baks2), 0)
        t.assert_eq("PR-D bool: config.toml still byte-equal after re-install",
                    cfg1.read_text(encoding="utf-8"), cfg1_original)

        # Variant B: hooks = false -> rewrite to true -> [mcp]/[plugins]
        # sections preserved.
        cfg2 = Path(td) / "rewrite.toml"
        cfg2_original = (
            "# header comment\n"
            "[features]\n"
            "goals = true\n"
            "hooks = false\n"
            "[mcp]\n"
            'servers = ["foo", "bar"]\n'
            "[plugins]\n"
            'name = "x"\n'
        )
        cfg2.write_text(cfg2_original, encoding="utf-8")
        hooks2 = Path(td) / "rewrite-hooks.json"
        hooks2.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        rc3, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "codex",
             "--hooks-file", str(hooks2),
             "--config-file", str(cfg2),
             "--no-strict-verify"],
        )
        t.assert_eq("PR-D false->true: install ok", rc3, 0)
        # Read back, check mcp/plugins preserved.
        if tomllib is not None:
            with open(cfg2, "rb") as f:
                data = tomllib.load(f)
            t.assert_true("PR-D [mcp] section preserved after rewrite",
                          isinstance(data.get("mcp"), dict)
                          and data["mcp"].get("servers") == ["foo", "bar"],
                          f"mcp={data.get('mcp')!r}")
            t.assert_true("PR-D [plugins] section preserved after rewrite",
                          isinstance(data.get("plugins"), dict)
                          and data["plugins"].get("name") == "x",
                          f"plugins={data.get('plugins')!r}")
            t.assert_eq("PR-D rewrite flips features.hooks to True",
                        data.get("features", {}).get("hooks"), True)

    # Variant C: hooks = "true" (quoted string) -> idempotent byte-equal.
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-prd-quoted-") as td:
        cfg4 = Path(td) / "quoted.toml"
        cfg4.write_text('[features]\nhooks = "true"\n', encoding="utf-8")
        hooks4 = Path(td) / "quoted-hooks.json"
        hooks4.write_text(
            json.dumps({
                "hooks": {
                    "UserPromptSubmit": [{
                        "metadata": {"hook_namespace": NAMESPACE},
                        "hooks": [{"type": "command", "command": "echo"}],
                    }]
                }
            }),
            encoding="utf-8",
        )
        # First install on the quoted-string config: should be no-op.
        rc4, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "codex",
             "--hooks-file", str(hooks4),
             "--config-file", str(cfg4),
             "--no-strict-verify"],
        )
        t.assert_eq("PR-D quoted: install ok", rc4, 0)
        # File should still contain `hooks = "true"` byte-equal.
        t.assert_true(
            "PR-D hooks = \"true\" preserved byte-equal",
            'hooks = "true"' in cfg4.read_text(encoding="utf-8-sig"),
            f"file={cfg4.read_text(encoding='utf-8-sig')!r}",
        )
        # Second install: still byte-equal. (See note above re
        # --force-backup: hooks.json gets re-written each install and
        # would otherwise collide on the same-day backup; what we test
        # here is config.toml byte-equality.)
        original_quoted = cfg4.read_text(encoding="utf-8-sig")
        rc5, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "codex",
             "--hooks-file", str(hooks4),
             "--config-file", str(cfg4),
             "--force-backup",
             "--no-strict-verify"],
        )
        t.assert_eq("PR-D quoted: re-install ok", rc5, 0)
        t.assert_eq("PR-D quoted: byte-equal after re-install",
                    cfg4.read_text(encoding="utf-8-sig"), original_quoted)


def _self_test_antigravity_detector(t: _SelfTest, hooks_dir: Path) -> None:
    """Antigravity detector edge cases and envelope format checks."""
    verify = hooks_dir / "verify_install.py"
    if not verify.is_file():
        t.assert_true("antigravity-detector: verify_install.py present", False, "missing")
        return
    sys.path.insert(0, str(hooks_dir))
    from verify_install import check_antigravity  # type: ignore[import-not-found]
    sys.path.pop(0)

    script_path = hooks_dir / "light_rip_reminder.py"
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-agy-") as td_str:
        td = Path(td_str)

        disabled_cfg = td / "disabled.json"
        disabled_cfg.write_text(json.dumps({
            NAMESPACE: {
                "enabled": False,
                "PreInvocation": [{"type": "command", "command": f'"{sys.executable}" "{script_path}" --format antigravity'}],
            }
        }), encoding="utf-8")
        t.assert_eq("antigravity: enabled=false -> installed=False",
                    check_antigravity(disabled_cfg).get("installed"), False)

        missing_fmt_cfg = td / "missing-fmt.json"
        missing_fmt_cfg.write_text(json.dumps({
            NAMESPACE: {
                "enabled": True,
                "PreInvocation": [{"type": "command", "command": f'"{sys.executable}" "{script_path}"'}],
            }
        }), encoding="utf-8")
        t.assert_eq("antigravity: missing --format antigravity -> match_count=0",
                    check_antigravity(missing_fmt_cfg).get("match_count"), 0)

        wrong_type_cfg = td / "wrong-type.json"
        wrong_type_cfg.write_text(json.dumps({
            NAMESPACE: {
                "enabled": True,
                "PreInvocation": [{"type": "http", "command": f'"{sys.executable}" "{script_path}" --format antigravity'}],
            }
        }), encoding="utf-8")
        t.assert_eq("antigravity: type=http -> match_count=0",
                    check_antigravity(wrong_type_cfg).get("match_count"), 0)

        good_cfg = td / "good.json"
        good_cfg.write_text(json.dumps({
            NAMESPACE: {
                "PreInvocation": [{"command": f'"{sys.executable}" "{script_path}" --format antigravity'}],
            }
        }), encoding="utf-8")
        t.assert_eq("antigravity: valid minimal entry -> installed=True",
                    check_antigravity(good_cfg).get("installed"), True)

        # Reminder script emits valid Antigravity PreInvocation injectSteps JSON.
        completed = subprocess.run(
            [sys.executable, str(script_path), "--format", "antigravity"],
            input='{"hookEventName":"PreInvocation","invocationNum":1}',
            capture_output=True, text=True, check=False,
        )
        t.assert_eq("antigravity reminder rc=0", completed.returncode, 0)
        parsed = json.loads(completed.stdout)
        steps = parsed.get("injectSteps", [])
        msg = steps[0].get("systemMessage", {}).get("systemMessage", "") if steps else ""
        t.assert_true("antigravity reminder emits injectSteps.systemMessage.systemMessage",
                      "Evidence Before Claims" in msg, f"got {parsed!r}")
        t.assert_eq("antigravity reminder emits terminationBehavior=''",
                    parsed.get("terminationBehavior"), "")


def _self_test_cross_runtime_flag(t: _SelfTest, hooks_dir: Path) -> None:
    """Cross-runtime flag rejection: passing a runtime-specific flag
    under the wrong --runtime must abort with exit 2 and
    error=flag_not_applicable_for_runtime.
    """
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-flag-") as td:
        settings = Path(td) / "settings.json"
        rc, _, _, j = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install",
             "--runtime", "claude",
             "--hooks-file", str(settings),
             "--no-strict-verify"],
            expect_rc=2,
            check_json_field=("error", "flag_not_applicable_for_runtime"),
        )
        t.assert_eq("cross-runtime flag: --hooks-file under claude exits 2",
                    rc, 2)
        if j is not None:
            t.assert_eq("cross-runtime flag: error=flag_not_applicable_for_runtime",
                        j.get("error"), "flag_not_applicable_for_runtime")
            t.assert_eq("cross-runtime flag: names offending flag",
                        j.get("flag"), "--hooks-file")


def _self_test_install_verify_roundtrip(t: _SelfTest, hooks_dir: Path) -> None:
    """Close the loop: installer writes -> verifier agrees."""
    sys.path.insert(0, str(hooks_dir))
    from verify_install import check_antigravity, check_claude, check_codex
    sys.path.pop(0)

    with tempfile.TemporaryDirectory(prefix="light-rip-roundtrip-") as td:
        td = Path(td)

        # --- Claude round-trip ---
        claude_settings = td / "claude.json"
        rc, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install", "--runtime", "claude",
             "--settings-file", str(claude_settings),
             "--no-strict-verify"],
        )
        t.assert_eq("claude round-trip: install rc", rc, 0)
        result = check_claude(claude_settings)
        t.assert_eq("claude round-trip: verifier installed=True",
                    result.get("installed"), True)
        t.assert_eq("claude round-trip: verifier match_count=1",
                    result.get("match_count"), 1)

        # --- Codex round-trip ---
        codex_hooks = td / "codex-hooks.json"
        codex_config = td / "codex-config.toml"
        rc, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install", "--runtime", "codex",
             "--hooks-file", str(codex_hooks),
             "--config-file", str(codex_config),
             "--no-strict-verify"],
        )
        t.assert_eq("codex round-trip: install rc", rc, 0)
        result = check_codex(codex_hooks, codex_config)
        t.assert_eq("codex round-trip: verifier installed=True",
                    result.get("installed"), True)
        t.assert_eq("codex round-trip: verifier match_count=1",
                    result.get("match_count"), 1)

        # --- Antigravity round-trip (preserving unrelated existing hooks) ---
        agy_hooks = td / "antigravity-hooks.json"
        agy_hooks.write_text(json.dumps({
            "other-hook": {"Stop": [{"type": "command", "command": "echo done"}]}
        }), encoding="utf-8")
        rc, _, _, _ = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_harness.py"),
             "install", "--runtime", "antigravity",
             "--antigravity-hooks", str(agy_hooks),
             "--no-strict-verify"],
        )
        t.assert_eq("antigravity round-trip: install rc", rc, 0)
        result = check_antigravity(agy_hooks)
        t.assert_eq("antigravity round-trip: verifier installed=True",
                    result.get("installed"), True)
        t.assert_eq("antigravity round-trip: verifier match_count=1",
                    result.get("match_count"), 1)
        written = json.loads(agy_hooks.read_text(encoding="utf-8"))
        t.assert_true("antigravity round-trip: preserves other-hook",
                      "other-hook" in written, f"keys={list(written.keys())}")
        t.assert_true("antigravity round-trip: writes PreInvocation list",
                      isinstance(written.get(NAMESPACE, {}).get("PreInvocation"), list),
                      f"entry={written.get(NAMESPACE)!r}")


def run_self_test(args: argparse.Namespace) -> int:
    """Run the in-process self-test covering P0 contracts."""
    verbose = bool(getattr(args, "verbose", False))
    t = _SelfTest(verbose=verbose)

    hooks_dir = Path(__file__).resolve().parent

    try:
        print("[installer_common]")
        _self_test_installer_common(t)
        print()
        print("[parse-before-backup]")
        _self_test_parse_before_backup(t, hooks_dir)
        print()
        print("[backup-collision]")
        _self_test_backup_collision(t, hooks_dir)
        print()
        print("[PR-D: codex config.toml byte-equal preservation]")
        _self_test_codex_toml_byte_equal(t, hooks_dir)
        print()
        print("[antigravity detector & format]")
        _self_test_antigravity_detector(t, hooks_dir)
        print()
        print("[cross-runtime flag rejection]")
        _self_test_cross_runtime_flag(t, hooks_dir)
        print()
        print("[install+verify round-trip]")
        _self_test_install_verify_roundtrip(t, hooks_dir)
    except Exception as exc:
        print(f"FAILED: self-test setup error — {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    total = len(t.passed) + len(t.failed)
    if t.failed:
        print(f"FAILED: {len(t.failed)}/{total} passed")
        return 1
    print(f"OK: {len(t.passed)}/{total} passed")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Light RIP reminder hook on a supported "
                    "agent runtime (Claude Code, Codex, or Antigravity). "
                    "Pick one with --runtime.",
    )
    subparsers = parser.add_subparsers(dest="action")

    # ----- install subparser -----
    install_p = subparsers.add_parser(
        "install",
        help="Install the Light RIP reminder hook into a runtime config.",
    )
    install_p.add_argument(
        "--runtime",
        choices=["claude", "codex", "antigravity"],
        required=True,
        help="Which agent runtime to install the Light RIP reminder hook into.",
    )

    # ----- Claude-specific (only meaningful under --runtime claude) -----
    install_p.add_argument(
        "--settings-file", type=Path, default=None,
        help="(claude) Path to Claude Code settings.json. "
             "Default: ~/.claude/settings.json.",
    )

    # ----- Codex-specific (only meaningful under --runtime codex) -----
    install_p.add_argument(
        "--hooks-file", type=Path, default=None,
        help="(codex) Path to Codex hooks.json. Default: ~/.codex/hooks.json.",
    )
    install_p.add_argument(
        "--config-file", type=Path, default=None,
        help="(codex) Path to Codex config.toml. Default: ~/.codex/config.toml.",
    )

    # ----- Antigravity-specific (only meaningful under --runtime antigravity) -----
    install_p.add_argument(
        "--antigravity-hooks", type=Path, default=None,
        help="(antigravity) Path to Antigravity hooks.json. Default: "
             "~/.gemini/config/hooks.json.",
    )

    # ----- Shared -----
    install_p.add_argument(
        "--force-backup", action="store_true", default=False,
        help="Overwrite an existing <path>.bak-YYYY-MM-DD backup instead of "
             "aborting with error=backup_collision.",
    )
    verify_group = install_p.add_mutually_exclusive_group()
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

    # ----- self-test subparser -----
    self_test_p = subparsers.add_parser(
        "self-test",
        help="Run in-process P0 contract tests in a tempdir; never touches "
             "real ~/<runtime> configs.",
    )
    self_test_p.add_argument(
        "--verbose", action="store_true", default=False,
        help="Print verbose detail to stderr.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.action == "self-test":
        return run_self_test(args)

    # action defaults to None when no subcommand is given; in that
    # case argparse should already have errored. We keep a defensive
    # guard so ``python install_hook_based_harness.py --help`` works
    # but a bare invocation is a usage error.
    if args.action is None or args.action == "install":
        # When called bare as ``install_hook_based_harness.py --runtime ...``
        # (no subcommand), argparse assigns None; for back-compat we
        # accept that path as an implicit install.
        if not hasattr(args, "runtime"):
            parser.error("missing subcommand: use 'install' or 'self-test'")
        rc = _validate_args(args)
        if rc is not None:
            return rc
        installer = INSTALLERS[args.runtime]
        return installer(args)

    parser.error(f"unknown action: {args.action}")
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())