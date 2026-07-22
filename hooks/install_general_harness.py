#!/usr/bin/env python3
"""Light RIP general installer — prompt dispatcher.

This script is the universal entry point for installing the Light RIP
reminder on **any** agent runtime. Unlike `install_hook_based_harness.py`
(which knows the hook schema for Claude Code, Codex, and ZCode), this
script does **not** write any files and does
**not** take a `--agent` argument. Its sole job is to print a
structured prompt that:

  1. States what the reminder is and where its source files live.
  2. Walks the receiving agent through the OpenCode worked example
     (instructions path) and asks it to install for its own runtime
     **by analogy**, picking the instructions path or the hook path
     based on its own loader.
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
`python hooks/verify_install.py` to confirm.

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
    python {skill}/hooks/verify_install.py

(Path 1 has no script to spawn, so there is no self-test to run here.
Confirm `verify_install.py` says PASS and observe one real prompt
to make sure the runtime concatenates `reminder.md` into the
system prompt.)

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

For a runtime whose hook layer is not yet covered by the dedicated
installer (`install_hook_based_harness.py install --runtime <name>`),
inspect the runtime's hook schema in its own docs or loader source.
Write a matching entry whose command spawns `{reminder_py}` (the
default `--format harness` works on most runtimes via the
`hookSpecificOutput.additionalContext` envelope). If your runtime
is Claude Code, Codex, or ZCode, run the dedicated installer with
the matching `--runtime` flag instead of following this path by
hand.

**Dedicated installers exist for three well-known runtimes**:
  - Claude Code:  `python {skill}/hooks/install_hook_based_harness.py --runtime claude`
                  (writes `~/.claude/settings.json` JSON entry).
  - Codex:        `python {skill}/hooks/install_hook_based_harness.py --runtime codex`
                  (writes `~/.codex/hooks.json` JSON entry and enables
                  `[features] hooks = true` in `~/.codex/config.toml`).
  - ZCode:        `python {skill}/hooks/install_hook_based_harness.py --runtime zcode`
                  (writes `~/.zcode/cli/config.json` process entry).
If your runtime IS one of these three, run the unified installer with
the matching `--runtime` flag instead of following the ZCode analogy.
Do not use this general installer to edit `~/.claude/`, `~/.codex/`, or
`~/.zcode/`.

After installing, confirm with:
    python {skill}/hooks/verify_install.py

Then do a self-test of the reminder script — this verifies the script
itself can start and read `reminder.md`, independent of any runtime's
envelope shape:

    echo '{{"input":{{"prompt":"hi"}},"output":{{}}}}' \\
        | python {reminder_py} --format harness

Pass criteria (do not assume any specific envelope field names —
different runtimes use different shapes; rely on the reminder script's
own contract instead):
  - exit code 0
  - stdout is non-empty
  - stdout contains the substring "Evidence Before Claims" (a literal
    marker from `reminder.md`)

If any of those fail, the script is broken at this path: it cannot
start, it cannot read `reminder.md`, or its output does not carry the
reminder content. The runtime would inject nothing. Re-check the
configured `command` / `args` (Path 2) or `instructions` entry
(Path 1).

This self-test does NOT prove the runtime will actually invoke the
hook on real prompts. It only proves the script-side half is wired
up. To prove the runtime half, observe one real prompt's behavior
and confirm the reminder content shows up in the agent's context.

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