# Resumed paper authority build

This checkpoint continues the approved autonomous-agent plan on
`feat/autonomous-crypto-agent`. It is a partial software build, not Build/Review/
Ship acceptance or evidence that a live account can trade.

## Work preserved and advanced

- `85f2f98` records additive platform scope and remaining integration work in
  `master-context.md`.
- `1fd3625` adds an inert managed IOC transport boundary. Exact quantity/price,
  fee, client-ID, intent, market and injected-client evidence checks precede the
  single fake-tested submission. It propagates uncertain failures without retry.
  No production capability verifier or live authority port is supplied.
- Existing paper execution, worker, nightly metrics, research-card and public-data
  work is preserved. This is not a claim that every corresponding plan task is
  accepted.
- Partial IOC entries that end canceled can reduce their accounted holdings.
  Parent status alone cannot establish a fill, and the normal risk/owned-base
  checks still apply.
- Entry sizing is reproduced against the final refreshed executable book and
  rules. Only the current intent's own reservation is excluded from this second
  calculation; other reservations still consume capacity.
- The ledger's independent gross backstop includes existing liquidation holdings.
- Paper fills respect cumulative reserved fees before execution, including adverse
  per-fill microdollar rounding. Unaffordable quantities remain unfilled; fees
  are never clipped. Sub-lot levels do not hide later executable depth.
- Review-only CLI paths use SQLite read-only connections. They no longer trigger
  authority recovery, revoke permits, advance generations or rewrite in-flight
  intents merely by reporting on an active ledger.
- Temporary absolute-path Cursor diagnostics were removed from autonomous code
  and its tests. Durable journal and evidence writes remain.
- `reservation_releasable` expresses a necessary terminal/fill/fee proof. It does
  not itself reconcile an account or release any reservation.

The execution safety fixes were independently reviewed and regression-tested.
The managed transport received a separate review with no blocking finding in its
explicitly inert scope.

## Local control slice

The local Unix protocol authenticates configured sessions, peer credentials,
account/mode/boot, role, token and expiring nonces. It bounds and strictly decodes
messages and durably records request claims before callbacks. Interrupted
callbacks retain an unknown outcome instead of being repeated automatically.

The concrete ledger backend implements status and kill, sharing the order
authority's account mutex. Authorization is checked again after acquiring that
mutex. Kill commits before acknowledgment; in-flight orders retain reservations
and are reported as pending cancellation/reconciliation. Journal directory links
are fsynced before callbacks. Independent review found no remaining blocking issue
in this scoped implementation; 12 tests include actual sockets and ledger state.

Session provisioning, daemon/dashboard wiring, cancel transport, arm/deadman,
review resolution and release activation remain unfinished. Unavailable handlers
reject; there is no automatic hard-kill reset.

## Verification

Full working-tree suite: 525 tests passed on Python 3.12.14 and 525 on Python
3.14.4. The runs include the control and transport tests; no authenticated provider
or exchange calls were made. The dependency emits a Starlette/httpx deprecation
warning, and Python 3.14 exposes unclosed SQLite connections in some test fixtures.
These passes establish local tested behavior, not completion of the full plan.

An independent checkout of the staged source passed 523 tests on Python 3.12.14.
This checkout excludes the inherited dashboard/deployment edits and their two
additional API tests, proving the runtime checkpoint stands independently.

## Continuing requirements

Full Tasks 12–22 remain subject to the approved plan. In particular: production
venue capabilities and authenticated economic evidence; bounded reads, fill
pagination/corrections and uncertain-order reconciliation; remaining authenticated
operator commands and dashboard forwarding; immutable release activation;
production causal ingestion and separated role workers; frozen nightly archives,
forecast-level metrics and external BRAIN integration; causal replay/promotion
evidence; dependency locks, deployment isolation, operational drills and final
independent acceptance.

The browser/dashboard and deployment edits inherited at resume are separate
uncommitted work. They must be reviewed and integrated through the authority.
Current diagnostics and unit tests cannot qualify candidates, prove provider
token accounting, or grant strategy/live promotion.

## WHAT COULD I BE WRONG ABOUT?

Passing synthetic tests can miss venue semantics, sparse-book behavior,
correlated outcomes and adverse costs. Inert transport tests prove request
construction, not real client-ID idempotency or IOC behavior. Calibration and
research evidence may remain insufficient even when the software runs correctly.
Reports must preserve that uncertainty and keep unsupported entries disabled.

## Control rules (2026-10-18, Tasks 14/15)

These are enforced in `bot/autonomous/ledger.py` and reached only through the
authenticated control protocol (`bot/autonomous/control.py`).

- **Latches.** DRAWDOWN is never clearable by any command (`LATCH_PERSISTENT`).
  DAILY clears only by UTC rollover inside the ledger's loss latching
  (`CLEARS_AT_ROLLOVER`). CRISIS is cleared only by an operator, with a non-empty
  reason, on an account that is unarmed (`permit='none'`), ready (an exact NAV
  mark within the maximum mark age) and has no unknown intents. KILL is cleared
  only with no unresolved orders. Every clear leaves the account unarmed with a
  new permit epoch and is recorded in `control_events`. Removing a `KILL` file
  never clears the ledger kill.
- **Single latch owner.** In ledger accounting the ledger's `account_control`
  owns every latch. A REFLEX crisis is raised there (after that cycle's
  protective exits), and the runtime's latch view derives from it.
- **Arm.** Operator-only and PAPER-only; LIVE arm is refused with
  `LIVE_ARM_REQUIRES_TASK22` whatever the role. Arming requires a reconciled
  account, the named release active and unexpired, no unknown intents and no
  latch. It sets a bounded deadman lease. Only the `authority` worker role can
  renew a live lease; a lapsed lease cannot be renewed, disarms entries
  (`reduce_only`, new epoch) and keeps a bounded reduce-only lease so exits stay
  allowed.
- **Activation.** Operator `activate_release` plans for the next hour. The ledger
  itself re-checks all six gates, operator-only approvers and actor, and bound
  hashes when scheduling and again when applying. Applying also requires the
  account unarmed, ready and not hard-killed, the active release unchanged and
  the bundle unexpired. Nothing activates without an operator plan.
- **Sessions.** `python -m bot.autonomous.control issue-session --role operator`
  mints a uid-bound, short-lived (at most 1 h) 0600 session file. Only operator
  and reader roles are issuable. A ledger-mode runtime hosts the socket at
  `<data-dir>/control/control.sock` (0700 directory) and reloads sessions from
  `<data-dir>/control/sessions`.
