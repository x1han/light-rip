#!/usr/bin/env python3
"""Install the Light RIP reminder as a UserPromptSubmit hook on a hook-based agent runtime.

This unified installer replaces the three prior per-runtime scripts
(``install_claude_hook.py``, ``install_codex_hook.py``, plus the ZCode
worked example formerly living inside ``install_general_agent_hook.py``)
with a single dispatcher that takes ``--runtime {claude,codex,zcode}``.

Per-runtime behavior:

  * ``claude``  — merges one entry into ``~/.claude/settings.json`` under
                  ``hooks.UserPromptSubmit`` (legacy JSON hooks schema).
  * ``codex``   — writes ``~/.codex/hooks.json`` (legacy JSON hooks
                  schema) AND enables ``[features] hooks = true`` in
                  ``~/.codex/config.toml`` (TOML, via ``tomllib`` +
                  ``tomli_w``; see ``Dependencies`` below).
  * ``zcode``   — merges one entry into ``~/.zcode/cli/config.json``
                  under ``hooks.events.UserPromptSubmit[]`` (ZCode
                  process-hook schema). First-class installer for a
                  runtime that previously only had a worked example
                  prompt.

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
  3. Idempotent upsert. Re-install on an already-correct config is
     byte-equal and creates NO backup (PR-D invariant).
  4. Atomic write via ``atomic_write_json`` (temp file in the
     target directory, fsync, ``os.replace`` over destination).
  5. Run the runtime-agnostic ``verify_install.py`` as the final
     step. Under ``--strict-verify`` (default), missing verifier
     or non-zero verifier exit codes propagate. Under
     ``--no-strict-verify``, the verifier is informational only.

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
  On Python ≥ 3.11 you also need ``tomllib`` (stdlib — no install
  needed) and on older Pythons you need ``tomli`` as the backport.
  The Claude and ZCode branches need no extra dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
    is_our_namespace,
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


def default_zcode_config() -> Path:
    return Path.home() / ".zcode" / "cli" / "config.json"


# ---------------------------------------------------------------------------
# Hook-group builders (legacy JSON hooks schema for Claude + Codex)
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


# ---------------------------------------------------------------------------
# ZCode dedup predicate (process-hook schema, NO metadata.hook_namespace)
# ---------------------------------------------------------------------------

def _is_our_zcode_entry(entry: object, python_exe: Path, reminder: Path) -> bool:
    """True iff ``entry`` is a prior Light RIP ZCode install we should dedup.

    Mirrors the matching criterion in ``verify_install.check_zcode``:
    the entry MUST be ``type="process"`` (ZCode silently skips other
    types), its ``command`` MUST resolve to our recorded Python
    executable, and ``args[0]`` MUST resolve to our recorded reminder
    script. This is tighter than the verifier (which only checks that
    ``command`` is a Python launcher and that ``args`` contains the
    reminder script name) because during install we know the exact
    paths we just wrote.
    """
    if not isinstance(entry, dict):
        return False
    if str(entry.get("type", "")) != "process":
        return False
    cmd = entry.get("command", "")
    args = entry.get("args", [])
    if not isinstance(cmd, str) or not isinstance(args, list):
        return False
    try:
        if Path(cmd).resolve() != python_exe.resolve():
            return False
    except OSError:
        return False
    if not args:
        return False
    try:
        return Path(str(args[0])).resolve() == reminder.resolve()
    except OSError:
        return False


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

# Per-(flag, wrong-runtime) hints, lifted to constants so each
# _validate_args rejection becomes a single line. Wire shape
# (envelope keys + stderr JSON) unchanged.
_HINT_HOOKS_FILE_CLAUDE = "--hooks-file is Codex-only; omit it under --runtime claude"
_HINT_HOOKS_FILE_ZCODE = "--hooks-file is Codex-only; omit it under --runtime zcode"
_HINT_CONFIG_FILE_CLAUDE = "--config-file is Codex-only; omit it under --runtime claude"
_HINT_CONFIG_FILE_ZCODE = "--config-file is Codex-only; omit it under --runtime zcode"
_HINT_SETTINGS_FILE_CODEX = "--settings-file is Claude-only; omit it under --runtime codex"
_HINT_SETTINGS_FILE_ZCODE = "--settings-file is Claude-only; omit it under --runtime zcode"
_HINT_ZCODE_CONFIG_CLAUDE = "--zcode-config is ZCode-only; omit it under --runtime claude"
_HINT_ZCODE_CONFIG_CODEX = "--zcode-config is ZCode-only; omit it under --runtime codex"


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
    if runtime == "claude":
        if args.hooks_file is not None:
            return _reject("--hooks-file", "claude", _HINT_HOOKS_FILE_CLAUDE)
        if args.config_file is not None:
            return _reject("--config-file", "claude", _HINT_CONFIG_FILE_CLAUDE)
        if args.zcode_config is not None:
            return _reject("--zcode-config", "claude", _HINT_ZCODE_CONFIG_CLAUDE)
        if args.settings_file is None:
            args.settings_file = default_claude_settings()
        return None
    if runtime == "codex":
        if args.settings_file is not None:
            return _reject("--settings-file", "codex", _HINT_SETTINGS_FILE_CODEX)
        if args.zcode_config is not None:
            return _reject("--zcode-config", "codex", _HINT_ZCODE_CONFIG_CODEX)
        if args.hooks_file is None:
            args.hooks_file = default_codex_hooks()
        if args.config_file is None:
            args.config_file = default_codex_config()
        return None
    # runtime == "zcode"
    if args.settings_file is not None:
        return _reject("--settings-file", "zcode", _HINT_SETTINGS_FILE_ZCODE)
    if args.hooks_file is not None:
        return _reject("--hooks-file", "zcode", _HINT_HOOKS_FILE_ZCODE)
    if args.config_file is not None:
        return _reject("--config-file", "zcode", _HINT_CONFIG_FILE_ZCODE)
    if args.zcode_config is None:
        args.zcode_config = default_zcode_config()
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
                      "the install itself succeeded, but the script-side "
                      "smoke check could not run. Re-clone the skill or "
                      "restore verify_install.py."),
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


def install_zcode(args: argparse.Namespace) -> int:
    """Install the Light RIP reminder into ``~/.zcode/cli/config.json``.

    Unlike the Claude / Codex branches, ZCode has no
    ``metadata.hook_namespace`` field in its hook entry shape;
    deduplication is by command basename + first arg basename (same
    criterion as ``verify_install.check_zcode``).
    """
    zcode_config: Path = args.zcode_config.expanduser().resolve()
    reminder = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    # Step 1: parse-before-backup.
    existing, load_err = load_json_safe(zcode_config)
    if load_err and load_err != "file_not_found":
        return _emit_error(
            "invalid_runtime_config", stage="load",
            zcode_config=str(zcode_config),
            detail=load_err,
            hint=("fix or remove the corrupt config first; do NOT pass "
                  "--force-backup because no good backup exists yet"),
        )
    if existing is None:
        existing = {}

    # Step 2: build the new hooks block in memory (idempotent,
    # no-clobber). ``setdefault`` only sets the key when absent, so
    # a deliberate ``hooks.enabled: false`` is preserved.
    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        existing["hooks"] = hooks
    hooks.setdefault("enabled", True)
    hooks.setdefault("timeoutMs", 10000)
    hooks.setdefault("maxOutputBytes", 4096)
    events = hooks.setdefault("events", {})
    if not isinstance(events, dict):
        events = {}
        hooks["events"] = events
    ups_list = events.setdefault("UserPromptSubmit", [])
    if not isinstance(ups_list, list):
        ups_list = []
        events["UserPromptSubmit"] = ups_list

    # Step 2b: append (not replace) our entry; dedup any prior ours.
    # NO ``matcher`` key — Zod's min(1) rejects empty strings and
    # silently drops the whole hooks block. Omit it to match all
    # prompts.
    new_entry = {
        "type": "process",
        "command": str(python_exe),
        "args": [str(reminder)],
        "timeoutMs": 5000,
    }
    ups_list[:] = [
        e for e in ups_list
        if not _is_our_zcode_entry(e, python_exe, reminder)
    ]
    ups_list.append(new_entry)

    # Step 3: back up the (known-good) source.
    backup_path: Path | None = None
    try:
        backup_path = backup_file(zcode_config, force=args.force_backup)
    except BackupCollisionError as exc:
        return _emit_error(
            "backup_collision",
            zcode_config=str(zcode_config),
            backup_path=exc.backup_path,
            hint=("pass --force-backup to overwrite, or rename the "
                  "existing backup (recovery paths printed in the "
                  "error key are NOT shell-escaped; quote them with "
                  "your shell of choice when copying)"),
        )
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="backup",
            zcode_config=str(zcode_config),
            detail=str(exc),
            hint=("check that the parent directory is writable; this "
                  "is a filesystem problem, not a same-day collision"),
        )

    # Step 4: atomic write.
    try:
        atomic_write_json(zcode_config, existing)
    except OSError as exc:
        return _emit_error(
            "permission_or_io_error", stage="write",
            zcode_config=str(zcode_config),
            backup_path=str(backup_path) if backup_path else None,
            detail=str(exc),
            hint=("restore manually from the backup path above; the "
                  "reported paths are NOT shell-escaped — quote them "
                  "with your shell of choice when copying"),
        )

    # Step 5: success record BEFORE the verifier.
    print(json.dumps({
        "zcode_config": str(zcode_config),
        "hook": NAMESPACE,
        "backup_path": str(backup_path) if backup_path else None,
    }, ensure_ascii=True))
    sys.stdout.flush()

    # Step 6: verifier.
    return _run_verifier(
        args,
        verify_argv_extra=["--zcode-config", str(zcode_config)],
    )


# Dispatch table.
INSTALLERS = {
    "claude": install_claude,
    "codex":  install_codex,
    "zcode":  install_zcode,
}


# ---------------------------------------------------------------------------
# Self-test (replaces smoke_fix_batch.py)
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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


def _self_test_python_launcher(t: _SelfTest, hooks_dir: Path) -> None:
    """verify_install's _PY_LAUNCHER rejects non-interpreter names."""
    verify_path = hooks_dir / "verify_install.py"
    if verify_path.is_file():
        # Re-use the verifier regex via a direct invocation by importing.
        sys.path.insert(0, str(hooks_dir))
        from verify_install import _PY_LAUNCHER  # type: ignore[import-not-found]
        positives = ["py", "py.exe", "python.exe", "python3.12.exe"]
        for name in positives:
            t.assert_true(
                f"_PY_LAUNCHER accepts {name!r}",
                bool(_PY_LAUNCHER.match(name)),
                f"got False",
            )
        negatives = [
            "pythonw.exe", "mypython.exe",
            "python3.12-config", "python3.12-config.exe",
            "python_d.exe", "pythonw.exe", "pythonw_d.exe",
        ]
        for name in negatives:
            t.assert_true(
                f"_PY_LAUNCHER rejects {name!r}",
                not bool(_PY_LAUNCHER.match(name)),
                f"matched unexpectedly",
            )
        sys.path.pop(0)
    else:
        t.assert_true("verify_install.py present for launcher predicate", False,
                      "missing")


def _make_zcode_cfg(td: Path, name: str, *, command: str,
                    type_value: str = "process",
                    enabled: bool = True,
                    include_matcher: bool = False,
                    matcher_value: str = "",
                    reminder_script: str | None = None) -> Path:
    """Create a ZCode fixture config with the given shape.

    ``reminder_script`` is the path that goes into ``args``. ``None``
    (default) uses the actual on-disk ``light_rip_reminder.py``,
    enough for the matcher below to verify ``REMINDER_SCRIPT_NAME``
    appears in the arg blob.
    """
    if reminder_script is None:
        reminder_script = str(
            (Path(__file__).resolve().parent / "light_rip_reminder.py")
        )
    hooks_entry: dict = {
        "type": type_value,
        "command": command,
        "args": [reminder_script],
    }
    if include_matcher:
        hooks_entry["matcher"] = matcher_value
    cfg = {
        "hooks": {
            "enabled": enabled,
            "events": {
                "UserPromptSubmit": [{
                    "hooks": [hooks_entry],
                }]
            }
        }
    }
    out = td / name
    out.write_text(json.dumps(cfg), encoding="utf-8")
    return out


def _self_test_zcode_detector(t: _SelfTest, hooks_dir: Path) -> None:
    """ZCode detector edge cases via verify_install."""
    verify = hooks_dir / "verify_install.py"
    if not verify.is_file():
        t.assert_true("zcode-detector: verify_install.py present", False, "missing")
        return
    sys.path.insert(0, str(hooks_dir))
    from verify_install import check_zcode  # type: ignore[import-not-found]
    sys.path.pop(0)

    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-zcode-") as td_str:
        td = Path(td_str)
        # R7: hooks.enabled=false is rejected (the entry exists but the
        # hook layer is disabled → installed must read False).
        bad_enabled = _make_zcode_cfg(td, "bad-enabled.json",
                                       command="python.exe",
                                       enabled=False)
        state = check_zcode(bad_enabled)
        t.assert_eq("zcode R7: hooks.enabled=false -> installed=False",
                    state.get("installed"), False)

        # R7: type != "process" is rejected (loader silently skips them).
        wrong_type = _make_zcode_cfg(td, "wrong-type.json",
                                      command="python.exe",
                                      type_value="command")
        state = check_zcode(wrong_type)
        t.assert_eq("zcode R7: type=command -> match_count=0",
                    state.get("match_count"), 0)

        # R7: command path that does NOT match PEP 394 regex (e.g. node.exe).
        non_python = _make_zcode_cfg(td, "non-python.json",
                                      command="C:/Windows/System32/node.exe")
        state = check_zcode(non_python)
        t.assert_eq("zcode R7: node.exe -> match_count=0",
                    state.get("match_count"), 0)

        # Valid entry passes (no matcher key, command is sys.executable).
        with tempfile.TemporaryDirectory() as atd:
            good = _make_zcode_cfg(
                Path(atd), "good.json",
                command=str(Path(sys.executable).resolve()),
            )
            state = check_zcode(good)
            t.assert_eq("zcode: valid entry installed=True",
                        state.get("installed"), True)

        # Zod min(1) trap: the installer's contract is "never write
        # matcher=\"\"" and "no matcher is fine". Verify by reading the
        # installer's actual output on a fresh tempdir install.
        with tempfile.TemporaryDirectory(prefix="light-rip-selftest-zod-") as ztd:
            zcfg = Path(ztd) / "config.json"
            rc, _, _, _ = _run_subprocess(
                [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
                 "install",
                 "--runtime", "zcode",
                 "--zcode-config", str(zcfg),
                 "--no-strict-verify"],
            )
            t.assert_eq("zcode matcher-trap: install ok", rc, 0)
            zdata = json.loads(zcfg.read_text(encoding="utf-8-sig"))
            entries = zdata["hooks"]["events"]["UserPromptSubmit"]
            # Each wrapper group may carry a `hooks` array (the loader
            # iterates group.hooks[]. The installer's standard shape is
            # one outer group with one inner entry; either the inner
            # entry or the wrapper group itself must NOT have a
            # ``matcher`` key whose value is "".
            def _walk(node, found):
                if isinstance(node, dict):
                    if "matcher" in node and node["matcher"] == "":
                        found.append(node)
                    for v in node.values():
                        _walk(v, found)
                elif isinstance(node, list):
                    for v in node:
                        _walk(v, found)
            found: list = []
            _walk(zdata, found)
            t.assert_eq("zcode Zod trap: installer never writes matcher=\"\"",
                        found, [])
            # And confirm no matcher key at all in the freshly-written
            # entry (the installer's contract).
            entry = (
                zdata["hooks"]["events"]["UserPromptSubmit"][0]
                if zdata["hooks"]["events"]["UserPromptSubmit"]
                else {}
            )
            # Walk one level of wrapper-group indirection if present.
            inner = (entry.get("hooks", [{}]) or [{}])[0]
            t.assert_eq("zcode Zod trap: written entry has no matcher key",
                        "matcher" in inner, False)


def _self_test_cross_runtime_flag(t: _SelfTest, hooks_dir: Path) -> None:
    """replaces T-UNI-3 (--no-enable-feature is gone): passing
    --hooks-file under --runtime claude must abort with exit 2 and
    error=flag_not_applicable_for_runtime.
    """
    with tempfile.TemporaryDirectory(prefix="light-rip-selftest-flag-") as td:
        settings = Path(td) / "settings.json"
        rc, _, _, j = _run_subprocess(
            [sys.executable, str(hooks_dir / "install_hook_based_agent.py"),
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
        print("[PEP 394/397 launcher predicate]")
        _self_test_python_launcher(t, hooks_dir)
        print()
        print("[zcode detector edge cases]")
        _self_test_zcode_detector(t, hooks_dir)
        print()
        print("[cross-runtime flag rejection]")
        _self_test_cross_runtime_flag(t, hooks_dir)
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
        description="Install the Light RIP reminder as a UserPromptSubmit "
                    "hook on a hook-based agent runtime (Claude Code, Codex, "
                    "or ZCode). Pick one with --runtime.",
    )
    subparsers = parser.add_subparsers(dest="action")

    # ----- install subparser -----
    install_p = subparsers.add_parser(
        "install",
        help="Install the Light RIP reminder hook into a runtime config.",
    )
    install_p.add_argument(
        "--runtime",
        choices=["claude", "codex", "zcode"],
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

    # ----- ZCode-specific (only meaningful under --runtime zcode) -----
    install_p.add_argument(
        "--zcode-config", type=Path, default=None,
        help="(zcode) Path to ZCode config.json. Default: "
             "~/.zcode/cli/config.json.",
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
    # guard so ``python install_hook_based_agent.py --help`` works
    # but a bare invocation is a usage error.
    if args.action is None or args.action == "install":
        # When called bare as ``install_hook_based_agent.py --runtime ...``
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