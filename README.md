# 🪦 Light RIP

Light RIP is an agent skill package, not a software application.

`RIP` names the three safeguards: `Review`, `Implement`, and `Plan`. In execution, every tier runs `Plan -> Implement -> Verify` — a lightweight loop with extra review passes for medium and large.

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

### Codex

If `CODEX_HOME` is unset, Codex normally uses `$HOME/.codex`. Clone into the Codex skills directory and run the required hook setup from the installed copy:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/x1han/light-rip "$CODEX_HOME/skills/light-rip"
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

### Claude Code

Claude Code documentation and community examples use `$HOME/.claude/skills` or `~/.claude/skills`. Clone into the Claude Code skills directory and run the required hook setup from the installed copy:

```bash
mkdir -p "$HOME/.claude/skills"
git clone https://github.com/x1han/light-rip "$HOME/.claude/skills/light-rip"
cd "$HOME/.claude/skills/light-rip"
python hooks/install_claude_hook.py
```

### Other agents (Mavis / Mavis Code)

For any agent runtime that is not Claude Code or Codex, use the general installer. Defaults target Mavis; override flags install for any runtime that uses the same hook-file convention.

```bash
git clone https://github.com/x1han/light-rip
cd light-rip
python hooks/install_general_agent_hook.py
```

#### Windows: Git Bash PATH

The Mavis hook runner executes the body of a fenced `bash` block through `sh` on Windows. Git for Windows ships `sh.exe` at `C:\Program Files\Git\bin\sh.exe`, but it is not on `PATH` by default. The installer detects this and appends Git Bash to the user's `PATH` via the registry (with a `SendMessageTimeout` broadcast so new processes pick it up immediately).

The currently running Mavis daemon keeps the PATH it was launched with, so restart the agent runtime after installing so the new PATH takes effect. If you prefer to manage PATH yourself, pass `--no-path-fix` to the installer and ensure `sh` is on the runtime's PATH beforehand.

## Updating

`cd` into the installed `light-rip` folder, run `git pull`, then re-run the installer that matches your agent (see Installation). Restart the agent runtime after.

## Files

- `SKILL.md` — the skill instructions.
- `reminder.md` — the context injected by the hook.
- `hooks/light_rip_reminder.py` — the shared hook command. Multi-format via `--format` (`harness` for Claude Code / Codex back-compat; `mavis` for Mavis).
- `hooks/install_codex_hook.py` — Codex-specific installer.
- `hooks/install_claude_hook.py` — Claude Code-specific installer.
- `hooks/install_general_agent_hook.py` — generic installer for any other agent runtime.