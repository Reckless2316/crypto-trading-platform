# HANDOFF — for Cursor / next agent

Signed: `HANDOFF(claude-opus-5.5 2026-10-17)`. Search the tree for `HANDOFF(` to find inline markers.

## State at handoff
- Branch `feat/autonomous-crypto-agent`. Commit `32a72bc` checkpoints Codex's paper-reconciliation WIP. The full suite was 582 tests OK on Python 3.14 (`.venv/bin/python -m unittest discover -s tests`).
- History: `.agenkit/sdd/progress.md` (newest entries at the top), `master-context.md` (Task 13–22 gap table), `docs/checkpoints/`.
- Still uncommitted, operator-owned: `dashboard/`, `deploy/deploy.sh`, `bot/live_coinbase_adapter.py`, `tests/test_live_adapter.py`, `tests/test_platform_api.py`. Preserve them; review before committing.

## Done (Claude session), see `docs/checkpoints/2026-10-17-claude-paper-runtime.md`
- `bot/autonomous/runtime.py`: the paper loop, reviewed twice; all findings fixed. Running locally, unarmed, on `data/autonomous/runtime/`.
- `GET /api/autonomous/runtime` read model, plus `deploy/autonomous/` systemd units (not installed).

## Next for Cursor (in order)
1. Jev adapter is wired (`bot/autonomous/jev_reflex.py`); next is an authenticated evaluation with the operator's key and approved schema, plus tokenizer proof.
2. Route runtime paper orders through OrderAuthority/Ledger (Tasks 12/16).
3. Verified MarketRules; labels that include spread and slippage.
4. Latch clearing and kill via the authenticated control socket (Tasks 14/17).

## Hard rules (do not relax)
- No live orders or arming. Live progression requires Tasks 13/14/15/20/21 and explicit operator approval (Task 22).
- Code owns every threshold, size and side effect. The model only judges.
- Below 100 resolved labels per cohort, the calibrated probability is a prior with zero Kelly, so no paper entries occur. That outcome is correct, not a bug.
