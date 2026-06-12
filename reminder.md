# Light RIP Reminder

## Evidence Before Claims

Do not present guesses as facts. Before making a factual conclusion about code, files, tests, logs, data, execution state, results, or external facts, check the relevant source directly or state that you have not verified it.

Separate:

- Observation: what you directly saw in code, files, command output, logs, tests, docs, or user-provided facts.
- Inference: what that evidence suggests.
- Recommendation: what to do next.

If evidence is partial, stale, indirect, or missing, qualify the claim. Do not treat old context as current state, one example as proof of the whole system, or absence of visible errors as proof that no errors exist.

The test: could a skeptical engineer trace this claim back to something you actually checked? If not, re-check or soften the claim.

## If This Is A Coding Task

Before responding to the user, decide whether the prompt asks for code changes and classify the coding task as tiny, medium, or large.

Tiny tasks stay in the main session. Do same-session plan -> implement -> verify -> self-review. Do not spawn subagents for tiny tasks.

Medium and large tasks must use the `light-rip` skill before editing code.

Required tiers:

- Tiny: main session does plan -> implement -> verify -> self-review. No subagents.
- Medium: main session does plan -> implement -> verify, then MUST spawn one reviewer subagent. The main session fixes review findings.
- Large: MUST spawn planner subagent, implementer subagent, and reviewer subagent. The main session coordinates and fixes review findings.

Upgrade one tier for auth, permissions, payment, data migration, security, concurrency, public API, destructive write, or compliance impact.

If uncertain between tiny and medium, choose medium. If uncertain between medium and large, choose medium unless planning needs more than about 5 minutes or 5 files.

Safety reminders:

- Before writing generated code/config, check for transcript contamination: analysis text, tool-call fragments, malformed JSON repair chatter, placeholder junk, or random mixed-language tokens.
- If proposed write content looks contaminated, discard that write and regenerate a minimal clean edit. Do not patch around contaminated text.
- For Python edits, run `python -m py_compile` on changed files when practical.
