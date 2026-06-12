---
name: light-rip
description: Use when a coding task needs structured plan, implementation, verification, and review; scales from same-session tiny changes to subagent-backed medium and large changes.
---

# Light RIP

Tiny coding tasks use this skill in the main session only. Medium and large coding tasks MUST use subagents as specified below.

This repository is an agent skill package, not a software application. To install it, place this folder in the target agent's skills directory, then mount the required `UserPromptSubmit` reminder hook. Do not clone it as an app project or run it as a standalone service.

Installation is not complete until the reminder hook is mounted. If you install or update this skill, you MUST run the appropriate hook installer from inside the installed `light-rip` skill directory before telling the user installation is done:

- Codex: `python hooks/install_codex_hook.py`
- Claude Code: `python hooks/install_claude_hook.py`

Do not present hook installation as optional. The hook is part of the default installation.

Light RIP is a lightweight Claude Code/Codex-style coding workflow. It keeps the useful parts of modern agentic coding: plan, implement surgically, verify with commands, then review before claiming completion. It has exactly three tiers: tiny, medium, and large.

## When to Use

Use this skill when the user asks for code changes.

Do not spawn subagents for tiny tasks such as typos, comments, log text, one constant, obvious import cleanup, or other changes where the diff is smaller than the process.

If a task has auth, permissions, payment, data migration, security, concurrency, public API, destructive write, or compliance impact, upgrade it by one tier. A risky tiny task becomes medium; a risky medium task becomes large.

## Operating Rules

- Keep the process smaller than the change. Timebox planning and review.
- Tiny tasks stay in the main session: plan, implement, verify, self-review.
- Medium tasks MUST spawn one reviewer subagent after the main session implements and verifies.
- Large tasks MUST spawn three subagents: planner, implementer, reviewer.
- Prefer the repo's existing patterns over new abstractions.
- Do not write a long spec unless the user asked for one.
- Do not create commits, branches, or docs unless the user asked for them or the repo workflow requires them.
- Reviewer subagents are read-only by default. They report issues; the main session fixes them.
- Before writing generated code or config, check the proposed content for transcript contamination: analysis text, tool-call fragments, malformed JSON repair chatter, placeholder junk, or random mixed-language tokens.
- If proposed write content looks contaminated, discard that write and regenerate a minimal clean edit. Do not try to patch around contaminated text.
- When replacing existing source file content, inspect the proposed replacement for contamination before applying it; prefer minimal patch-style edits when available.
- Verify before saying the work is complete.

## Flow

```text
1. Classify scope
   tiny -> main-session plan + implement + verify + self-review
   medium -> main-session plan + implement + verify, then reviewer subagent
   large -> planner subagent + implementer subagent + reviewer subagent

2. Planning
   Tiny/medium: main session writes 2-5 bullets inline.
   Large: planner subagent is read-only and outputs at most 15 lines:
   - goal
   - assumptions
   - files or areas to inspect/change
   - verification commands
   - risks
   Timebox: one focused pass over the obvious files. If medium planning needs more
   than about 5 minutes or 5 files, reclassify as large.

3. Implementation
   Tiny/medium: main session implements and verifies.
   Large: implementer subagent implements and verifies.

4. Review
   Tiny: main session self-reviews the diff.
   Medium/large: reviewer subagent MUST review the actual diff and verification output.
   Timebox: one focused pass over the diff and verification output.

5. Fix loop
   Main session fixes P0/P1 issues. Fix P2 issues only when they are clearly worth the churn.
   Re-run relevant verification after fixes. For nontrivial P1 fixes, do one short reviewer
   subagent re-review of the changed lines.
```

## Planner Prompt

Use this planner subagent prompt for large tasks. Tiny and medium tasks do planning inline in the main session.

```text
You are the planner for a lightweight coding workflow.

Task:
<user request>

Inspect only the context needed to make a small implementation plan. Do not edit files.

Return:
- Goal
- Assumptions or questions that would block implementation
- Files/areas likely involved
- Implementation steps, max 5 bullets
- Verification commands
- Main risks

Keep the whole answer under 15 lines. If this is a tiny change, say "SKIP LIGHT RIP" and explain in one line.
```

If the planner has a blocking question, ask the user or resolve it from local context before implementation. If the question is not blocking, proceed with a stated assumption.

If subagents are unavailable for a large task, say that Light RIP cannot run the large-task workflow as specified and ask whether to downgrade to the medium workflow.

## Implementer Prompt

Use this implementer subagent prompt for large tasks. Tiny and medium tasks are implemented by the main session.

```text
You are the implementer for a lightweight coding workflow.

Task:
<user request>

Plan:
<planner output>

Rules:
- Make the smallest code change that satisfies the task.
- Match existing style and architecture.
- Do not perform unrelated refactors.
- Add or update focused tests when behavior changes.
- Run the planned verification commands, or explain exactly why they cannot run.
- Before writing generated code or config, check for transcript contamination or malformed tool-call repair text.
- If proposed write content looks contaminated, discard it and regenerate a clean minimal edit.

Return:
- Changed files
- What changed
- Verification commands and results
- Any concerns or follow-up risks
```

If subagents are unavailable for a large task, say that Light RIP cannot run the large-task workflow as specified and ask whether to downgrade to the medium workflow.

## Reviewer Prompt

Medium and large tasks MUST spawn a reviewer subagent. Use an existing user-provided reviewer skill when available. If no reviewer skill is available, use this Light RIP reviewer prompt. The reviewer is read-only:

```text
You are the independent reviewer for a lightweight coding workflow.

Review the diff against the original user request and the planner output. Do not edit files.

Focus on:
- requirement mismatches
- bugs or regressions
- missing or weak verification
- generated-content contamination, including analysis text, tool-call repair chatter, malformed JSON fragments, placeholder junk, or random mixed-language tokens
- risky overengineering or unrelated changes

Report findings by severity:
- P0 blocks completion
- P1 should fix before completion
- P2 worth considering, but not required for small tasks

If there are no real issues, say "Approved" and mention any residual test gap.
```

If subagents are unavailable for a medium or large task, say that Light RIP cannot run the requested tier as specified. Ask whether to downgrade to the tiny workflow, which uses same-session self-review only.

## Completion Criteria

Complete only when:

- The requested behavior is implemented.
- Relevant verification has run and passed, or the limitation is clearly reported.
- Written code/config changes have been checked for transcript/tool-call contamination.
- Tiny: same-session self-review has run.
- Medium/large: required subagent review has run.
- P0/P1 review findings are fixed or explicitly judged inapplicable with evidence.
- The final response names the main files changed and verification performed.

## Required Reminder Hook

This skill includes a required reminder hook that runs on `UserPromptSubmit`. It does not block prompts. For every prompt, it injects `reminder.md` as additional context so the agent remembers to apply evidence discipline before factual claims. The same reminder also tells the agent to classify coding tasks and use the required Light RIP subagent tiers for medium and large code changes.

This is a skill setup step, not software installation. First place the `light-rip` folder in the target skills directory, then run the matching hook installer from that installed folder.

Codex skill location:

```text
$CODEX_HOME/skills/light-rip
```

Claude Code skill location:

```text
~/.claude/skills/light-rip
```

Codex hook setup:

```bash
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

This updates `$CODEX_HOME/hooks.json` and ensures `[features] hooks = true` in `$CODEX_HOME/config.toml`.

Claude Code hook setup:

```bash
cd ~/.claude/skills/light-rip
python hooks/install_claude_hook.py
```

This updates `~/.claude/settings.json` by adding a `UserPromptSubmit` command hook.

Codex agents installing this skill from GitHub should do both steps:

```bash
# after copying the repo contents to $CODEX_HOME/skills/light-rip
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

Claude Code agents installing this skill from GitHub should do both steps:

```bash
# after copying the repo contents to ~/.claude/skills/light-rip
cd ~/.claude/skills/light-rip
python hooks/install_claude_hook.py
```

## Common Mistakes

- Spawning subagents for tiny tasks. Keep tiny in the main session.
- Doing medium review in the main session. Medium requires a reviewer subagent.
- Doing large planning or implementation in the main session. Large requires planner, implementer, and reviewer subagents.
- Letting the planner write a long design doc. Cap it.
- Letting reviewers rewrite code. Keep review read-only.
- Treating P2 suggestions as mandatory. Avoid churn.
- Claiming completion before verification.
