# HANDOFF — for Cursor / next agent

Signed `HANDOFF(claude-opus-5.5 2026-10-17)`, updated 2026-10-18. Search the tree for `HANDOFF(` to find inline markers.

## State
- Branch `feat/autonomous-crypto-agent`. Suite: `.venv/bin/python -m unittest discover -s tests` passed with 905 tests at `27af3eb`. Network-guard run: `PYTHONPATH=deploy/autonomous/ci SURGE_BLOCK_NETWORK=1`.
- Running locally (paper): `data/autonomous/runtime` (unarmed) and `data/autonomous/runtime-demo-armed` (armed paper, tiny exploration). Stop either with `touch <dir>/STOP`.
- Full narrative: `docs/checkpoints/2026-10-17-claude-paper-runtime.md`. Codex-WIP acceptance record: `docs/reviews/2026-10-17-codex-wip-acceptance.md`. Operations: `docs/runbooks/autonomous-paper.md`. History: `.agenkit/sdd/progress.md`, `master-context.md`.

## What exists (commit → piece)
| Commit | Piece |
|---|---|
| `81910dd`…`1eaf7ca` | `runtime.py` paper loop. Offline/Jev REFLEX, code gate, quarter Kelly, hard risk, cost-aware labels, public market rules, nightly. Every piece independently reviewed and fixed. |
| `4b98194`, `1eaf7ca` | `jev_reflex.py`. Inert until JEV_API_KEY plus an operator-approved schema; hard 250 ms deadline; fail-safe holds. |
| `a1cc5cc` | `runtime_authority.py`: `--accounting ledger` routes orders via OrderAuthority/Ledger. |
| `4fb0de9`, `71e75ff` | `live_venue.py` (Task 13): no submit retry, single dispatch, transfers, fee finality, streamed bounds. `authority_client.py` and dashboard (Task 17): authenticated forwarding, durable nonces, Origin/CSRF. |
| `7028b56`, `7900c6e` | Task 15 activation: ledger-enforced approvals, operator-only, at the hour boundary. Task 14: paper arm with deadman lease, uid-bound sessions, latch rules (drawdown never clearable, daily at rollover, crisis operator-only with conditions). |
| `bcafcd7` | Task 20: compose overlay, CI workflow with network guard, lock file, SQLite backup tool, runbook. |
| `c812fca` | Task 19: `replay.py`, exact-fidelity replay, sealed splits with purge/embargo, stresses, holdout register, promotion criteria. |
| `27af3eb` | Task 18: `brain_anthropic.py`. Opus 5.5 BRAIN for escalations and nightly, proposal-only (`python -m bot.autonomous.nightly`). |

## In flight when this was written
1. Builder: an authority lease renewer (Task 16), plus a formal decision bridge so ledger-mode entries can work once a real calibration attestation exists. Uncommitted edits are in runtime*.py, ledger.py, jev_reflex.py and test_authority_worker.py.
2. Reviewer agent: BRAIN request without `fallbacks` by default, token usage accounting, and a dashboard proposals/midnight view.

## Next (in order)
1. Finish the two in-flight items above and get an independent review of each.
2. Authenticated evaluations needing operator credentials: Jev (key, approved schema, tokenizer proof, latency) and Anthropic BRAIN (key).
3. Task 13: import live evidence into ledger journals; reconcile UNKNOWN intents at startup; production capability verifier.
4. Task 15: production release verifier and trusted catalog.
5. Task 20: four-role process split with file-mounted secrets; run `docker compose config` and the drills.
6. Task 21 independent acceptance. Task 22 needs explicit operator approval for any live progression.

## Hard rules (do not relax)
- No live orders or arming. Live arm is refused in code (`LIVE_ARM_REQUIRES_TASK22`).
- Code owns every threshold, size and side effect. Models (Jev/BRAIN) only judge or propose; they can never approve, activate, arm or clear.
- Calibration comes only from real resolved labels. An empty eligible universe or "never trades" is a valid outcome.
