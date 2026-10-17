# AgenKit

This project uses **AgenKit** — a specialist-driven engineering workflow. Run `/agenkit`
to start the guided pipeline (Orient → Brainstorm → Architecture → Plan → Build → Review
→ Ship). Invoke a relevant AgenKit skill before acting; the `using-agenkit` skill explains
how the skills and specialist agents fit together.

## Query local memory first

Before reading or grepping source to locate code, consult AgenKit local memory:

- Run `agenkit memory status` (then `agenkit memory build` if it's missing, or
  `agenkit memory update` if it reports drift).
- Ask `agenkit memory query "<question>"` for scoped `file:line` hits, or
  `agenkit memory explain "<Symbol>"` for where a symbol is defined and how it connects.
- Open only the exact `file:line` the memory points to — not the whole file, and not a
  broad grep, unless the memory genuinely lacks the answer.

If the `agenkit` CLI is unavailable, read `.agenkit/memory/MEMORY.md` first, then fall
back to targeted reads. See the `codebase-memory` skill for the full workflow.
