# HANDOFF — for Cursor / next agent

Signed: `HANDOFF(claude-opus-5.5 2026-10-17)`. Search the tree for `HANDOFF(` to find inline markers.

## State at handoff
- Branch `feat/autonomous-crypto-agent`. Commit `32a72bc` checkpoints Codex's paper-reconciliation WIP. The full suite was 582 tests OK on Python 3.14 (`.venv/bin/python -m unittest discover -s tests`).
- History: `.agenkit/sdd/progress.md` (newest entries at the top), `master-context.md` (Task 13–22 gap table), `docs/checkpoints/`.
- Still uncommitted, operator-owned: `dashboard/`, `deploy/deploy.sh`, `bot/live_coinbase_adapter.py`, `tests/test_live_adapter.py`, `tests/test_platform_api.py`. Preserve them; review before committing.

## In progress (Claude session)
1. `bot/autonomous/runtime.py`: the end-to-end PAPER loop. Public books → causal snapshot (<400 tokens) → REFLEX (Jev, or the labeled offline reflex) → code gate → quarter-Kelly sizing → hard risk/kill file → adverse paper fill → labels/Brier → `status.json`. Run it with `python -m bot.autonomous.runtime --paper --once`.
2. Next: point the dashboard at the runtime's `status.json` read model (Task 17, read-only), then the nightly cron entry.

## Hard rules (do not relax)
- No live orders or arming. Live progression requires Tasks 13/14/15/20/21 and explicit operator approval (Task 22).
- Code owns every threshold, size and side effect. The model only judges.
- Below 100 resolved labels per cohort, the calibrated probability is a prior with zero Kelly, so no paper entries occur. That outcome is correct, not a bug.
