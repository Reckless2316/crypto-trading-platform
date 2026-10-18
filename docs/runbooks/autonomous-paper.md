# Runbook: autonomous PAPER runtime

Scope: `python -m bot.autonomous.runtime --paper`, the paper-only loop described in
`docs/checkpoints/2026-10-17-claude-paper-runtime.md`. It reads public Coinbase books, holds no venue
credentials and cannot send a real order.

**Live trading is not available.** See [Live trading gate](#live-trading-gate) at the end.

There are two ways to run it. Use one per host, never both on the same data directory.

| | Host (systemd user units) | Container (compose overlay) |
|---|---|---|
| Files | `deploy/autonomous/surge-paper-runtime.service`, `surge-nightly.service`, `surge-nightly.timer` | `deploy/docker-compose.autonomous.yml` on top of `deploy/docker-compose.yml` |
| Data dir (`$D` below) | `~/crypto-trading-platform/data/autonomous/runtime` | volume `autonomous-runtime`, mounted at `/var/lib/surge/runtime` in `surge-paper-runtime` and read-only at `/var/lib/surge-runtime/runtime` in `surge` |
| Secrets | `UnsetEnvironment=` strips venue/provider keys and live switches | none in the environment, no published ports, own egress network |

The data dir holds `runtime.sqlite3` (decisions, labels, fills, latches, escalations), `ledger.sqlite3`
(only with `--accounting ledger`), `public_calls.sqlite3` (public data call log), `status.json`, `reviews/`,
the control files `STOP` and `KILL`, and the Jev schema files.

Compose shorthand used below:

```bash
C="docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.autonomous.yml --project-directory ."
```

## 1. Start

Host:

```bash
cp deploy/autonomous/surge-paper-runtime.service deploy/autonomous/surge-nightly.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now surge-paper-runtime surge-nightly.timer
systemctl --user status surge-paper-runtime
```

Container:

```bash
$C config --quiet                       # resolve and validate first
$C build surge                          # builds surge-paper:local; the runtime service never pulls
$C up -d surge-paper-runtime surge
$C ps                                   # surge-paper-runtime becomes "healthy" after the first cycle (start_period 180 s)
```

Both start paths remove a stale `STOP` file and leave `KILL` in place. The default is **unarmed**: the
runtime observes, judges, labels and escalates, but does not fill. To permit paper fills, change the command
deliberately. On the host, edit `ExecStart` to add `--arm-paper`. In a container, use a local override file,
never the committed overlay. `--arm-paper` is a local paper flag and cannot arm a live account.

## 2. Stop (graceful): STOP file

```bash
touch "$D/STOP"                                   # host
$C exec surge-paper-runtime touch /var/lib/surge/runtime/STOP   # container
```

The loop checks `STOP` before and after each sleep. It finishes the current cycle and exits with code 0.
Then systemd (`Restart=on-failure`) and compose (`restart: on-failure`) both leave it stopped. Reservations
and open paper positions are preserved in SQLite. `systemctl --user stop` and `$C stop` are also safe:
compose sends SIGINT (`stop_signal`), so `runtime.close()` runs, and SQLite commits are atomic either way.

## 3. Kill new orders: KILL file

```bash
touch "$D/KILL"
$C exec surge-paper-runtime touch /var/lib/surge/runtime/KILL
```

`KILL` blocks every order path, **including exits** (a project global constraint). A killed account with an
open paper position stays exposed until the operator acts. `status.json` shows `"kill_switch": true`.
Remove `KILL` only on purpose: `rm "$D/KILL"`. Backups never restore a `KILL` file over a live directory.
KILL is a hard stop for orders, while risk latches (drawdown 15%, daily 2%, crisis) are code-set halts.
List latches in `status.json` → `latches`. Clear one only as the operator:

```bash
.venv/bin/python -m bot.autonomous.runtime --paper --data-dir "$D" --clear-latch <kind>
```

The model can set a crisis latch but has no path to clear one.

## 4. Restart and recovery after a crash

1. Look at the cause first: `journalctl --user -u surge-paper-runtime -n 200` or `$C logs --tail 200 surge-paper-runtime`.
   Cycle errors are logged as JSON `{"cycle_error": ...}` on stderr. They do not stop the loop. A process exit is a real crash.
2. Check the databases: `.venv/bin/python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone())" "$D/runtime.sqlite3"`.
   Do **not** delete `-wal`/`-shm` files next to a live database. They hold committed data.
3. Restart: `systemctl --user restart surge-paper-runtime` or `$C up -d surge-paper-runtime`.
   On restart the runtime re-binds its metadata. It does not replay missed historical entries, and missed
   candles are simply not traded. Labels that cannot be resolved are marked `unresolvable`.
4. Confirm recovery: `status.json` `at_us` advances every minute, `latches` matches what you expect, and
   `positions`/`cash_usd` match the last good status from the backup (section 5).
5. If integrity fails, stop (section 2) and restore the latest verified backup (section 5).
6. Crash loops: systemd retries every 30 s. Compose `on-failure` retries with back-off. Touch `STOP` to end
   the loop while you investigate.

## 5. Backup and restore of the SQLite files (WAL-safe)

The runtime uses `journal_mode=WAL`. **Never `cp` a live `*.sqlite3`**: committed pages can still live only in
the `-wal` file. Use SQLite's online backup, which is consistent while the runtime is writing.

With the sqlite3 CLI (if installed):

```bash
TS=$(date -u +%Y-%m-%dT%H%MZ); mkdir -p backups/$TS
for db in runtime ledger public_calls; do
  [ -f "$D/$db.sqlite3" ] && sqlite3 "$D/$db.sqlite3" ".backup 'backups/$TS/$db.sqlite3'"
done
sqlite3 backups/$TS/runtime.sqlite3 "PRAGMA integrity_check;"     # must print: ok
```

Without the CLI (stdlib only; also writes `MANIFEST.json` with sha256 and integrity results):

```bash
.venv/bin/python deploy/autonomous/sqlite_backup.py backup "$D" backups/$(date -u +%Y-%m-%dT%H%MZ)
.venv/bin/python deploy/autonomous/sqlite_backup.py verify backups/<ts>
```

Container. The tool is not in the image, so feed it on stdin, write inside the volume, then copy it out:

```bash
TS=$(date -u +%Y-%m-%dT%H%MZ)
$C exec -T surge-paper-runtime python - backup /var/lib/surge/runtime /var/lib/surge/backups/$TS < deploy/autonomous/sqlite_backup.py
$C cp surge-paper-runtime:/var/lib/surge/backups/$TS backups/$TS
$C exec surge-paper-runtime rm -rf /var/lib/surge/backups/$TS
```

Restore (host). The tool refuses unless the runtime is stopped:

```bash
touch "$D/STOP"; systemctl --user stop surge-paper-runtime     # wait for the exit
.venv/bin/python deploy/autonomous/sqlite_backup.py restore backups/<ts> "$D"
systemctl --user start surge-paper-runtime                     # ExecStartPre clears STOP
```

`restore` verifies sha256 and integrity, deletes stale `-wal`/`-shm` next to each restored database (so old WAL
frames cannot be replayed onto it) and never restores `KILL`. With the sqlite3 CLI the equivalent is: stop,
`rm -f "$D"/runtime.sqlite3-wal "$D"/runtime.sqlite3-shm`, `sqlite3 "$D/runtime.sqlite3" ".restore 'backups/<ts>/runtime.sqlite3'"`.

Retention: take a backup before every upgrade and restore drill and daily after the nightly review. Keep 14
dailies. Backups contain no secrets, but they do contain the full decision history, so keep them local.

## 6. Clock skew checks

Candle cutoffs come from the local clock, and the runtime accepts a book within **±10 s** of the candle close
(`close_tolerance_us = 10_000_000`). The observed WSL skew was about **4 s ahead** of Coinbase (commit
`16a4d6e`). That is inside the tolerance, but WSL drifts after host sleep, and a skew above 10 s
**silently holds every cycle forever** (reasons `AWAY_FROM_CLOSE` / `FUTURE_OBSERVATION`).

```bash
# Skew against the venue (positive = local clock ahead):
python3 -c "import json,time,urllib.request; t0=time.time(); v=json.load(urllib.request.urlopen('https://api.exchange.coinbase.com/time',timeout=5))['epoch']; t1=time.time(); print(f'skew_s={(t0+t1)/2-v:+.3f} rtt_s={t1-t0:.3f}')"
chronyc tracking            # or: timedatectl status  ("System clock synchronized: yes")
```

- |skew| < 2 s: fine. 2–8 s: fix it at the next opportunity. Above 8 s: fix it now. Treat cycles as unreliable until it is fixed.
- WSL fix: `sudo hwclock -s` or `sudo chronyc makestep`. If that fails, run `wsl --shutdown` from Windows and restart.
- Containers share the host kernel clock, so fix the host.
- Watch for it: a rising share of `AWAY_FROM_CLOSE`/`FUTURE_OBSERVATION` hold reasons in `status.json` → `instruments`.

## 7. Reading status.json

`status.json` is rewritten atomically at the end of every cycle (once per minute). Host: `cat "$D/status.json"`.
Container: `$C exec surge-paper-runtime cat /var/lib/surge/runtime/status.json`. Dashboard: `GET /api/autonomous/runtime`
(read-only; `stale` is true above 180 s).

| Field | Meaning / expected |
|---|---|
| `mode`, `live` | always `"paper"`, `false`. Anything else: stop and escalate. |
| `at_us` | cycle cutoff, µs since epoch. It must advance every ~60 s (the container healthcheck fails after 180 s). |
| `armed_paper` | `false` by default; `true` only with `--arm-paper` |
| `kill_switch`, `latches` | KILL file present; active risk latches (blocking) |
| `reflex`, `reflex_calibrated`, `reflex_fallback`, `jev` | REFLEX id (cohort key). It is always uncalibrated today. `jev_not_configured` is expected. |
| `equity_usd`, `cash_usd`, `high_water_usd`, `day_open_usd`, `positions`, `dust_usd` | paper accounting |
| `brier*`, `resolved_labels`, `unresolvable_labels`, `labeled_this_cycle` | calibration evidence. Fewer than 100 resolved labels per cohort means size is 0. |
| `pending_escalations` | BRAIN-review rows within the window. A growing count needs operator review. |
| `fills`, `accounting`, `ledger` | paper fill count; `runtime` or `ledger` accounting path |
| `instruments` | per-instrument decision and hold reasons for this cycle |

Quick health check:

```bash
python3 -c "import json,sys,time; s=json.load(open(sys.argv[1])); print('age_s', round(time.time()-s['at_us']/1e6,1), 'mode', s['mode'], 'live', s['live'], 'kill', s['kill_switch'], 'latches', s['latches'])" "$D/status.json"
```

## 8. Nightly report

The timer runs at 06:10 UTC and reviews the **previous complete UTC day**. Run it by hand:

```bash
.venv/bin/python -m bot.autonomous.runtime --paper --report --data-dir "$D"                        # yesterday
.venv/bin/python -m bot.autonomous.runtime --paper --report --data-dir "$D" --review-day 2026-10-17
$C exec surge-paper-runtime python -m bot.autonomous.runtime --paper --report --data-dir /var/lib/surge/runtime
systemctl --user list-timers surge-nightly.timer; journalctl --user -u surge-nightly -n 50
```

It prints the metrics JSON and writes `reviews/nightly-<day>.json` plus a schema **proposal**. Proposals are
never applied automatically. An operator reads them and decides.

## 9. Jev schema approval procedure

The Jev REFLEX refuses to send anything until an operator approves the exact schema. It is unavailable in the
container overlay and under the systemd unit, which both strip `JEV_API_KEY` on purpose.

1. Write the proposal (offline, no key needed, no network call):
   `.venv/bin/python -m bot.autonomous.runtime --paper --data-dir "$D" --write-jev-schema`
   It prints `{"proposed": ".../jev_schema.proposed.json", "schema_hash": "...", ...}`.
2. Review `"$D/jev_schema.proposed.json"`: fields, types, bounds and the 400-token snapshot budget. Record the `schema_hash`.
3. Approve by copying it exactly: `cp "$D/jev_schema.proposed.json" "$D/jev_schema.approved.json"`.
   Do not edit the approved file. A hash mismatch holds every entry (`REFLEX_UNAVAILABLE`).
4. Run Jev manually only after the provider evaluation is authorized. Supply the key through the shell
   (never in chat, logs or committed files): `JEV_API_KEY=... .venv/bin/python -m bot.autonomous.runtime --paper --reflex jev --data-dir "$D"`.
   Any failure holds entries, and there is no silent fallback unless `--reflex-fallback offline` is passed.
5. Revoke: `rm "$D/jev_schema.approved.json"`. A schema change needs a new proposal and approval.
   Jev cohorts are kept separate by `reflex_id`.

## 10. Upgrade and rollback

Upgrade: take a backup (section 5), `touch STOP`, check out the new commit, run
`.venv/bin/pip install --no-deps -r requirements-autonomous.lock` (host) or `$C build surge` (container), then start.
Rollback: `touch STOP`, check out the previous commit or image, restore the pre-upgrade backup if the schema
changed, then start. The runtime checks its metadata binding on open. A mismatch fails loudly instead of
silently mixing cohorts.

## 11. Four-role deployment

The compose overlay splits the paper deployment into four roles. Each role sees only its own secret, and
every secret is a file mounted into exactly one role. No role gets a secret through its environment.

| Role | Service | Starts | Mounts | Secret |
|---|---|---|---|---|
| Authority | `surge-paper-runtime` (`--accounting ledger`) | always | `autonomous-runtime` rw (databases, `control/` socket, sessions, journal) | none |
| Reflex | `surge-reflex` | `--profile jev` | none | `jev_api_key` |
| Brain | `surge-brain` (nightly, proposal-only) | `--profile brain` | runtime **read-only**, `autonomous-brain` rw | `anthropic_api_key` |
| Dashboard | `surge-dashboard` (`python -m dashboard.launch`) | always | runtime read-only, brain output read-only, `dashboard-state` rw | none |

The legacy env-configured `surge` dashboard from the base file only starts with `--profile legacy-display`.

**Secrets.** Put each key in its own file under `deploy/autonomous/secrets/` (the directory is git-ignored),
readable by the container user, and never paste keys into chat, logs or committed files:

```bash
install -m 0440 /dev/stdin deploy/autonomous/secrets/anthropic_api_key   # paste the key, then Ctrl-D
install -m 0440 /dev/stdin deploy/autonomous/secrets/jev_api_key
```

**Authority and dashboard.** Start the authority, then issue a dedicated operator session for the dashboard
(sessions last at most one hour; reissue and restart the dashboard to renew). The boot id is the
authority's (`runtime-boot` by default):

```bash
$C up -d surge-paper-runtime
$C exec surge-paper-runtime python -m bot.autonomous.control issue-session --role operator \
   --actor operator:you --account runtime-paper --mode paper --boot-id runtime-boot \
   --dir /var/lib/surge/runtime/control/sessions --ttl-s 3600
$C up -d surge-dashboard
$C logs surge-dashboard | grep '#bootstrap='     # open that URL once; the token is single-use
```

`dashboard.launch` binds `127.0.0.1` only (host network, no published ports), refuses a missing or
loose-permission session file, reads the control epoch with one status request at start, and prints the
one-time `http://127.0.0.1:8000/#bootstrap=<token>` URL. The page exchanges the token for an HttpOnly
SameSite=Strict cookie and removes it from the address bar. Host equivalent:
`.venv/bin/python -m dashboard.launch --runtime-dir "$D" --session-file <issued file> --port 8000`.

**Brain (nightly).** `$C --profile brain run --rm surge-brain` reads the runtime read-only and writes
`brain/proposals/<day>.json` and `brain/nightly/combined-<day>.json` on its own volume. Proposals are
PENDING operator review and are never applied. Without a key file it falls back to deterministic metrics.

**Reflex.** `--profile jev` only reserves the Jev secret: a separate REFLEX process does not exist yet, so the
service refuses to start (`HANDOFF` in the overlay). Jev still runs only as described in section 9.

## Live trading gate

**Live trading is not available from this runtime, this overlay, these units or CI.** `--paper` is required
and is the only mode. `status.json` reports `"live": false`. No venue secret reaches the process. Deploying
any of these files never arms funds. `--arm-paper` only permits *paper* fills.

Live remains blocked until all of the following exist, each with recorded evidence
(`docs/plans/2026-10-16-autonomous-crypto-agent.md`, `master-context.md`):

- **Tasks:** 13/14/17 (authenticated control socket for kill, latch and arm instead of files); 16 (runtime
  orders through `OrderAuthority`/`Ledger` as the only accounting path for paper and live); 18 (provider
  REFLEX/BRAIN roles with file-mounted, role-specific secrets); 19 (replay and promotion evidence); 20's four-role
  split (`HANDOFF` in the overlay); 21 (independent spec and security/accounting review with no open
  Critical/Important findings and an accepted Build gate); 22 (Ship choice, credential-free paper, then
  real-data shadow with orders disabled).
- **Evidence:** provider tokenizer proof of the 400-token budget, measured Jev whole-call latency,
  calibrated cohorts, verified venue capabilities (bounded orders, client IDs, fill history) and at least
  one qualified instrument. An empty eligible universe is a valid outcome and keeps live disabled.
- **Explicit operator approvals:** venue and account, capital allocation, per-order cap, entry/reducing
  permit duration and renewal (deadman) procedure, the approved bundle, reconciled opening balances, bases
  and foreign orders, and kill/restart/unknown-order drill evidence.

Until every item is present, no live submission is possible.
