#!/usr/bin/env python3
"""
Light RIP — Reasonix (Codex) Hook Installer

Reasonix runs on Codex. This installer registers the Light RIP reminder as a
Codex UserPromptSubmit hook, which injects the reminder text before every
user prompt so the agent always follows the Light RIP workflow.

Installation:  python hooks/install_reasonix_hook.py
Uninstall:     python hooks/install_reasonix_hook.py --uninstall
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


NAMESPACE = "light-rip-reminder"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def codex_home() -> Path:
    """Return the Codex home directory (Reasonix home directory)."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def hooks_path() -> Path:
    return codex_home() / "hooks.json"


def config_path() -> Path:
    return codex_home() / "config.toml"


def skill_dir() -> Path:
    """Return the light-rip skill directory (relative to this script)."""
    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Config.toml helpers — enable hooks feature
# ---------------------------------------------------------------------------

def ensure_hooks_feature(cfg_path: Path) -> None:
    """Ensure [features] hooks = true exists in config.toml."""
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("[features]\nhooks = true\n", encoding="utf-8")
        return

    lines = cfg_path.read_text(encoding="utf-8-sig").splitlines()
    features_start = None
    next_section = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[features]":
            features_start = idx
        if features_start is not None and idx > features_start and stripped.startswith("[") and stripped.endswith("]"):
            next_section = idx
            break

    if features_start is None:
        prefix = lines + ([] if not lines or lines[-1] == "" else [""])
        prefix.extend(["[features]", "hooks = true"])
        cfg_path.write_text("\n".join(prefix) + "\n", encoding="utf-8")
        return

    for idx in range(features_start + 1, next_section):
        if lines[idx].strip().startswith("hooks"):
            lines[idx] = "hooks = true"
            cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    lines.insert(next_section, "hooks = true")
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Hook group builder
# ---------------------------------------------------------------------------

def build_hook_group(python_exe: Path, hook_script: Path) -> dict:
    return {
        "metadata": {
            "workflow": NAMESPACE,
            "hook_role": "UserPromptSubmit",
            "hook_namespace": NAMESPACE,
        },
        "hooks": [
            {
                "type": "command",
                "command": f'"{python_exe}" "{hook_script}"',
                "statusMessage": "[LR] Light RIP: checking task scope...",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Hook upsert / remove
# ---------------------------------------------------------------------------

def upsert_hook(hooks_config: dict, group: dict) -> None:
    """Add or replace the light-rip hook group in hooks_config."""
    hooks_root = hooks_config.setdefault("hooks", {})
    event_list = hooks_root.setdefault("UserPromptSubmit", [])
    for index, existing in enumerate(event_list):
        metadata = existing.get("metadata") if isinstance(existing, dict) else None
        if isinstance(metadata, dict) and metadata.get("hook_namespace") == NAMESPACE:
            event_list[index] = group
            return
    event_list.append(group)


def remove_hook(hooks_config: dict) -> bool:
    """Remove the light-rip hook group. Returns True if removed."""
    hooks_root = hooks_config.get("hooks", {})
    event_list = hooks_root.get("UserPromptSubmit", [])
    before = len(event_list)
    hooks_root["UserPromptSubmit"] = [
        g for g in event_list
        if not (
            isinstance(g, dict)
            and isinstance(g.get("metadata"), dict)
            and g["metadata"].get("hook_namespace") == NAMESPACE
        )
    ]
    return len(hooks_root["UserPromptSubmit"]) < before


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or uninstall the Light RIP reminder as a Reasonix/Codex UserPromptSubmit hook."
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove the Light RIP hook instead of installing it."
    )
    parser.add_argument(
        "--hooks-file", type=Path, default=None,
        help="Path to hooks.json (default: $CODEX_HOME/hooks.json)."
    )
    parser.add_argument(
        "--config-file", type=Path, default=None,
        help="Path to config.toml (default: $CODEX_HOME/config.toml)."
    )
    parser.add_argument(
        "--no-enable-feature", action="store_true",
        help="Do not edit config.toml to enable hooks feature."
    )
    args = parser.parse_args()

    hk_path = (args.hooks_file or hooks_path()).expanduser().resolve()
    cfg_path = (args.config_file or config_path()).expanduser().resolve()
    hook_script = skill_dir() / "hooks" / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    hooks_config = load_json(hk_path)

    if args.uninstall:
        removed = remove_hook(hooks_config)
        write_json(hk_path, hooks_config)
        result = {
            "action": "uninstalled" if removed else "not_found",
            "hooks_path": str(hk_path),
            "hook": NAMESPACE,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 0 if removed else 1

    # Install
    upsert_hook(hooks_config, build_hook_group(python_exe, hook_script))
    write_json(hk_path, hooks_config)
    if not args.no_enable_feature:
        ensure_hooks_feature(cfg_path)

    result = {
        "action": "installed",
        "hooks_path": str(hk_path),
        "config_path": str(cfg_path),
        "hook": NAMESPACE,
        "reminder_source": str(hook_script),
    }
    print(json.dumps(result, ensure_ascii=True))
    print(f"\n[OK] Light RIP hook installed for Reasonix (Codex).")
    print(f"   hooks.json  : {hk_path}")
    print(f"   config.toml : {cfg_path}")
    print(f"   Reminder    : {hook_script}")
    print(f"\nRestart your agent session for the hook to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
