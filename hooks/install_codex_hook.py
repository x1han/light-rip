#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


NAMESPACE = "light-rip-reminder"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def ensure_hooks_feature(config_path: Path) -> None:
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("[features]\nhooks = true\n", encoding="utf-8")
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
        config_path.write_text("\n".join(prefix) + "\n", encoding="utf-8")
        return

    for index in range(features_start + 1, next_section):
        if lines[index].strip().startswith("hooks"):
            lines[index] = "hooks = true"
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    lines.insert(next_section, "hooks = true")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    args = parser.parse_args()

    hooks_path = args.hooks_file.expanduser().resolve()
    config_path = args.config_file.expanduser().resolve()
    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    python_exe = Path(sys.executable).resolve()

    hooks_config = load_json(hooks_path)
    upsert(hooks_config, build_group(python_exe, hook_script))
    write_json(hooks_path, hooks_config)
    if not args.no_enable_feature:
        ensure_hooks_feature(config_path)
    print(json.dumps({"hooks_path": str(hooks_path), "config_path": str(config_path), "hook": NAMESPACE}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
