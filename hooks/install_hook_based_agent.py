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
import subprocess
import sys
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


def _validate_args(args: argparse.Namespace) -> int | None:
    """Validate flag/runtime match. Returns None on success or exit code.

    Path flags use sentinel ``None`` defaults so we can detect "user
    explicitly passed X under the wrong runtime". The runtime-correct
    defaults are filled in here.
    """
    if args.runtime == "claude":
        if args.hooks_file is not None:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--hooks-file", runtime="claude",
                hint=("--hooks-file is Codex-only; omit it under "
                      "--runtime claude"),
            )
        if args.config_file is not None:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--config-file", runtime="claude",
                hint=("--config-file is Codex-only; omit it under "
                      "--runtime claude"),
            )
        if args.no_enable_feature:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--no-enable-feature", runtime="claude",
                hint=("--no-enable-feature only takes effect under "
                      "--runtime codex; omit it for claude"),
            )
        if args.zcode_config is not None:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--zcode-config", runtime="claude",
                hint=("--zcode-config is ZCode-only; omit it under "
                      "--runtime claude"),
            )
        if args.settings_file is None:
            args.settings_file = default_claude_settings()
        return None
    if args.runtime == "codex":
        if args.settings_file is not None:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--settings-file", runtime="codex",
                hint=("--settings-file is Claude-only; omit it under "
                      "--runtime codex"),
            )
        if args.zcode_config is not None:
            return _emit_error(
                "flag_not_applicable_for_runtime",
                flag="--zcode-config", runtime="codex",
                hint=("--zcode-config is ZCode-only; omit it under "
                      "--runtime codex"),
            )
        if args.hooks_file is None:
            args.hooks_file = default_codex_hooks()
        if args.config_file is None:
            args.config_file = default_codex_config()
        return None
    # args.runtime == "zcode"
    if args.settings_file is not None:
        return _emit_error(
            "flag_not_applicable_for_runtime",
            flag="--settings-file", runtime="zcode",
            hint=("--settings-file is Claude-only; omit it under "
                  "--runtime zcode"),
        )
    if args.hooks_file is not None:
        return _emit_error(
            "flag_not_applicable_for_runtime",
            flag="--hooks-file", runtime="zcode",
            hint=("--hooks-file is Codex-only; omit it under "
                  "--runtime zcode"),
        )
    if args.config_file is not None:
        return _emit_error(
            "flag_not_applicable_for_runtime",
            flag="--config-file", runtime="zcode",
            hint=("--config-file is Codex-only; omit it under "
                  "--runtime zcode"),
        )
    if args.no_enable_feature:
        return _emit_error(
            "flag_not_applicable_for_runtime",
            flag="--no-enable-feature", runtime="zcode",
            hint=("--no-enable-feature only takes effect under "
                  "--runtime codex; omit it for zcode"),
        )
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
    if not args.no_enable_feature:
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
        "args": [str(reminder), "--format", "zcode"],
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
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Light RIP reminder as a UserPromptSubmit "
                    "hook on a hook-based agent runtime (Claude Code, Codex, "
                    "or ZCode). Pick one with --runtime.",
    )
    parser.add_argument(
        "--runtime",
        choices=["claude", "codex", "zcode"],
        required=True,
        help="Which agent runtime to install the Light RIP reminder hook into.",
    )

    # ----- Claude-specific (only meaningful under --runtime claude) -----
    parser.add_argument(
        "--settings-file", type=Path, default=None,
        help="(claude) Path to Claude Code settings.json. "
             "Default: ~/.claude/settings.json.",
    )

    # ----- Codex-specific (only meaningful under --runtime codex) -----
    parser.add_argument(
        "--hooks-file", type=Path, default=None,
        help="(codex) Path to Codex hooks.json. Default: ~/.codex/hooks.json.",
    )
    parser.add_argument(
        "--config-file", type=Path, default=None,
        help="(codex) Path to Codex config.toml. Default: ~/.codex/config.toml.",
    )
    parser.add_argument(
        "--no-enable-feature", action="store_true", default=False,
        help="(codex) Do not edit config.toml to enable Codex hooks.",
    )

    # ----- ZCode-specific (only meaningful under --runtime zcode) -----
    parser.add_argument(
        "--zcode-config", type=Path, default=None,
        help="(zcode) Path to ZCode config.json. Default: "
             "~/.zcode/cli/config.json.",
    )

    # ----- Shared -----
    parser.add_argument(
        "--force-backup", action="store_true", default=False,
        help="Overwrite an existing <path>.bak-YYYY-MM-DD backup instead of "
             "aborting with error=backup_collision.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    rc = _validate_args(args)
    if rc is not None:
        return rc

    installer = INSTALLERS[args.runtime]
    return installer(args)


if __name__ == "__main__":
    raise SystemExit(main())