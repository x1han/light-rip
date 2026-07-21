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

## Dependencies

The dedicated Codex installer (`hooks/install_codex_hook.py`) needs a TOML library at runtime to read and rewrite `~/.codex/config.toml` safely:

  - `tomllib` — stdlib on Python 3.11+.
  - `tomli` — backport for Python 3.10 and earlier.
  - `tomli_w` — required for **writing**. Not in stdlib on any Python version.

On Python 3.11+ install only `tomli_w`: `pip install tomli-w`.
On Python 3.10 and earlier install both: `pip install tomli tomli-w`.

If `tomli_w` is missing, the installer aborts before touching any backup with `error=invalid_runtime_config` and the hint `install tomli_w with \`pip install tomli-w\``.

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

Pass `--runtime <name>` (`claude`, `codex`, `zcode`, or `all`) to
scope the verifier to a single runtime. The default `--runtime all`
is lenient: a runtime config file that does not yet exist contributes
0 (the user has not chosen that runtime). In `--runtime <name>`
strict mode, a missing config for the named runtime contributes 1
to the exit code. The verifier covers **Claude Code, Codex, and
ZCode** explicitly. Other runtimes (OpenCode, …) install via the
general prompt and are not actively verified here — re-read the
prompt's worked examples and confirm the script-side smoke checks
above pass after install.

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

#### Backups and atomic writes

The dedicated installers (`install_claude_hook.py`,
`install_codex_hook.py`) back up any config file they touch to
`<path>.bak-YYYY-MM-DD` (UTC) before writing. A same-day backup
collision aborts with `error=backup_collision`; pass
`--force-backup` to overwrite or rename the existing backup. The
installer parses the source first and refuses to back up a corrupt
config (`error=invalid_runtime_config`) — a recovery snapshot from
known-bad content would later be overwritten by `--force-backup`
into a "good" backup that is actually corrupt.

Writes are atomic: text is staged in a sibling temp file, fsynced,
then renamed over the destination via `os.replace`. A crash
mid-write leaves the previous config intact instead of producing a
half-written JSON file. Filesystem-level failures (permission
denied, locked file, …) surface as `error=permission_or_io_error`,
distinct from `backup_collision` so the user can tell "you have a
choice" from "I literally cannot write here".

The same parse-before-backup invariant applies to Codex's
`config.toml`: the installer reads and parses the file first, and
only backs it up once it knows the source is good. Re-running the
installer on an already-correct `config.toml` (where `[features]
hooks` is already truthy per Codex's own acceptance rules — TOML
bool `true` or the case-insensitive string `"true"`) is a no-op:
the file is left byte-equal so its comments and formatting survive.

When the installer does need to rewrite `config.toml`, the rewrite
goes through `tomli_w`, which does not preserve original comments
or formatting. The single backup at `<config.toml>.bak-YYYY-MM-DD`
is your recovery path if the rewrite dropped something you wanted
back. Idempotent re-installs never reach the rewrite path.

#### ZCode detector: strict Python-launcher predicate

`verify_install.py`'s ZCode branch matches the runtime hook command
against a strict regex anchored on the **full filename**
(`cmd_path.name`, not `.stem`) of PEP 394 + PEP 397 blessed names:

  - Accepts: `python`, `python.exe`, `python2`, `python2.exe`,
    `python3`, `python3.12`, `python3.12.exe`, `py`, `py.exe`,
    `py3`, `py3.exe`.
  - Rejects: `pythonw`, `pythonw.exe` (GUI launcher, no console);
    `python_d`, `pythonw_d` (debug builds); `mypython`,
    `python3.12-config`, `python3.12-config.exe`, `pyfoo`,
    `python.exe.bak` (anything that starts with `python` or `py`
    but is not a real interpreter).

The earlier predicate (`cmd.endswith(".exe") or "python" in cmd`)
matched every Windows process path. The full-filename anchor is
also what rejects `python3.12-config.exe`: on Windows,
`Path("python3.12-config.exe").stem` reduces to `python3.12`, so
checking `.stem` would falsely accept the dev tool as an
interpreter.

## Updating

`cd` into the installed `light-rip` folder, run `git pull`, then
re-run the installer that matches your agent (see Installation).


## Files

- `SKILL.md` — the skill instructions.
- `reminder.md` — the context injected by the hook. Runtime-neutral.
- `hooks/light_rip_reminder.py` — the shared hook command. The default `--format harness` emits a `hookSpecificOutput.additionalContext` envelope that most runtimes accept; an alternative format is also shipped for runtimes that rewrite the prompt instead.
- `hooks/installer_common.py` — shared helpers for the dedicated installers: date-stamped backups (`<path>.bak-YYYY-MM-DD` UTC, abort-on-collision), atomic writes (temp + fsync + rename), safe JSON load, and dedup-all upsert.
- `hooks/install_codex_hook.py` — Codex-specific installer (auto-writes `~/.codex/hooks.json` and enables `[features] hooks = true` in `~/.codex/config.toml` via `tomllib` + `tomli_w`; requires `pip install tomli-w`).
- `hooks/install_claude_hook.py` — Claude Code-specific installer (auto-writes `~/.claude/settings.json`).
- `hooks/install_general_agent_hook.py` — generic installer for any other agent runtime. Prints an install prompt; does not write files itself.
- `hooks/verify_install.py` — verifies that an install landed correctly. Actively checks Claude Code, Codex, and ZCode; other runtimes install via the general prompt and are not actively verified here.
