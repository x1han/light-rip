---
name: light-rip
description: Use when a coding task needs structured plan, implementation, verification, and review; scales from same-session tiny changes to subagent-backed medium and large changes.
---

# Light RIP

Tiny coding tasks use this skill in the main session only. Medium and large coding tasks MUST use subagents as specified below.

This repository is an agent skill package, not a software application. To install it, place this folder in the target agent's skills directory, then mount the required `UserPromptSubmit` reminder hook. Do not clone it as an app project or run it as a standalone service.

Installation is not complete until the reminder hook is mounted. If you install or update this skill, you MUST run the appropriate hook installer from inside the installed `light-rip` skill directory before telling the user installation is done:

- Claude Code: `python hooks/install_claude_hook.py`
- Codex: `python hooks/install_codex_hook.py`
- Any other agent: `python hooks/install_general_agent_hook.py`. This prints a structured install prompt that the receiving agent executes by inspecting its own runtime and installing by analogy to the worked examples (OpenCode for the instructions path, ZCode for the hook path). The script itself does not write files.

After the install (whichever path), confirm with `python hooks/verify_install.py --agent <runtime-name>`.

Do not present hook installation as optional. The hook is part of the default installation.

Light RIP is a lightweight Claude Code / Codex-style coding workflow. It keeps the useful parts of modern agentic coding: plan, implement surgically, verify with commands, then review before claiming completion. It has exactly three tiers: tiny, medium, and large.

## When to Use

Use this skill when the user asks for code changes.

Do not spawn subagents for tiny tasks such as typos, comments, log text, one constant, obvious import cleanup, or other changes where the diff is smaller than the process.

If a task has auth, permissions, payment, data migration, security, concurrency, public API, destructive write, or compliance impact, upgrade it by one tier. A risky tiny task becomes medium; a risky medium task becomes large.

## Operating Rules

- Keep the process smaller than the change. Timebox planning and review.
- Tiny tasks stay in the main session: plan, implement, verify, self-review. No subagents.
- Medium tasks MUST spawn a reviewer subagent twice: once for pre-work review of the inline plan, and once for post-work review of the diff. The main session implements and verifies between the two passes.
- Large tasks MUST spawn a planner subagent, a pre-work reviewer subagent, an implementer subagent (which self-verifies), a verifier subagent (which independently re-runs verification), and a post-work reviewer subagent. The main session coordinates and fixes review findings.
- Pre-work review and post-work review are separate passes with different inputs and different concerns. Do not merge them. Do not skip pre-work review on Medium or Large.
- The verifier (Large only) is not a replacement for the implementer's self-verification; it is an independent second look. Run it after the implementer reports verification complete.
- Prefer the repo's existing patterns over new abstractions.
- Do not write a long spec unless the user asked for one.
- Do not create commits, branches, or docs unless the user asked for them or the repo workflow requires them.
- Reviewer and verifier subagents are read-only by default. They report issues or results; the main session fixes them.
- Before writing generated code or config, check the proposed content for transcript contamination: analysis text, tool-call fragments, malformed JSON repair chatter, placeholder junk, or random mixed-language tokens.
- If proposed write content looks contaminated, discard that write and regenerate a minimal clean edit. Do not try to patch around contaminated text.
- When replacing existing source file content, inspect the proposed replacement for contamination before applying it; prefer minimal patch-style edits when available.
- Verify before saying the work is complete.

## Flow

```text
1. Classify scope
   tiny -> main-session plan + implement + verify + self-review
   medium -> main-session plan
             -> reviewer subagent: pre-work review of plan
             -> main-session implement + verify
             -> reviewer subagent: post-work review of diff
   large  -> planner subagent: plan
             -> reviewer subagent: pre-work review of plan
             -> implementer subagent: implement + self-verify
             -> verifier subagent: independent re-verification
             -> reviewer subagent: post-work review of diff

2. Planning
   Tiny: 1-2 inline bullets.
   Medium: main session writes 2-5 bullets inline.
   Large: planner subagent is read-only and outputs at most 15 lines:
   - goal
   - assumptions
   - files or areas to inspect/change
   - verification commands
   - risks
   Timebox planning to one focused pass over the obvious files. If medium planning needs more
   than about 5 minutes or 5 files, reclassify as large.

2.5. Pre-work review (medium and large)
   After the plan exists, MUST spawn a reviewer subagent to scrutinize it before any code is written.
   Use the Plan Reviewer Prompt below. The reviewer is read-only and reports findings by severity.
   Address P0/P1 issues by revising the plan. Do not start implementation until pre-work review
   approves or only flags non-blocking P2s.

3. Implementation
   Tiny/medium: main session implements and verifies.
   Large: implementer subagent implements and self-verifies (runs the planned verification commands).

3.5. Independent verification (large only)
   After the implementer reports verification complete, MUST spawn a verifier subagent to re-run
   the verification commands with fresh eyes. Use the Verifier Prompt below. The verifier is
   read-only and reports which checks passed, which failed, and any verification gap it noticed.
   Treat verifier findings (failed checks, missing checks, inadequate commands) as P0 issues —
   block completion until they are resolved or explicitly waived with evidence.

4. Post-work review (medium and large)
   After implementation (and verifier, for large), MUST spawn a reviewer subagent to scrutinize
   the diff and verification output. For Large, the reviewer sees both the implementer's report
   and the verifier's independent report. Use the Reviewer Prompt below. The reviewer is read-only.
   Tiny: skip this step — main session self-reviews in step 5.

5. Fix loop (all tiers)
   Main session fixes P0/P1 issues raised by the most recent reviewer or verifier pass. Fix P2
   issues only when they are clearly worth the churn. Re-run relevant verification after fixes.
   For nontrivial P1 fixes, do one short reviewer subagent re-review of the changed lines.
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

## Plan Reviewer Prompt

Use this prompt for the pre-work reviewer subagent on Medium and Large tasks. This pass reviews the plan only; do not use it for the post-work diff review.

```text
You are the independent plan reviewer for a lightweight coding workflow.

User request:
<original user prompt>

Plan to review:
<main-session inline plan OR planner subagent output>

Do not edit files. Do not implement. Read-only review of the plan only.

Focus on:
- Does the plan address the right problem? Does it match the user's request?
- Are the listed files and areas actually the right ones to change? Any obvious miss?
- Is the verification approach sound? Will it actually catch a regression?
- Are the assumptions stated, or are there hidden ones that block implementation?
- Is there a clearly simpler or safer approach the plan missed?

Report findings by severity:
- P0 blocks implementation (wrong target, missing dependency, unverifiable)
- P1 should be addressed before implementation
- P2 worth considering, but not blocking

If the plan is sound, say "Approved" and mention any residual risk the implementer should watch for.
```

If subagents are unavailable for the pre-work review on a medium or large task, say that Light RIP cannot run the requested tier as specified. Ask whether to downgrade to the tiny workflow.

## Verifier Prompt

Large tasks MUST spawn a verifier subagent after the implementer reports verification complete. The verifier independently re-runs the verification commands and reports what actually passes. Do not use this prompt for Medium — Medium does not have a verifier step.

```text
You are the independent verifier for a lightweight coding workflow.

User request:
<original user prompt>

Plan to verify against:
<main-session inline plan OR planner subagent output>

Implementer's verification report:
<implementer subagent's verification output>

Do not edit files. Do not implement. Read-only verification only.

Run the verification commands from the plan yourself, in a fresh context. Do not trust the
implementer's report — your job is to independently confirm whether the planned checks actually
pass on the current state of the code.

Report:
- Which verification commands you ran
- Which ones passed, which ones failed, which ones you could not run (and why)
- Any gap you noticed: tests that should have been run but were not, edge cases the implementer's
  verification missed, or verification commands that look inadequate for catching regressions in this change
- Whether the implementation matches the user's request from a behavioral standpoint (you do not need
  to read every file, but spot-check the changed areas)

If all planned checks pass and no gaps are obvious, say "Verified" and mention any residual concern
the reviewer should follow up on.
```

If subagents are unavailable for the verifier step on a large task, say that Light RIP cannot run the large workflow as specified. Ask whether to downgrade to the medium workflow (which uses only the post-work reviewer).

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
- Medium: pre-work reviewer subagent has run on the inline plan, and post-work reviewer subagent has run on the diff.
- Large: pre-work reviewer subagent has run on the planner output, implementer subagent has implemented and self-verified, verifier subagent has independently re-verified, and post-work reviewer subagent has run on the diff and verification output.
- P0/P1 review findings are fixed or explicitly judged inapplicable with evidence.
- The final response names the main files changed and verification performed.

## Required Reminder Hook

This skill includes a required reminder hook that runs on `UserPromptSubmit`. It does not block prompts. For every prompt, it injects `reminder.md` as additional context so the agent remembers to apply evidence discipline before factual claims. The same reminder also tells the agent to classify coding tasks and use the required Light RIP subagent tiers for medium and large code changes.

This is a skill setup step, not software installation. First place the `light-rip` folder in the target skills directory, then run the matching hook installer from that installed folder.

Claude Code skill location:

```text
~/.claude/skills/light-rip
```

Codex skill location:

```text
$CODEX_HOME/skills/light-rip
```

Claude Code hook setup:

```bash
cd ~/.claude/skills/light-rip
python hooks/install_claude_hook.py
```

This updates `~/.claude/settings.json` by adding a `UserPromptSubmit` command hook.

Codex hook setup:

```bash
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

This updates `$CODEX_HOME/hooks.json` and ensures `[features] hooks = true` in `$CODEX_HOME/config.toml`.

Claude Code agents installing this skill from GitHub should do both steps:

```bash
# after copying the repo contents to ~/.claude/skills/light-rip
cd ~/.claude/skills/light-rip
python hooks/install_claude_hook.py
```

Codex agents installing this skill from GitHub should do both steps:

```bash
# after copying the repo contents to $CODEX_HOME/skills/light-rip
cd "$CODEX_HOME/skills/light-rip"
python hooks/install_codex_hook.py
```

## Common Mistakes

- Spawning subagents for tiny tasks. Keep tiny in the main session.
- Skipping pre-work review on medium or large tasks. The plan must be scrutinized by a reviewer subagent before any code is written.
- Merging pre-work and post-work review into a single pass. They have different inputs and different concerns; run them as two separate reviewer invocations.
- Skipping the verifier on large tasks. The implementer's self-verification is not enough — Large requires a fresh independent verifier pass.
- Treating the verifier as a replacement for the implementer's self-verification or the post-work reviewer. The verifier is a separate role: it executes verification commands, while the reviewer reads verification output.
- Doing medium review in the main session. Medium requires pre-work and post-work reviewer subagents.
- Doing large planning or implementation in the main session. Large requires planner, pre-work reviewer, implementer, verifier, and post-work reviewer subagents.
- Letting the planner write a long design doc. Cap it.
- Letting reviewers or the verifier rewrite code. Keep review and verification read-only.
- Treating P2 suggestions as mandatory. Avoid churn.
- Claiming completion before verification.
