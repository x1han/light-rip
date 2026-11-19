#!/usr/bin/env python3
"""Light RIP general installer — prompt dispatcher.

This script is the universal entry point for installing the Light RIP
reminder on **any** agent runtime. Unlike `install_hook_based_harness.py`
(which knows the hook schema for Claude Code, Codex, and Antigravity),
this script does **not** write any files and does **not** take a
`--agent` argument. Its sole job is to print a structured prompt that:

  1. Asks the receiving agent to identify which harness it is running on.
  2. If it is Claude Code, Codex, or Antigravity, tells it to run
     `install_hook_based_harness.py install --runtime <name>` directly.
  3. If it is any other harness (such as ZCode, OpenCode, etc.), tells
     it to inspect its own harness's hook/instructions mechanism, study
     how Claude Code, Codex, and Antigravity are adapted in
     `hooks/install_hook_based_harness.py` and
     `hooks/light_rip_reminder.py`, evaluate the cleanest adaptation for
     its own harness, and ask the user for confirmation before
     installing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

NAMESPACE = "light-rip-reminder"
PROMPT_MARKER_START = "[light-rip-install-prompt]"
PROMPT_MARKER_END = "[end light-rip-install-prompt]"


# ---------- path resolution ----------

def skill_root() -> Path:
    """<skill>/hooks/install_general_harness.py → <skill>."""
    return Path(__file__).resolve().parents[1]


def reminder_script() -> Path:
    return skill_root() / "hooks" / "light_rip_reminder.py"


def reminder_md() -> Path:
    return skill_root() / "reminder.md"


def detect_python() -> str:
    return str(Path(sys.executable).resolve())


# ---------- prompt rendering ----------

def build_prompt(python_exe: str, skill: Path) -> str:
    reminder_py = reminder_script()
    reminder_path = reminder_md()

    return f"""{PROMPT_MARKER_START}
python: {python_exe}
skill-root: {skill}
reminder-md: {reminder_path}
reminder-script: {reminder_py}

You are tasked with installing the **Light RIP reminder** for the
agent harness you are currently running on.

  - The reminder source is `{reminder_path}`. It is **runtime-neutral**
    and must not be edited.
  - The reminder script is `{reminder_py}`. It reads `{reminder_path}`
    and emits a JSON envelope on stdout (`--format harness` for Claude
    Code / Codex `UserPromptSubmit`, `--format antigravity` for
    Antigravity `PreInvocation`).

== Step 1: Identify your harness ==

Check which agent harness you are running inside right now:

  - **Claude Code**:  `python {skill}/hooks/install_hook_based_harness.py install --runtime claude`
                      (writes `~/.claude/settings.json`).
  - **Codex**:        `python {skill}/hooks/install_hook_based_harness.py install --runtime codex`
                      (writes `~/.codex/hooks.json` and enables `[features] hooks = true`
                      in `~/.codex/config.toml`).
  - **Antigravity**:  `python {skill}/hooks/install_hook_based_harness.py install --runtime antigravity`
                      (writes `~/.gemini/config/hooks.json` under `PreInvocation`).

If you are one of these three harnesses, run the matching command above
directly, verify with `python {skill}/hooks/verify_install.py --runtime <name>`,
and tell the user to restart the harness if needed. Do NOT edit
`~/.claude/`, `~/.codex/`, or `~/.gemini/config/hooks.json` manually.

== Step 2: Any other harness (e.g. ZCode, OpenCode, Cursor, custom CLI) ==

If you are NOT Claude Code, Codex, or Antigravity:

  1. **Inspect your own harness's hook / system-prompt injection
     mechanism** (check your runtime's docs, config schema, or loader
     source: event names, stdin/stdout JSON contract, config file path,
     or `instructions`-style context-file lists).
  2. **Study how the three supported harnesses are adapted** by reading
     `{skill}/hooks/install_hook_based_harness.py` and
     `{skill}/hooks/light_rip_reminder.py` (backups, atomic writes,
     namespace isolation, and output envelope formatting).
  3. **Evaluate the cleanest adaptation** for your harness (either a
     dedicated hook format + config entry, or an `instructions`-style
     file reference if no hook layer exists).
  4. **Ask the user for confirmation before modifying any config files**
     — summarize what files/entries you plan to write or adapt and wait
     for their approval.

== Reference paths for other harnesses ==

**Path A — instructions (no hook script needed)**:
  If your runtime reads a config array of context/instruction files
  (e.g., OpenCode's `~/.config/opencode/opencode.json` -> `instructions`),
  append `"{reminder_path}"` to that list.

**Path B — hook (script invoked per prompt/invocation)**:
  If your runtime exposes a prompt/invocation hook (e.g., `UserPromptSubmit`
  or `PreInvocation` — such as ZCode's `~/.zcode/cli/config.json`),
  check whether `--format harness` fits or if a new entry in `FORMATS`
  inside `{reminder_py}` is needed, back up any edited config as
  `<path>.bak-YYYY-MM-DD`, never overwrite entries outside `{NAMESPACE}`,
  and verify the script output with:

    echo '{{"input":{{"prompt":"hi"}},"output":{{}}}}' | python {reminder_py} --format harness

  and:

    python {skill}/hooks/verify_install.py

== Output contract ==

After installing (or after user confirmation for other harnesses), report:

    {{
      "ok": true|false,
      "kind": "hook"|"instructions",
      "paths": ["<absolute path>", ...],
      "notes": "<free text — caveats, restart instructions, etc.>"
    }}

{PROMPT_MARKER_END}"""


# ---------- entry point ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print a Light RIP install prompt. Runtime-agnostic: "
                    "does not take an agent name. Does not write files "
                    "itself; the receiving agent executes the install.",
    )
    parser.add_argument("--python",
                        help="Absolute path to the Python interpreter to "
                             "encode in the prompt. Defaults to the "
                             "interpreter running this script.")
    parser.add_argument("--skill-root",
                        help="Absolute path to the Light RIP skill root. "
                             "Defaults to the directory containing this "
                             "script's parent.")
    args = parser.parse_args()

    skill = (Path(args.skill_root).expanduser().resolve()
             if args.skill_root else skill_root())
    if not (skill / "reminder.md").is_file():
        print(json.dumps({"error": f"reminder.md not found under {skill}"}),
              file=sys.stderr)
        return 1
    if not (skill / "hooks" / "light_rip_reminder.py").is_file():
        print(json.dumps({"error": f"reminder script not found under {skill}"}),
              file=sys.stderr)
        return 1

    python_exe = args.python or detect_python()

    prompt = build_prompt(
        python_exe=python_exe,
        skill=skill,
    )
    # Print the prompt to stdout, no leading/trailing decoration.
    # Callers can pipe it into another agent's input channel verbatim.
    sys.stdout.write(prompt)
    if not prompt.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())