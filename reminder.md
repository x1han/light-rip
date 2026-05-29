# Light RIP Reminder

Before responding to the user, decide whether the prompt asks for code changes and classify the coding task as tiny, medium, or large.

Tiny tasks stay in the main session. Do same-session plan -> implement -> verify -> self-review. Do not spawn subagents for tiny tasks.

Medium and large tasks must use the `light-rip` skill before editing code.

Required tiers:

- Tiny: main session does plan -> implement -> verify -> self-review. No subagents.
- Medium: main session does plan -> implement -> verify, then MUST spawn one reviewer subagent. The main session fixes review findings.
- Large: MUST spawn planner subagent, implementer subagent, and reviewer subagent. The main session coordinates and fixes review findings.

Upgrade one tier for auth, permissions, payment, data migration, security, concurrency, public API, destructive write, or compliance impact.

If uncertain between tiny and medium, choose medium. If uncertain between medium and large, choose medium unless planning needs more than about 5 minutes or 5 files.
