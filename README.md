# 🪦 Light RIP

Light RIP is an agent skill package, not a software application.

`RIP` names the three safeguards in the skill: `Review`, `Implement`, and `Plan`. In execution, the lightweight loop runs as `Plan -> Implement -> Review`; the name keeps the three pieces memorable without turning them into a heavyweight process.

Light RIP has exactly three tiers:

- `Tiny`: the main session plans, implements, verifies, and self-reviews. No subagents.
- `Medium`: the main session plans, implements, and verifies, then a reviewer subagent reviews the diff. The main session fixes review findings.
- `Large`: planner, implementer, and reviewer are all subagents. The main session coordinates and fixes review findings.

```mermaid
flowchart LR
    A["User prompt"] --> B{"Coding task size?"}
    B -->|"Tiny"| C["Main session: plan + implement + verify + self-review"]
    B -->|"Medium"| D["Main session: plan + implement + verify"]
    D --> E["Reviewer subagent"]
    B -->|"Large"| F["Planner subagent"]
    F --> G["Implementer subagent"]
    G --> H["Reviewer subagent"]
    E --> I["Main session fixes findings"]
    H --> I
    C --> J["Done"]
    I --> K["Final verify"]
    K --> J
```

Risk upgrades the tier: risky tiny becomes medium, risky medium becomes large, and risky large stays large while reviewers focus on the risk area.

Review findings always return to the main session. For nontrivial P1 fixes, do one short reviewer subagent re-review of the changed lines.

```mermaid
flowchart LR
    A["Subagent review"] --> B{"P0/P1 findings?"}
    B -->|"Yes"| C["Main session fixes"]
    C --> D["Relevant verification"]
    D --> E{"Nontrivial P1 fix?"}
    E -->|"Yes"| A
    E -->|"No"| F["Complete"]
    B -->|"No"| F
```

## Installation

Do not install it as an app, service, Python package, or normal project checkout. Install it by placing the `light-rip` folder in your agent's skills directory, then mount the required `UserPromptSubmit` reminder hook.

Installation is incomplete until the hook is mounted.

### Claude Code

Claude Code documentation and community examples use `$HOME/.claude/skills` or `~/.claude/skills`. Claude Code does not define a standard `$CLAUDE_HOME` variable.

Clone directly into the Claude Code skills directory, then run the required hook setup from that installed copy:

```bash
mkdir -p "$HOME/.claude/skills"
git clone https://github.com/x1han/light-rip "$HOME/.claude/skills/light-rip"
cd "$HOME/.claude/skills/light-rip"
python hooks/install_claude_hook.py
```

This writes or updates:

- `~/.claude/settings.json`

Restart Claude Code after installing or updating the skill.

### Codex

If `CODEX_HOME` is unset, Codex normally uses `$HOME/.codex`.

Clone directly into the Codex skills directory, then run the required hook setup from that installed copy:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/x1han/light-rip "$CODEX_HOME/skills/light-rip"
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

This writes or updates:

- `$CODEX_HOME/hooks.json`
- `$CODEX_HOME/config.toml`, ensuring `[features] hooks = true`

Restart Codex after installing or updating the skill.

## Hook Behavior

The required `UserPromptSubmit` hook is non-blocking. For every prompt, it injects `reminder.md` as additional context so the agent remembers:

- factual claims need current evidence
- observations, inferences, and recommendations should stay separate
- tiny code edits do not need Light RIP
- medium and large coding tasks should use Light RIP
- risky tasks should use the stronger review path

## Files

- `SKILL.md`: the skill instructions
- `reminder.md`: the context injected by the hook
- `hooks/light_rip_reminder.py`: the shared hook command
- `hooks/install_codex_hook.py`: Codex hook setup
- `hooks/install_claude_hook.py`: Claude Code hook setup
