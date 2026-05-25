# 🪦 Light RIP

Light RIP is an agent skill package, not a software application.

`RIP` names the three safeguards in the skill: `Review`, `Implement`, and `Plan`. In execution, the lightweight loop runs as `Plan -> Implement -> Review`; the name keeps the three pieces memorable without turning them into a heavyweight process. Tiny code edits skip the loop.

```mermaid
flowchart LR
    A["User prompt"] --> B{"Coding task size?"}
    B -->|"Tiny edit"| C["Skip Light RIP"]
    B -->|"Medium"| D["Plan inline"]
    B -->|"Large or risky"| E["Planner pass"]
    D --> F["Implement"]
    E --> F
    F --> G["Review"]
    G --> H{"P0/P1 issues?"}
    H -->|"Yes"| I["Fix + verify"]
    I --> G
    H -->|"No"| J["Done"]
```

Do not install it as an app, service, Python package, or normal project checkout. Install it by placing the `light-rip` folder in your agent's skills directory, then mount the required `UserPromptSubmit` reminder hook.

Installation is incomplete until the hook is mounted.

## Codex

Clone directly into the Codex skills directory, then run the required hook setup from that installed copy:

```bash
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/x1han/light-rip "$CODEX_HOME/skills/light-rip"
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

This writes or updates:

- `$CODEX_HOME/hooks.json`
- `$CODEX_HOME/config.toml`, ensuring `[features] hooks = true`

Restart Codex after installing or updating the skill.

## Claude Code

Clone directly into the Claude Code skills directory, then run the required hook setup from that installed copy:

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/x1han/light-rip ~/.claude/skills/light-rip
cd ~/.claude/skills/light-rip
python hooks/install_claude_hook.py
```

This writes or updates:

- `~/.claude/settings.json`

Restart Claude Code after installing or updating the skill.

## What The Hook Does

The hook runs on `UserPromptSubmit`. It does not block prompts.

For likely coding requests, it injects `reminder.md` as additional context so the agent remembers:

- tiny code edits do not need Light RIP
- medium and large coding tasks should use Light RIP
- risky tasks should use the stronger review path

For non-coding prompts, it stays quiet.

## Files

- `SKILL.md`: the skill instructions
- `reminder.md`: the context injected by the hook
- `hooks/light_rip_reminder.py`: the shared hook command
- `hooks/install_codex_hook.py`: Codex hook setup
- `hooks/install_claude_hook.py`: Claude Code hook setup
