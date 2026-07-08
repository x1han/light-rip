# 🪦 Light RIP

Light RIP is an agent skill package, not a software application.

`RIP` names the three safeguards: `Review`, `Implement`, and `Plan`. In execution, every tier runs `Plan -> Implement -> Review` — a lightweight loop with extra review passes for medium and large.

- **`Tiny`**: the main session plans, implements, verifies, and self-reviews. No subagents.
- **`Medium`**: the main session writes an inline plan; a reviewer subagent does a pre-work review; the main session implements and verifies; the reviewer (or a fresh one) does a post-work review of the diff; the main session fixes findings.
- **`Large`**: a planner subagent produces the plan; a reviewer subagent does a pre-work review; an implementer subagent implements and self-verifies; a verifier subagent independently re-runs verification; the reviewer (or a fresh one) does a post-work review of the diff and verification output; the main session coordinates and fixes findings.

## Reviewer and verifier roles

Light RIP uses three independent subagent roles in Medium and Large:

- **Pre-work reviewer**: scrutinizes the plan before any code is written. Catches wrong target files, wrong abstractions, missing verification, and misread requirements.
- **Implementer (Large only)**: does the work and runs the planned verification commands itself.
- **Verifier (Large only)**: independently re-runs the verification commands with fresh eyes, so verification is not single-sourced.
- **Post-work reviewer**: scrutinizes the diff and verification output after implementation. Catches bugs, regressions, transcript contamination, and risky overengineering.

Risk upgrades the tier: risky tiny becomes medium; risky medium becomes large.

P0/P1 findings from any reviewer or verifier pass block completion. The main session fixes them; for Large, the main session may route verifier fixes back through the implementer for fix + self-verify + verifier re-run. Relevant verification runs again, and nontrivial P1 fixes get a short re-review.

## Installation

Do not install it as an app, service, Python package, or normal project checkout. Install it by placing the `light-rip` folder in your agent's skills directory, then mount the required `UserPromptSubmit` reminder hook — the skill folder alone is incomplete without the hook. After installing or updating, restart the agent runtime so the hook registers.

### Claude Code

Claude Code documentation and community examples use `$HOME/.claude/skills` or `~/.claude/skills`. Clone into the Claude Code skills directory and run the required hook setup from the installed copy:

```bash
mkdir -p "$HOME/.claude/skills"
git clone https://github.com/x1han/light-rip "$HOME/.claude/skills/light-rip"
cd "$HOME/.claude/skills/light-rip"
python hooks/install_claude_hook.py
```

### Codex

If `CODEX_HOME` is unset, Codex normally uses `$HOME/.codex`. Clone into the Codex skills directory and run the required hook setup from the installed copy:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/x1han/light-rip "$CODEX_HOME/skills/light-rip"
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

### Other agents (OpenCode / ZCode / anything else)

For any agent runtime that is not Claude Code or Codex, use the **general installer**. It does **not** write any files itself — instead it prints a structured prompt that describes how to install the Light RIP reminder. Feed that prompt to the agent (or to yourself in a chat); the agent then inspects its own runtime and installs by analogy.

```bash
git clone https://github.com/x1han/light-rip
cd light-rip
python hooks/install_general_agent_hook.py
```

The installer takes **no agent name on the command line** — there are too many agent runtimes to enumerate, and the list would go stale immediately. The printed prompt works for any runtime and uses two worked examples as anchors:

  1. **Instructions** — if the runtime reads a list of context files
     (OpenCode's `instructions` field, …), the agent appends a path
     pointing to `reminder.md` and restarts the runtime. **Preferred.**
  2. **Hook** — if the runtime has a `UserPromptSubmit` hook layer
     (ZCode, …), the agent writes the hook entry itself or runs the
     matching dedicated installer.

Other agent runtimes install by analogy to these two paths. See
[Verify the install](#verify-the-install) below for how to confirm
the install landed, regardless of which path was taken.

### Verify the install

After any install — dedicated installer or general installer's
prompt — run the runtime-agnostic verifier:

```bash
python hooks/verify_install.py          # human-readable
python hooks/verify_install.py --json   # machine-readable
```

It performs two checks that are valid for every install path:

  1. `reminder.md` is readable at the expected skill-root location.
  2. `light_rip_reminder.py` can start, read the reminder, and emit
     stdout containing the literal marker `Evidence Before Claims`.

These checks prove the **script-side** wiring is sound. They do NOT
prove any runtime will actually invoke the hook on real prompts —
that requires observing one real prompt and confirming the reminder
content shows up in the agent's context.

The dedicated Claude Code and Codex installers call `verify_install.py`
automatically at the end, so the same two checks run as part of
`python hooks/install_codex_hook.py` and
`python hooks/install_claude_hook.py`.

Exit codes:

  - `0` — both checks passed
  - `1` — one check failed (e.g. `reminder.md` not found, or the
    reminder script could not spawn, or its stdout lacked the marker)
  - `2` — setup is wrong (e.g. `reminder.md` missing entirely; the
    script-side wiring cannot work at all)

This self-test proves the **script-side** wiring. It does **not**
prove the runtime actually invokes the hook on real prompts — that
requires observing one real prompt and confirming the reminder
content shows up in the agent's context.

#### Breaking change vs older releases

Earlier versions of `install_general_agent_hook.py` auto-installed
for one specific runtime and accepted `--data-dir`, `--hooks-dir`,
`--format`, `--no-path-fix`, and `--agent`. All those flags are
removed. The new design is runtime-agnostic — the agent that
receives the printed prompt decides the install path.

The previous Windows PATH-fix behavior (appending Git Bash to the
user PATH via the registry) has also moved out of the installer. If
the chosen install path is a `sh`-backed hook and `sh` is missing
from PATH, the receiving agent must add it manually before invoking
the reminder script.

## Updating

`cd` into the installed `light-rip` folder, run `git pull`, then
re-run the installer that matches your agent (see Installation).


## Files

- `SKILL.md` — the skill instructions.
- `reminder.md` — the context injected by the hook. Runtime-neutral.
- `hooks/light_rip_reminder.py` — the shared hook command. The default `--format harness` emits a `hookSpecificOutput.additionalContext` envelope that most runtimes accept; an alternative format is also shipped for runtimes that rewrite the prompt instead.
- `hooks/install_codex_hook.py` — Codex-specific installer (auto-writes `~/.codex/hooks.json`).
- `hooks/install_claude_hook.py` — Claude Code-specific installer (auto-writes `~/.claude/settings.json`).
- `hooks/install_general_agent_hook.py` — generic installer for any other agent runtime. Prints an install prompt; does not write files itself.
- `hooks/verify_install.py` — verifies that an install (Codex / Claude / ZCode / OpenCode / generic) landed correctly.
