# Light RIP Reminder

Before responding to the user, decide whether the prompt asks for code changes.

If the prompt asks for a tiny code change, do not use Light RIP. Tiny changes include typos, comments, log text, one constant, obvious import cleanup, or similarly small edits where process would be larger than the diff.

If the prompt asks for a medium or large code change, use the `light-rip` skill before editing code. This includes behavior changes, multi-file changes, public API changes, feature work, bug fixes, refactors, or anything with moderate implementation risk.

Default classification:

- Tiny: edit directly and verify as appropriate.
- Medium: use Light RIP with a main-agent short plan, implementation, verification, and one independent review pass.
- Large: use Light RIP with planner, implementer, verification, and reviewer.
- Risky: use Light RIP with planner, implementer, spec reviewer, and code reviewer.

If uncertain, choose the lighter safe path: make a 2-5 bullet plan and use one independent reviewer after implementation.
