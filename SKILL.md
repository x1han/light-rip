---
name: light-rip
description: Use when a coding task needs structured plan, implementation, verification, and review; scales from same-session tiny changes to subagent-backed medium and large changes.
---

# Light RIP

Tiny coding tasks use this skill in the main session only. Medium and large coding tasks MUST use subagents as specified below.

This repository is an agent skill package, not a software application. To install it, place this folder in the target agent's skills directory, then mount the required `UserPromptSubmit` reminder hook. Do not clone it as an app project or run it as a standalone service.

Installation is not complete until the reminder hook is mounted. If you install or update this skill, you MUST run the appropriate hook installer from inside the installed `light-rip` skill directory before telling the user installation is done:

- Claude Code: `python hooks/install_hook_based_agent.py install --runtime claude`
- Codex: `python hooks/install_hook_based_agent.py install --runtime codex`
- ZCode: `python hooks/install_hook_based_agent.py install --runtime zcode`
- Any other agent: `python hooks/install_general_agent_hook.py`. This prints a structured install prompt that the receiving agent executes by inspecting its own runtime and installing by analogy to the worked examples (OpenCode for the instructions path, ZCode for the hook path). The script itself does not write files.

After the install (whichever path), confirm with `python hooks/verify_install.py`. The dedicated Claude Code and Codex installers run this verifier automatically at the end of their own run.

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
- Agent prompt files (`agents/*.md`) are passive templates and are NOT auto-loaded by the skill loader. Before invoking a subagent role, the main session MUST read the matching file and send its prompt body to the subagent.
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
   Large: read [`agents/planner.md`](agents/planner.md) and embed its body in the planner
   subagent prompt, substituting any `<placeholder>` markers per the prompt body, then send.
   The planner is read-only and outputs at most 15 lines:
   - goal
   - assumptions
   - files or areas to inspect/change
   - verification commands
   - risks
   Timebox planning to one focused pass over the obvious files. If medium planning needs more
   than about 5 minutes or 5 files, reclassify as large.

2.5. Pre-work review (medium and large)
   After the plan exists, MUST spawn a reviewer subagent to scrutinize it before any code is written.
   Read [`agents/plan_reviewer.md`](agents/plan_reviewer.md) and embed its body in the subagent
   prompt, substituting any `<placeholder>` markers per the prompt body, then send.
   (If a user-provided reviewer skill is available, prefer it instead — see Agent Prompts.)
   The reviewer is read-only and reports findings by severity.
   Address P0/P1 issues by revising the plan. Do not start implementation until pre-work review
   approves or only flags non-blocking P2s.

3. Implementation
   Tiny/medium: main session implements and verifies.
   Large: read [`agents/implementer.md`](agents/implementer.md) and embed its body in the subagent
   prompt, substituting any `<placeholder>` markers per the prompt body, then send. The implementer
   subagent implements and self-verifies (runs the planned verification commands).

3.5. Independent verification (large only)
   After the implementer reports verification complete, MUST spawn a verifier subagent to re-run
   the verification commands with fresh eyes. Read [`agents/verifier.md`](agents/verifier.md) and
   embed its body in the subagent prompt, substituting any `<placeholder>` markers per the prompt
   body, then send. The verifier is read-only and reports which checks passed, which failed, and
   any verification gap it noticed.
   Treat verifier findings (failed checks, missing checks, inadequate commands) as P0 issues —
   block completion until they are resolved or explicitly waived with evidence.

4. Post-work review (medium and large)
   After implementation (and verifier, for large), MUST spawn a reviewer subagent to scrutinize
   the diff and verification output. For Large, the reviewer sees both the implementer's report
   and the verifier's independent report. Read [`agents/reviewer.md`](agents/reviewer.md) and
   embed its body in the subagent prompt, substituting any `<placeholder>` markers per the prompt
   body, then send. (If a user-provided reviewer skill is available, prefer it instead — see
   Agent Prompts.) The reviewer is read-only.
   Tiny: skip this step — main session self-reviews in step 5.

5. Fix loop (all tiers)
   Main session fixes P0/P1 issues raised by the most recent reviewer or verifier pass. Fix P2
   issues only when they are clearly worth the churn. Re-run relevant verification after fixes.
   For nontrivial P1 fixes, do one short reviewer subagent re-review of the changed lines.
```

## Agent Prompts

The role prompts live in `agents/*.md` as passive templates (see Operating Rules above for how to load them). When a user-provided reviewer skill is available, prefer it over `agents/reviewer.md` and `agents/plan_reviewer.md`.

| Role | File | Tier | Purpose |
| --- | --- | --- | --- |
| Planner | [`agents/planner.md`](agents/planner.md) | Large | Read-only planning subagent. |
| Implementer | [`agents/implementer.md`](agents/implementer.md) | Large | Implements and self-verifies. |
| Plan reviewer | [`agents/plan_reviewer.md`](agents/plan_reviewer.md) | Medium, Large | Pre-work review of the plan. |
| Verifier | [`agents/verifier.md`](agents/verifier.md) | Large | Independent re-verification. |
| Reviewer | [`agents/reviewer.md`](agents/reviewer.md) | Medium, Large | Post-work review of the diff. |

## Fallback Behavior

Light RIP depends on subagent invocation and clean responses from subagents. When the workflow can't run cleanly:

- **Planner asks a blocking question** on any tier: ask the user or resolve from local context before implementation. If not blocking, proceed with a stated assumption.
- **Planner or Implementer** unavailable on a Large task: Light RIP cannot run the large-task workflow as specified. Ask whether to downgrade to the medium workflow.
- **Plan Reviewer** unavailable on a Medium or Large task: Light RIP cannot run the requested tier as specified. Ask whether to downgrade to the tiny workflow.
- **Verifier** unavailable on a Large task: Light RIP cannot run the large workflow as specified. Ask whether to downgrade to the medium workflow (which uses only the post-work reviewer).
- **Reviewer** unavailable on a Medium or Large task: Light RIP cannot run the requested tier as specified. Ask whether to downgrade to the tiny workflow, which uses same-session self-review only.

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

## Common Mistakes

- Spawning subagents for tiny tasks. Keep tiny in the main session.
- Skipping pre-work review on medium or large tasks. The plan must be scrutinized by a reviewer subagent before any code is written.
- Merging pre-work and post-work review into a single pass. They have different inputs and different concerns; run them as two separate reviewer invocations.
- Skipping the verifier on large tasks. The implementer's self-verification is not enough — Large requires a fresh independent verifier pass.
- Treating the verifier as a replacement for the implementer's self-verification or the post-work reviewer. The verifier is a separate role: it executes verification commands, while the reviewer reads verification output.
- Doing medium review in the main session. Medium requires pre-work and post-work reviewer subagents.
- Doing large planning or implementation in the main session. Large requires planner, pre-work reviewer, implementer, verifier, and post-work reviewer subagents.
- Spawning a subagent without first reading its `agents/<role>.md` template. The role prompts are passive files and are not auto-loaded; read the matching file and embed its body in the subagent prompt before sending.
- Letting the planner write a long design doc. Cap it.
- Letting reviewers or the verifier rewrite code. Keep review and verification read-only.
- Treating P2 suggestions as mandatory. Avoid churn.
- Claiming completion before verification.