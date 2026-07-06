#!/usr/bin/env python3
"""Light RIP general installer — prompt dispatcher.

This script is the universal entry point for installing the Light RIP
reminder on **any** agent runtime. Unlike `install_codex_hook.py` and
`install_claude_hook.py`, which know the exact hook schema of their
target runtime, this script does **not** write any files and does
**not** take a `--agent` argument. Its sole job is to print a
structured prompt that:

  1. States what the reminder is and where its source files live.
  2. Walks the receiving agent through two reference examples —
     OpenCode (instructions path) and ZCode (hook path) — and asks
     it to install for its own runtime **by analogy**.
  3. Names the verify script to confirm the install landed.

The script is deliberately **agent-name-agnostic**. We do not
enumerate agent runtimes: there are too many, and the list would go
stale immediately. Two examples are enough to convey the two install
paths the agent can take; for any other runtime, the receiving agent
inspects its own environment, picks the matching path, and runs it.

The two reference paths the prompt covers:

  - instructions — runtime reads a config file that lists context
                   files; the runtime itself concatenates `reminder.md`
                   onto the system prompt, no script invocation needed.
  - hook         — runtime has a `UserPromptSubmit` hook layer; the
                   reminder script gets invoked on every prompt and
                   injects `reminder.md` into the context.

After running this script, feed the printed prompt to the target
agent (or follow its steps yourself). Then run
`hooks/verify_install.py --agent <runtime-name>` to confirm.

Backward-compatibility note
---------------------------
Earlier versions of this script auto-installed for one specific
runtime using flags such as `--data-dir`, `--hooks-dir`, `--format`,
`--no-path-fix`, and `--agent`. All those flags are removed; passing
any of them now triggers an argparse error. The new design is
runtime-agnostic — the agent that receives the printed prompt decides
the install path.
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
    """<skill>/hooks/install_general_agent_hook.py → <skill>."""
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
agent runtime you are currently running on (or for some other agent
runtime the user is configuring — confirm with the user if unclear).

  - The reminder source is `{reminder_path}`. It is **runtime-neutral**
    and must not be edited.
  - The reminder script is `{reminder_py}`. It reads the reminder
    file on every hook fire and prints a JSON envelope. It accepts
    a `--format` flag whose default is `harness` — that default emits
    the standard `hookSpecificOutput.additionalContext` envelope that
    most runtimes accept.

There are exactly two install paths. Pick the one that matches your
runtime by **inspecting your own environment first** (the runtime's
config files, docs, or source). The two paths below are worked
examples for two well-known runtimes; install by analogy for any
other.

== Path 1: instructions — runtime reads a config that lists context files ==

**When this fits**: your runtime reads a JSON/YAML/TOML config file
that contains a list of "instructions", "system prompt fragments",
"context files", or similar. The runtime itself concatenates those
files onto the system prompt at startup. No script invocation needed.

**Worked example — OpenCode**:

OpenCode reads `~/.config/opencode/opencode.json` and concatenates
every path under the `instructions` array onto its system prompt.

Install:
  1. Open (or create) `~/.config/opencode/opencode.json`.
  2. Make sure the top-level `instructions` key is a JSON array.
     If it already exists, append. If not, create it.
  3. Append `{reminder_path}` (with double quotes inside the JSON
     string) to that array. The full schema is:
         {{
           "$schema": "https://opencode.ai/config.json",
           "instructions": ["{reminder_path}"]
         }}
     If `instructions` already had entries, append to the array,
     do not overwrite.
  4. Save the file. Restart OpenCode — it re-reads `instructions`
     on launch.

Why this works: OpenCode has no hook layer; it has no notion of
"reminder script invocation". But it already has a documented
mechanism for injecting extra context into every prompt —
`instructions`. We use it. No Python is involved.

**By analogy for any other agent**: if its config has anything that
looks like "instructions" / "system_prompt_append" / "context_files"
/ "prepend_files" / etc., add `"{reminder_path}"` to that list.
Restart the runtime.

After installing, confirm with:
    python {skill}/hooks/verify_install.py --agent opencode

== Path 2: hook — runtime has a UserPromptSubmit hook layer ==

**When this fits**: your runtime has a documented mechanism to invoke
a script (or run a JSON-defined hook entry) on every user prompt,
BEFORE the agent reasons. Examples include Claude/Codex-style hooks
or any runtime that exposes `UserPromptSubmit` (or a similarly named)
event.

The reminder script contract is the same on every runtime:
  - Stdin: a JSON object `{{"input": ..., "output": ...}}` from the runtime.
  - Stdout: a JSON object that the runtime merges into `output`
    (the `harness` format — `additionalContext` field — works on most).
  - Non-zero exit / timeout is isolated by the runtime; later hooks
    still run. The script fails open: missing `reminder.md` does not
    block the prompt.

**Worked example — ZCode**:

ZCode stores user-level config at `~/.zcode/config.json`. Its loader
(in `resources/app.asar → out/host/index.js`, function
`fromZCodeHooksConfig`) reads a `hooks` block of this shape:

  - top-level `hooks` keys allowed: `enabled`, `timeoutMs`,
    `maxOutputBytes`, `events`.
  - `events.<Event>[i] = {{ matcher?, hooks: [...] }}`
  - `hooks[i] required fields`: `type="process"`, `command`,
    `args?` (list), `timeoutMs?` (number).

  - The `type` MUST be `"process"` (not `"command"`); ZCode's
    main hook path silently skips entries of other types.

Install:
  1. Open `~/.zcode/config.json`. Preserve all existing top-level
     keys (API keys, providers, locale, …). Use Python
     `json.load` + dict merge + `json.dump` rather than text editing.
  2. Make sure `hooks.enabled` is `true`.
  3. Under `hooks.events.UserPromptSubmit`, append an entry with:
       matcher: "" (or empty)
       hooks[0].type = "process"
       hooks[0].command = {python_exe}
       hooks[0].args = [{reminder_py}, "--format", "harness"]
       hooks[0].timeoutMs = 5000
     Do not delete existing entries that belong to other tools.
  4. Back up the file first (`cp ~/.zcode/config.json
     ~/.zcode/config.json.bak-YYYY-MM-DD`).
  5. Restart ZCode (the desktop app re-reads `config.json` on launch).

**By analogy for any other agent**: look up the runtime's hook schema
in its own docs or in the loader source. Write a matching entry whose
command spawns `{reminder_py} --format harness` with the runtime's
expected JSON shape. The reminder script will print a
`hookSpecificOutput.additionalContext` envelope that most runtimes
accept verbatim.

**Dedicated installers exist for two well-known runtimes**:
  - Claude Code:  `python {skill}/hooks/install_claude_hook.py`
                  (writes `~/.claude/settings.json` JSON entry).
  - Codex:        `python {skill}/hooks/install_codex_hook.py`
                  (writes `~/.codex/hooks.json` JSON entry).
If your runtime IS Claude Code or Codex, run the dedicated installer
instead of following the ZCode analogy. Do not use this general
installer to edit `~/.claude/` or `~/.codex/`.

After installing, confirm with:
    python {skill}/hooks/verify_install.py --agent zcode

== Choosing between the two paths ==

Prefer **Path 1 (instructions)** whenever it fits — it has no
process to spawn, no timeout to tune, no JSON envelope to merge, and
the runtime does all the work. Reach for **Path 2 (hook)** only when
the runtime truly has no `instructions`-style config and only a hook
event will reach every prompt.

== Hard requirements (apply to both paths) ==

  - Never use this general installer to modify `~/.codex/` or
    `~/.claude/`. Those have dedicated installers and a fixed schema.
  - Never overwrite a hook entry that does not belong to the
    `{NAMESPACE}` namespace. Always check `metadata.hook_namespace`
    (legacy JSON hooks) or `command` substring (process hooks) before
    replacing.
  - Back up any config file you modify (`<path>.bak-<YYYY-MM-DD>`
    suffix).
  - The reminder file is runtime-neutral; do not edit
    `{reminder_path}` to tailor it to the target runtime.
  - After writing, restart the runtime so it picks up the new config.

== Output contract ==

After install, the receiving agent must return a single JSON object
on its output channel (chat reply, stdout, etc.):

    {{
      "ok": true|false,
      "kind": "hook"|"instructions",
      "paths": ["<absolute path>", ...],
      "notes": "<free text — caveats, restart instructions, etc.>"
    }}

The caller will then run `verify_install.py` to confirm.

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