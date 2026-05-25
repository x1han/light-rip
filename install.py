#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_NAME = "light-rip"


def default_codex_skills_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills"


def default_claude_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def copy_skill(source_dir: Path, dest_dir: Path) -> Path:
    target = dest_dir / SKILL_NAME
    target.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "reminder.md", "hooks"):
        source = source_dir / name
        dest = target / name
        if source.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, dest)
    return target


def run_hook_installer(target: Path, platform: str) -> None:
    if platform == "codex":
        script = target / "hooks" / "install_codex_hook.py"
    elif platform == "claude":
        script = target / "hooks" / "install_claude_hook.py"
    else:
        raise ValueError(f"unknown platform: {platform}")
    subprocess.run([sys.executable, str(script)], check=True)


def detect_platform(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("CODEX_HOME") or (Path.home() / ".codex").exists():
        return "codex"
    return "claude"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Light RIP and mount its required UserPromptSubmit hook.")
    parser.add_argument("--platform", choices=("codex", "claude"), help="Target agent environment. Defaults to Codex when detected.")
    parser.add_argument("--skills-dir", type=Path, help="Override the target skills directory.")
    args = parser.parse_args()

    source_dir = Path(__file__).resolve().parent
    platform = detect_platform(args.platform)
    skills_dir = args.skills_dir
    if skills_dir is None:
        skills_dir = default_codex_skills_dir() if platform == "codex" else default_claude_skills_dir()

    target = copy_skill(source_dir, skills_dir.expanduser().resolve())
    run_hook_installer(target, platform)
    print(f"Installed {SKILL_NAME} to {target}")
    print(f"Mounted required {platform} UserPromptSubmit hook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
