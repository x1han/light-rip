#!/usr/bin/env python3
"""Verify the Light RIP install.

Two layers of checks:

  1. Script-side smoke tests (runtime-agnostic):
     - `reminder.md` is readable at the expected skill-root location.
     - `light_rip_reminder.py` can start, read the reminder, and emit
       stdout containing the literal marker "Evidence Before Claims".

  2. Runtime config verification:
     - For each runtime the user has installed on, verify that the
       runtime's config file actually contains a Light RIP hook entry.
     - This catches the silent failure mode where the installer ran
       but the config was overwritten, never written, or corrupted.

Exit codes:
  0 — script-side checks pass AND no runtime shows a broken install.
      With `--runtime <name>`, exit 0 means that specific runtime has
      a working entry. With `--runtime all` (default), exit 0 means
      every probed runtime (config file exists) has a working entry,
      OR no runtime config file was found at the default location
      (Category A — user has not chosen any runtime yet).
  1 — script-side checks pass, but at least one runtime's config
      exists and is missing the Light RIP entry / has ambiguous
      duplicates (Category B). OR `--runtime <name>` was specified
      and that runtime has no Light RIP entry.
  2 — setup is wrong: reminder.md missing, OR a runtime config file
      exists but is corrupt / unreadable (Category C).

Output:
  - default: human-readable multi-line summary
  - --json : single JSON object on stdout with two sibling keys
    `checks` (script-side) and `runtime_checks` (per-runtime state)

Per-runtime path overrides (CI / sandbox use):
  --claude-settings PATH   default ~/.claude/settings.json
  --codex-hooks PATH       default ~/.codex/hooks.json
  --codex-config PATH      default ~/.codex/config.toml
  --zcode-config PATH      default ~/.zcode/cli/config.json

Probe forwarding:
  When the parent process has LIGHT_RIP_PROBE=1, the verifier forwards
  the reminder script's stderr (which carries a probe JSON line) to
  its own stderr so the user can see the trace.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Make the sibling installer_common importable when this script is
# invoked as `python hooks/verify_install.py` (which does NOT add the
# script's directory to sys.path the way `python -m` does). We only
# need the canonical NAMESPACE constant and the namespace predicate
# from there — the bulk of this module is self-contained.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_common import NAMESPACE, is_our_namespace  # noqa: E402

# TOML library for config.toml reading. tomllib is stdlib on Python
# >= 3.11, tomli is the backport. If neither is available we fall
# back to a regex line scan that mirrors the (now-removed) install
# path so verification still works on systems with neither library;
# the install_codex_hook installer itself now requires tomli_w to
# write config.toml safely, so a missing tomli_w only blocks install,
# not verify.
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[import-untyped,no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


REMINDER_MARKER = "Evidence Before Claims"
REMINDER_SCRIPT_NAME = "light_rip_reminder.py"

# PEP 394 (POSIX blessed names) + PEP 397 (Windows Python launcher).
# Matches `python`, `python2`, `python3`, `python3.12`, `py`, `py3`,
# with or without a `.exe` suffix. Explicitly rejects `pythonw` (GUI
# launcher — doesn't allocate a console, may not run console scripts
# reliably), `python_d` / `pythonw_d` (debug builds), `mypython`,
# `python3.12-config`, `py-config`, and arbitrary names. Anchored on
# the full filename via `cmd_path.name` so `python3.12-config.exe`
# is not silently reduced to stem `python3.12` and accepted.
# Case-insensitive because Windows filenames are case-insensitive.
_PY_LAUNCHER = re.compile(
    r"^(?:python(?:2|3(?:\.\d+)?)?|py\d*)(?:\.exe)?$",
    re.IGNORECASE,
)


# ---------- path resolution ----------

def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def reminder_md_path() -> Path:
    return skill_root() / "reminder.md"


def reminder_script_path() -> Path:
    return skill_root() / "hooks" / "light_rip_reminder.py"


def default_claude_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_codex_hooks() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def default_codex_config() -> Path:
    return Path.home() / ".codex" / "config.toml"


def default_zcode_config() -> Path:
    return Path.home() / ".zcode" / "cli" / "config.json"


# ---------- check primitives ----------

def check(label: str, ok: bool, detail: str = "") -> dict:
    return {"name": label, "ok": bool(ok), "detail": detail}


def _safe_load_json(path: Path) -> tuple[object | None, str]:
    """Return (parsed, error_string). error_string is empty on success."""
    if not path.is_file():
        return None, "file_not_found"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"


def _latest_backup(path: Path) -> Path | None:
    """Return the most recent <path>.bak-* sibling by mtime, or None."""
    candidates = [
        p for p in path.parent.glob(path.name + ".bak-*")
        if p.is_file() and p != path
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def _backup_hint(path: Path) -> str:
    """Append a manual-restore hint pointing at the most recent
    <path>.bak-* sibling, or empty string if none exists."""
    latest = _latest_backup(path)
    if latest is None:
        return ""
    return f"; most recent backup: {latest} (restore: cp '{latest}' '{path}')"


# ---------- script-side checks ----------

def check_reminder_md_readable() -> dict:
    path = reminder_md_path()
    if not path.is_file():
        return check("reminder.md exists", False, f"missing at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return check("reminder.md exists", False, f"unreadable: {exc}")
    if REMINDER_MARKER not in text:
        return check(
            "reminder.md readable",
            False,
            f"{path} readable but missing marker {REMINDER_MARKER!r}",
        )
    return check("reminder.md readable", True,
                 f"{path} ({len(text)} bytes)")


def check_reminder_script_spawn() -> dict:
    """Spawn light_rip_reminder.py with simulated stdin and verify it
    exits 0 and emits the reminder marker. Uses --format harness which
    is the default Claude/Codex envelope; the script-side wiring is
    format-independent (the format only affects the envelope shape
    ZCode parses, not whether the script can spawn and emit content)."""
    script = reminder_script_path()
    if not script.is_file():
        return check("reminder script exists", False, f"missing at {script}")

    payload = '{"input":{"prompt":"hi"},"output":{}}'
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--format", "harness"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return check("reminder script runnable", False,
                     f"`python {script}` timed out after 10s")
    except OSError as exc:
        return check("reminder script runnable", False,
                     f"spawn failed: {exc}")

    # Forward probe stderr if probe mode is active in the parent env.
    # Otherwise the user loses the trace inside the verifier.
    if os.environ.get("LIGHT_RIP_PROBE") == "1" and completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()

    if completed.returncode != 0:
        return check(
            "reminder script runnable",
            False,
            f"exit code {completed.returncode}; "
            f"stderr: {completed.stderr.strip()[:200]}",
        )
    stdout = completed.stdout
    if not stdout.strip():
        return check("reminder script runnable", False, "stdout was empty")
    if REMINDER_MARKER not in stdout:
        return check(
            "reminder script runnable",
            False,
            f"stdout did not contain marker {REMINDER_MARKER!r}; "
            f"first 200 chars: {stdout[:200]!r}",
        )
    return check(
        "reminder script runnable",
        True,
        f"`{script.name}` exited 0, stdout {len(stdout)} bytes, marker present",
    )


def run_script_checks() -> list[dict]:
    return [
        check_reminder_md_readable(),
        check_reminder_script_spawn(),
    ]


# ---------- runtime-side checks ----------


def check_claude(settings_path: Path) -> dict:
    base = {
        "path": str(settings_path),
        "probed": False,
        "installed": False,
        "match_count": 0,
    }
    settings, err = _safe_load_json(settings_path)
    if err == "file_not_found":
        return {**base, "skipped_reason": "file_not_found"}
    if err:
        return {**base, "probed": True, "corrupt": True,
                "detail": f"runtime config corrupt: {err}{_backup_hint(settings_path)}"}
    if not isinstance(settings, dict):
        return {**base, "probed": True, "corrupt": True,
                "detail": f"runtime config root is not a JSON object{_backup_hint(settings_path)}"}
    hooks = settings.get("hooks")
    ups_list = (hooks.get("UserPromptSubmit")
                if isinstance(hooks, dict) else None)
    if not isinstance(ups_list, list):
        return {**base, "probed": True,
                "detail": "no UserPromptSubmit event entry"}
    count = sum(1 for g in ups_list if is_our_namespace(g))
    return {**base, "probed": True, "match_count": count,
            "installed": count == 1,
            "detail": _claude_codex_detail(settings_path, count)}


def _claude_codex_detail(path: Path, count: int) -> str:
    if count > 1:
        return (f"ambiguous: {count} Light RIP entries in {path}; "
                "runtime will honor only the last; remove duplicates")
    if count == 1:
        return f"Light RIP entry found in {path}"
    return f"{path} exists but no Light RIP entry (incomplete install)"


def _codex_feature_enabled(config_path: Path) -> bool | str:
    """True/False on success; string error on parse failure; None if
    file does not exist (treated lenient in --runtime all default)."""
    if not config_path.is_file():
        return None
    if tomllib is not None:
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except OSError as exc:
            return f"unreadable: {exc}"
        # tomllib raises its own subclass of ValueError; catching
        # `Exception` would also mask programming errors.
        except ValueError as exc:
            return f"parse_error: {exc}"
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, dict):
            return False
        value = features.get("hooks")
        # Match Codex's own acceptance: TOML bool ``true`` OR the case-
        # insensitive string ``"true"``. Plain ``bool(...)`` is wrong
        # here because ``bool("false")`` is True (non-empty string).
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() == "true":
            return True
        return False
    # Fallback: regex-based line scan mirroring install_codex_hook.py
    # when neither tomllib nor tomli is available.
    try:
        text = config_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return f"unreadable: {exc}"
    in_features = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            continue
        if in_features and stripped.startswith("hooks"):
            rhs = stripped.split("=", 1)[-1].strip().split("#", 1)[0].strip().lower()
            # Strip a single layer of surrounding quotes so the
            # fallback agrees with the TOML branch on `hooks = "true"`.
            if (rhs.startswith('"') and rhs.endswith('"')) or (
                rhs.startswith("'") and rhs.endswith("'")
            ):
                rhs = rhs[1:-1]
            return rhs == "true"
    return False


def check_codex(hooks_path: Path, config_path: Path) -> dict:
    base = {
        "path": str(hooks_path),
        "probed": False,
        "installed": False,
        "match_count": 0,
    }
    hooks, err = _safe_load_json(hooks_path)
    if err == "file_not_found":
        return {**base, "skipped_reason": "file_not_found"}
    if err:
        return {**base, "probed": True, "corrupt": True,
                "detail": f"runtime config corrupt: {err}{_backup_hint(hooks_path)}"}
    if not isinstance(hooks, dict):
        return {**base, "probed": True, "corrupt": True,
                "detail": "hooks.json root is not a JSON object"}
    ups_list = (hooks.get("hooks", {}).get("UserPromptSubmit")
                if isinstance(hooks.get("hooks"), dict) else None)
    if not isinstance(ups_list, list):
        return {**base, "probed": True,
                "detail": "no UserPromptSubmit event entry"}
    count = sum(1 for g in ups_list if is_our_namespace(g))
    feature = _codex_feature_enabled(config_path)
    if isinstance(feature, str):
        # parse error -- treat as corrupt
        return {**base, "probed": True, "corrupt": True,
                "match_count": count,
                "detail": f"config.toml parse error: {feature}{_backup_hint(config_path)}"}
    if feature is None:
        feature_detail = "config.toml missing"
    elif feature is True:
        feature_detail = "[features] hooks = true"
    else:
        feature_detail = "[features] hooks != true"
    overall = (count == 1) and (feature is True)
    detail = (f"hooks.json: {_claude_codex_detail(hooks_path, count)}; "
              f"config.toml: {feature_detail}")
    return {
        **base,
        "probed": True,
        "match_count": count,
        "installed": overall,
        "config_toml": {
            "path": str(config_path),
            "feature_enabled": feature,
            "detail": feature_detail,
        },
        "detail": detail,
    }


def check_zcode(config_path: Path) -> dict:
    base = {
        "path": str(config_path),
        "probed": False,
        "installed": False,
        "match_count": 0,
    }
    cfg, err = _safe_load_json(config_path)
    if err == "file_not_found":
        return {**base, "skipped_reason": "file_not_found"}
    if err:
        return {**base, "probed": True, "corrupt": True,
                "detail": f"runtime config corrupt: {err}{_backup_hint(config_path)}"}
    if not isinstance(cfg, dict):
        return {**base, "probed": True, "corrupt": True,
                "detail": f"runtime config root is not a JSON object{_backup_hint(config_path)}"}
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return {**base, "probed": True,
                "detail": "no `hooks` block in runtime config"}
    # ZCode's schema (verified via asar source) requires `hooks.enabled`
    # to be true for the hook layer to load at all. A config with the
    # entry present but `hooks.enabled: false` does NOT actually fire.
    # Earlier this check was missing, which caused false-positive
    # "installed" reports whenever the entry existed regardless of the
    # toggle.
    enabled = hooks.get("enabled")
    if enabled is not True:
        return {**base, "probed": True,
                "detail": ("`hooks.enabled` is not true; ZCode will not "
                           "fire hooks regardless of entries below")}
    events = hooks.get("events")
    ups_groups = (events.get("UserPromptSubmit")
                  if isinstance(events, dict) else None)
    if not isinstance(ups_groups, list):
        return {**base, "probed": True,
                "detail": "no `hooks.events.UserPromptSubmit` entry"}
    count = 0
    for group in ups_groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # ZCode's main hook path silently skips entries whose type
            # is not "process". Earlier this check was missing, so a
            # "command"-type entry (which the loader would discard)
            # could still count as installed.
            if str(entry.get("type", "")) != "process":
                continue
            cmd = str(entry.get("command", ""))
            args = entry.get("args", [])
            arg_blob = (" ".join(str(a) for a in args)
                        if isinstance(args, list) else str(args))
            # Require BOTH: command basename looks like a Python
            # interpreter AND args contains our script filename.
            #
            # The earlier predicate (`cmd.endswith(".exe") or "python"
            # in cmd`) accepted any tool whose path ended in `.exe`
            # (e.g. `node.exe`, `powershell.exe`) — every Windows
            # process path matches. We now anchor on the PEP 394 +
            # PEP 397 blessed names via `_PY_LAUNCHER` (defined at
            # module top), which accepts `python`, `python2`,
            # `python3`, `python3.12`, `py`, `py3`, each with or
            # without a `.exe` suffix, and explicitly rejects
            # `pythonw`, `python_d`, `pythonw_d`, `mypython`,
            # `python3.12-config`, `py-config`, and arbitrary names.
            #
            # Use `cmd_path.name` (not `cmd_path.stem`) so the regex
            # sees the full filename including `.exe`. Otherwise
            # `Path("python3.12-config.exe").stem == "python3.12"` and
            # the dev tool would be falsely accepted as an interpreter.
            cmd_path = Path(cmd) if cmd else Path("")
            cmd_name = cmd_path.name
            cmd_is_python = bool(_PY_LAUNCHER.match(cmd_name))
            if cmd_is_python and REMINDER_SCRIPT_NAME in arg_blob:
                count += 1
    return {
        **base,
        "probed": True,
        "match_count": count,
        "installed": count == 1,
        "detail": _zcode_detail(config_path, count),
    }


def _zcode_detail(path: Path, count: int) -> str:
    if count > 1:
        return (f"ambiguous: {count} Light RIP entries in {path}; "
                "remove duplicates — runtime may pick the wrong one")
    if count == 1:
        return f"Light RIP entry found in {path}"
    return f"{path} exists but no Light RIP entry (incomplete install)"


# ---------- orchestration ----------

def run_runtime_checks(runtime: str, paths: dict) -> dict:
    """Return {runtime_name: state_dict} for the requested scope."""
    out = {}
    if runtime in ("all", "claude"):
        out["claude"] = check_claude(paths["claude_settings"])
    if runtime in ("all", "codex"):
        out["codex"] = check_codex(paths["codex_hooks"], paths["codex_config"])
    if runtime in ("all", "zcode"):
        out["zcode"] = check_zcode(paths["zcode_config"])
    if runtime != "all" and runtime in out:
        out = {runtime: out[runtime]}
    return out


def compute_exit_code(
    script_checks: list[dict],
    runtime_checks: dict,
    runtime_mode: str = "all",
) -> int:
    """3-category logic:
      - reminder.md missing entirely: exit 2 (setup wrong), overrides
        everything else but does NOT short-circuit runtime state — the
        caller has already populated runtime_checks so the JSON can
        carry both errors.
      - script-side check failure (other than missing reminder.md,
        which is handled at the top): contributes 1
      - Category A (file missing): contributes 0 in `--runtime all`
        lenient mode (user has not chosen this runtime), contributes 1
        in `--runtime <name>` strict mode
      - Category B (file exists, no entry / ambiguous): contributes 1
      - Category C (corrupt): contributes 2 (overrides B)
    """
    if not reminder_md_path().is_file():
        return 2

    script_ok = all(c["ok"] for c in script_checks)
    if not script_ok:
        return 1

    any_b = False
    any_c = False
    for state in runtime_checks.values():
        if not state.get("probed", False):
            # Category A: file not found. In `--runtime <name>` strict
            # mode the user explicitly asked for this runtime, so
            # missing counts as incomplete (B). In `--runtime all`
            # default mode, the user has not chosen this runtime, so
            # it contributes 0.
            if runtime_mode != "all":
                any_b = True
            continue
        if state.get("corrupt", False):
            any_c = True
        # `installed` is the authoritative composite per-runtime
        # verdict: for Codex it folds in `hooks.features.enabled`,
        # which `match_count` alone does not capture. Trust the
        # composite so a 1-match hook entry with a missing
        # config.toml does not falsely exit 0.
        elif not state.get("installed", False):
            any_b = True
        elif state.get("match_count", 0) != 1:
            # Ambiguous: more than one matching entry. `installed`
            # already said False in this branch so we never reach
            # here on a clean 1-match install.
            any_b = True
    if any_c:
        return 2
    if any_b:
        return 1
    return 0


# ---------- output ----------

def render_human(script_checks: list[dict], runtime_checks: dict,
                 exit_code: int) -> str:
    header = "Light RIP install verification — " + (
        "PASS" if exit_code == 0 else f"FAIL (exit {exit_code})"
    )
    lines = [header, ""]
    lines.append("Script-side checks:")
    for c in script_checks:
        marker = "OK  " if c["ok"] else "FAIL"
        detail = f" — {c['detail']}" if c["detail"] else ""
        lines.append(f"  [{marker}] {c['name']}{detail}")
    lines.append("")
    lines.append("Runtime config checks:")
    any_probed = False
    for name, state in runtime_checks.items():
        if not state.get("probed", False):
            reason = state.get("skipped_reason", "not_checked")
            lines.append(f"  [SKIP] {name}: {reason} ({state.get('path', '')})")
            continue
        any_probed = True
        ok = state.get("installed", False)
        marker = "OK  " if ok else "FAIL"
        detail = f" — {state.get('detail', '')}"
        lines.append(f"  [{marker}] {name}{detail}")
        if name == "codex" and isinstance(state.get("config_toml"), dict):
            ct = state["config_toml"]
            lines.append(f"         config.toml — {ct.get('detail', '')}")
    if not any_probed:
        lines.append(f"  (no runtime config files found at default locations — "
                     "install with install_claude_hook.py / "
                     "install_codex_hook.py, or follow the ZCode worked "
                     "example printed by install_general_agent_hook.py)")
    return "\n".join(lines) + "\n"


def render_json(script_checks: list[dict], runtime_checks: dict,
                exit_code: int) -> str:
    return json.dumps(
        {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "checks": script_checks,
            "runtime_checks": runtime_checks,
        },
        indent=2, ensure_ascii=True,
    )


# ---------- entry point ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Light RIP install. Two layers: "
                    "script-side smoke tests + per-runtime config checks. "
                    "Use --runtime <name> to require a specific runtime "
                    "to be installed; default --runtime all is lenient "
                    "toward runtimes the user has not chosen yet.",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable text.")
    parser.add_argument("--runtime", choices=["claude", "codex", "zcode", "all"],
                        default="all",
                        help="Which runtime to verify (default: all). "
                             "Strict when a specific name is given: "
                             "missing entry -> exit 1.")
    parser.add_argument("--claude-settings", type=Path,
                        default=default_claude_settings(),
                        help="Path to Claude Code settings.json "
                             "(override for CI/sandbox).")
    parser.add_argument("--codex-hooks", type=Path,
                        default=default_codex_hooks(),
                        help="Path to Codex hooks.json (override).")
    parser.add_argument("--codex-config", type=Path,
                        default=default_codex_config(),
                        help="Path to Codex config.toml (override).")
    parser.add_argument("--zcode-config", type=Path,
                        default=default_zcode_config(),
                        help="Path to ZCode config.json (override).")
    args = parser.parse_args()

    paths = {
        "claude_settings": args.claude_settings,
        "codex_hooks": args.codex_hooks,
        "codex_config": args.codex_config,
        "zcode_config": args.zcode_config,
    }

    script_checks = run_script_checks()
    runtime_checks = run_runtime_checks(args.runtime, paths)
    exit_code = compute_exit_code(script_checks, runtime_checks, args.runtime)

    if args.json:
        print(render_json(script_checks, runtime_checks, exit_code))
    else:
        print(render_human(script_checks, runtime_checks, exit_code), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())