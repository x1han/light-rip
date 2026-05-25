---
name: light-rip
description: Use when a coding task changes behavior, touches multiple files, affects public APIs, or carries moderate implementation risk.
---

# Light RIP

Tiny code changes do not need to call this skill; medium and large coding tasks do. Use it when a task changes behavior, touches multiple files, affects public APIs, or carries enough risk that a second look would catch mistakes.

Installation is not complete until the `UserPromptSubmit` reminder hook is mounted. If you install or update this skill, you MUST run the appropriate hook installer before telling the user installation is done:

- Codex: `python hooks/install_codex_hook.py`
- Claude Code: `python hooks/install_claude_hook.py`

Do not present hook installation as optional. The hook is part of the default installation.

Light RIP is a lightweight Claude Code-style coding workflow. It keeps the useful parts of modern agentic coding: understand first, make a small plan, implement surgically, verify with commands, then use an independent review pass before claiming completion.

## When to Use

Use this skill when the task changes behavior, touches multiple files, affects public APIs, or carries enough risk that a second look would catch mistakes.

Do not use it for tiny edits such as typos, comments, log text, one constant, obvious import cleanup, or other changes where the diff is smaller than the process.

If the task is safety-critical, security-sensitive, payment/auth/data-migration related, or spans roughly four or more modules, use this workflow but upgrade the review step to two reviewers: one spec reviewer and one code reviewer.

## Operating Rules

- Keep the process smaller than the change. Timebox planning and review.
- For medium tasks, keep planning inline in the main agent and use one independent reviewer.
- Use a planner subagent only for large or risky tasks where a separate planning pass saves time.
- Prefer the repo's existing patterns over new abstractions.
- Do not write a long spec unless the user asked for one.
- Do not create commits, branches, or docs unless the user asked for them or the repo workflow requires them.
- Reviewer agents are read-only by default. They report issues; the implementer or main agent fixes them.
- Verify before saying the work is complete.

## Flow

```text
1. Classify scope
   tiny -> skip this skill
   medium -> main-agent short plan + implementation + reviewer
   large -> planner + implementer + reviewer
   risky -> planner + implementer + spec reviewer + code reviewer

2. Planning pass
   Medium tasks: main agent writes 2-5 bullets inline.
   Large/risky tasks: planner is read-only and outputs at most 15 lines:
   - goal
   - assumptions
   - files or areas to inspect/change
   - verification commands
   - risks
   Timebox: one focused pass over the obvious files. If planning needs more than
   about 5 minutes or 5 files, reclassify as large/risky or ask a blocking question.

3. Implementer pass
   Follow the plan, keep changes surgical, add or update focused tests when behavior changes,
   then run the planned verification.

4. Review pass, read-only
   Review the actual diff and verification output. Report only real issues with severity.
   Timebox: one focused pass over the diff and verification output.

5. Fix loop
   Fix P0/P1 issues. Fix P2 issues only when they are clearly worth the churn.
   Re-run relevant verification after fixes. For risky tasks or nontrivial P1 fixes,
   do one short re-review of the changed lines.
```

## Planner Prompt

Use a planner subagent for large or risky tasks. For medium tasks, the main agent writes the same plan inline in 2-5 bullets:

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

If subagents are unavailable, the main agent performs this planning step directly while preserving the same line and time limits.

## Implementer Prompt

Use an implementer subagent when the plan is clear and the task can be done mostly independently:

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

Return:
- Changed files
- What changed
- Verification commands and results
- Any concerns or follow-up risks
```

If subagents are unavailable, the main agent performs this step directly while preserving the same constraints.

## Reviewer Prompt

Use one reviewer for medium and large tasks. The reviewer is read-only:

```text
You are the independent reviewer for a lightweight coding workflow.

Review the diff against the original user request and the planner output. Do not edit files.

Focus on:
- requirement mismatches
- bugs or regressions
- missing or weak verification
- risky overengineering or unrelated changes

Report findings by severity:
- P0 blocks completion
- P1 should fix before completion
- P2 worth considering, but not required for small tasks

If there are no real issues, say "Approved" and mention any residual test gap.
```

For risky tasks, run two read-only reviewers:

- `spec-reviewer`: checks only whether the diff satisfies the request and avoids extra scope.
- `code-reviewer`: checks correctness, maintainability, edge cases, and test coverage.

If subagents are unavailable, the main agent performs a separate review pass after implementation. Keep it read-only until findings are listed, then fix only the selected issues.

## Completion Criteria

Complete only when:

- The requested behavior is implemented.
- Relevant verification has run and passed, or the limitation is clearly reported.
- P0/P1 review findings are fixed or explicitly judged inapplicable with evidence.
- The final response names the main files changed and verification performed.

## Reminder Hook

This skill includes a required reminder hook that runs on `UserPromptSubmit`. It does not block prompts. For likely coding requests, it injects `reminder.md` as additional context so the agent remembers to classify the task and use Light RIP for medium or large code changes.

The default install path is:

```bash
python install.py
```

That copies or refreshes the skill under the local skills directory and mounts the appropriate hook. Use the platform-specific commands below only when you already installed the skill files and need to repair the hook.

Claude Code hook repair:

```bash
python hooks/install_claude_hook.py
```

This updates `~/.claude/settings.json` by adding a `UserPromptSubmit` command hook.

Codex hook repair:

```bash
python hooks/install_codex_hook.py
```

This updates `$CODEX_HOME/hooks.json` and ensures `[features] hooks = true` in `$CODEX_HOME/config.toml`.

## Common Mistakes

- Using this for tiny edits. Skip it.
- Letting the planner write a long design doc. Cap it.
- Letting reviewers rewrite code. Keep review read-only.
- Treating P2 suggestions as mandatory. Avoid churn.
- Claiming completion before verification.
