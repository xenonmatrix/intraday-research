# Operator procedures

## Readiness is not activation

`doctor --broker` performs fresh read-only account, positions/orders, quotes, bars and tradability checks. It never binds order/cancel capabilities and never writes a passing opening check or clears a latch. Local-only `doctor` does not claim broker readiness. A nonzero exit means NOT READY. It checks current session/opening health, configuration/application identity, private-file permissions, clock, power/lid/sleep evidence, heartbeat, watchdog, broker flatness and risk capacity.

The active runtime continues collecting real data and may pass its normal opening-equivalent check later if all conditions pass. A clock failure keeps that check blocked. A prior-day PASS is not reused. Premarket doctor may establish broker access but cannot certify unavailable opening bars. A MISSED candidate is never converted to a new signal by doctor or activation.

## Apply configuration safely

1. Inspect positions, orders, ambiguity, operator controls and current session.
2. Disable new entries with `trader live disable`; let verified exits finish. Do not engage hard READ_ONLY while relying on automated exits.
3. Once broker and local state are confirmed flat, retain/enable READ_ONLY and maintenance.
4. Change settings via `config set section.key value` or private JSON; run `config validate` and review warnings/fingerprint.
5. Wait until outside the market session for strategy/session changes. There is no staged mid-session strategy override.
6. Restart the disarmed LaunchAgent using `launchctl kill SIGTERM "gui/$(id -u)/com.local.intraday-paper"`. KeepAlive restarts it when STOP is absent. Do not restart with an unfinished operator STOP unless deliberately resuming observation.
7. Run `doctor --broker` and `status`; verify applied fingerprint matches the canonical file. Startup refuses changes while positions/orders exist or persistent LIVE remains enabled. A desired/applied mismatch blocks new entries; the running worker retains old settings for exits.

If configuration is malformed while positions exist, preserve the worker and pinned settings for exits; do not stop/restart it to apply invalid configuration. Fix the desired file to match the applied snapshot. Loss/attempt/cutoff/ambiguity records must not be deleted.

## FUTURE LIVE ACTIVATION — operator-only, not part of setup/testing

These commands are documented for a later deliberate operator action. Installation and tests do not run them.

```sh
.venv/bin/python -m trader config validate
.venv/bin/python -m trader doctor --broker
.venv/bin/python -m trader production check
.venv/bin/python -m trader status
```

Inspect the report. Mandatory configuration, pinned-account reconciliation, flat positions/orders, clock/host/sleep, stable connectivity, fresh market data, current-session opening health, watchdog/heartbeat and risk capacity must pass. Only the explicit operator holds may remain. If a genuine gate fails, stop here. A prior MISSED ORB remains disqualified even if all infrastructure checks pass.

For an existing watchdog-origin KILL, audit its reason and retained events. Recovery requires the same healthy worker for at least five minutes plus the mandatory current checks. An unknown/operator emergency KILL is not automatically cleared. Resolve its cause through a separately audited repair; do not relabel the marker to trick recovery.

The single controlled transition is:

```sh
.venv/bin/python -m trader production activate --acknowledge-risk --recover-kill
.venv/bin/python -m trader status
```

Omit `--recover-kill` when KILL is absent. The command serializes against configuration edits, runs read-only readiness, verifies recovery cause, establishes a durable entry hold, performs another fresh check, then removes recovered KILL/testing holds and sets the persistent preference. Entries stay held while it reconciles again after enablement. Only successful final verification removes TRANSITION. Failure disarms LIVE and restores READ_ONLY/maintenance; an interrupted TRANSITION remains an explicit blocker for audited recovery. It never creates a candidate or places a test trade. Re-running the checked command is the supported way to recover an interrupted transition after diagnosing its failure; do not delete TRANSITION blindly.

Persistent permission is not date-bound, but every later order must pass all current gates again. Each session gets new opening/recovery evidence and counters; historical events remain. The exchange calendar supplies early-close cutoffs. Use `live disable` to revoke new-entry permission.

An explicitly authorized, date-scoped activation watcher is available in
`ops/activate_session.py`. It calls this same transition after the entry window
opens and read-only doctor passes; it does not place orders or create signals.
Its private authorization records the date, configuration fingerprint, current
operator holds and ledger event boundary. Changed configuration/holds, a later
LIVE-permission command, STOP, TRANSITION, liquidation, the entry cutoff or a
different date terminate authorization. A completed/stopped watcher cannot
activate again. Run it under a dedicated local LaunchAgent with caffeinate when
the operator explicitly requests unattended activation. This is separate from
the trader worker; never use it to bypass a failed gate. Results are recorded in
`runtime/activation-YYYY-MM-DD-status.json`.

## Monitor checks and incidents

Use `launchctl print`, `status`, `doctor --broker`, `runtime/heartbeat.json`, `runtime/supervisor-health.json` and the private SQLite journal. A fresh heartbeat alone does not prove authentication/reconciliation. A supervisor process alone does not prove the worker is healthy.

Transport reconnects are bounded in delay, with a lower cap while a position exists. Retries do not replay mutations. Outage episodes and recovery are separate from individual failed reads. Three qualifying observations over 30 seconds recover ordinary connectivity; a latched hold needs 30 over five minutes by default. Qualifying observations include regular-session data in the current implementation. No recovery clears operator KILL/maintenance, ambiguous orders or spent daily risk.

Worker startup allowance is 45 seconds; heartbeat stale threshold 15 seconds; worker restart threshold 45 seconds; rolling budget three starts/restarts per 300 seconds. Budget exhaustion leaves entries blocked, writes KILL and pauses starts until the rolling window has capacity. A power/network/broker failure can leave a real position open; keep independent broker access available. Never assume stop/exit/liquidation has filled.

## Publication checklist

This repository may not yet have Git initialized. No initialization, remote, commit or push is implicit. Run `python ops/repository_check.py` to inspect the proposed publishable files. Inspect `.gitignore`, `git status --short` and `git diff --cached` yourself before a first publication. If private files were ever tracked, ignoring them is insufficient: remove them from the index/history as appropriate and rotate exposed credentials. Do not print tokens while diagnosing.
