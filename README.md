# Intraday experiment

An experimental, persistent intraday execution engine for long U.S. equity ETFs through the **Robinhood Trading MCP**. It prioritizes deterministic risk limits, broker reconciliation, exactly-once order intent handling and auditable decisions. **Real-money trading can lose money. There is no profitability claim.** Fractional protective stops may be unavailable; local monitoring is not a broker-held stop.

The validated universe is **SPY and QQQ**, or a configured subset. The default strategy is a 15-minute opening-range breakout: range high + $0.01 entry trigger, range low − $0.01 invalidation, 2R target, completed-bar VWAP exit, and a 30-minute maximum hold. Additional features are logged, not new entry filters. A prior post-range breakout or range-low violation disqualifies that day's setup (`MISSED`); it is never replayed or reset to force a trade.

**A fresh installation cannot submit orders.** Configuration defaults to READ_ONLY, account selection is unset, migration installs a hard READ_ONLY marker and persistent LIVE is false. Tests use isolated fake brokers. LIVE enablement is an independent operator decision and never implies entry eligibility.

## Installation

Python **3.11 or newer**. Production host monitoring currently supports **macOS only**, with a readable battery and lid state by default. Linux can run configuration inspection and unit tests; production doctor fails closed without a supported host adapter. Windows is currently unsupported because local locking uses POSIX `fcntl`; no Windows host/locking adapter is provided. Desktop Macs without a battery currently fail the required battery evidence; the software does not invent a percentage. Do not disable checks to misrepresent unsupported hardware as supported.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,research]'
.venv/bin/python -m trader config init
.venv/bin/python -m trader config validate
.venv/bin/python -m trader migrate-persistent
.venv/bin/python -m pytest -q
```

`requirements.lock` records the development environment's package versions; `pyproject.toml` declares supported dependency ranges. `numpy` is only required for research/replay tests. Core dependencies are the official MCP Python SDK, HTTPX with HTTP/2 and pandas-market-calendars. No YAML dependency is needed.

## Configuration and authentication

The canonical local file is **`config/trader.json`**, created with mode 0600 and ignored by Git. The checked-in **`config/trader.example.json`** contains safe public defaults and no account selector. JSON decimal monetary/percentage fields are represented as strings to preserve decimal arithmetic. Unknown keys, wrong types, duplicates, NaN/infinity, unsupported modes and inconsistent timings are rejected.

Set `broker.account_suffix` to your account's final four digits locally. The selector must match exactly one MCP-enabled account; the full account identity is pinned privately in the ledger. Never commit account information. The CLI does not echo the selector in configuration summaries.

```sh
.venv/bin/python -m trader config path
.venv/bin/python -m trader config set broker.account_suffix YOUR_FOUR_DIGITS
.venv/bin/python -m trader auth
.venv/bin/python -m trader auth-status --check
.venv/bin/python -m trader doctor --broker
```

`auth` uses the standalone OAuth browser flow with a localhost callback. Persisted credentials stay in `runtime/oauth.json` (0600); runtime is 0700. Refresh is noninteractive where supported. The program never copies credentials from another application or hardcodes tokens. Authentication alone is not reconciliation or trading readiness.

Read [configuration reference](docs/CONFIGURATION.md) for all settings, derived risk, warnings, supported ranges and reload rules. Settings are not automatically hot-reloaded. Edits are staged; entries block on any desired/applied mismatch. Restart is required to apply them. Existing positions retain their original exit settings. Configuration changes are prohibited while positions or pending/ambiguous orders exist. Strategy/session changes cannot apply during a market session.

## Observation, status and operation

For a production-equivalent **disarmed** persistent monitor, first migrate and confirm LIVE is false and READ_ONLY is present. `--mode LIVE` selects the persistent execution engine; it does **not** grant submission permission.

```sh
.venv/bin/python -m trader config show
.venv/bin/python -m trader status
.venv/bin/python -m trader doctor --broker --json
.venv/bin/python -m trader supervise --mode LIVE
```

Without `--mode`, `run` and `supervise` default to READ_ONLY and use the legacy observation adapter. `--mode PAPER` is an explicit, mutation-disabled legacy simulation adapter, not certification of the new concurrent LIVE engine. The persistent path is selected by `runtime/PERSISTENT`; do not run two monitors against one runtime. The process lock rejects that.

For unattended macOS operation, generate a **local** LaunchAgent with your installation path:

```sh
.venv/bin/python -m trader launchagent generate
mkdir -p "$HOME/Library/LaunchAgents"
cp runtime/com.local.intraday-paper.plist "$HOME/Library/LaunchAgents/com.local.intraday-paper.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.intraday-paper.plist"
launchctl print "gui/$(id -u)/com.local.intraday-paper"
```

The existing label is retained for compatibility; its name does not imply PAPER mode. A LaunchAgent starts at login and runs without Terminal or an assistant session. It does not run before user login or survive power loss. The supervisor restarts workers, enforces a restart budget, maintains heartbeat/host evidence and asserts `caffeinate` during the monitoring window, persistent enablement or any owned position. Keep the lid open. Sleep prevention does not protect against a closed lid, reboot, logout or dead battery.

Stdout/stderr are `runtime/launchd.out.log` and `runtime/launchd.err.log`. All private state and logs are excluded from Git. `status` exposes current blockers, configuration fingerprints, health, risk, positions and individual exit state. A supervisor being alive is not proof that the worker or broker is healthy.

## Stop, maintenance and recovery

```sh
.venv/bin/python -m trader live disable
.venv/bin/python -m trader maintenance on
.venv/bin/python -m trader read-only on
.venv/bin/python -m trader disable-entries --reason 'Operator pause'
.venv/bin/python -m trader liquidate --reason 'Operator liquidation request'
.venv/bin/python -m trader stop
```

These have different effects:

- `live disable` and maintenance block new entries; verified exits remain eligible.
- `read-only on` blocks **all** broker mutations, including exits and cancellations. Do not engage it while holding a position unless intentionally preventing every broker action.
- KILL/`disable-entries` requests emergency handling and blocks entries. `liquidate` requests verified risk reduction. Neither guarantees a fill.
- `stop` writes STOP and stops monitoring; it does **not** liquidate. Do not stop an unattended open position without arranging independent management.

To resume a deliberately stopped, flat, disarmed monitor, inspect the stop cause, retain READ_ONLY and LIVE disabled, then remove only the operator STOP marker and restart/boot out + bootstrap the LaunchAgent. There is no automatic removal of KILL, maintenance, ambiguous order state or daily loss latches. Do not delete SQLite state to recover from a failure.

Connectivity uses reused HTTP/2/MCP sessions and bounded backoff. One outage episode is not counted for every failed retry. Recovery needs healthy observations over time; the halt recovery currently requires regular-session data. Clock failure blocks entries/opening health without being counted as a broker outage. Reconciliation, logging and verified exits remain operational where broker state and quotes allow. A timeout after submission remains ambiguous; no blind replacement order is sent. [Versioned position recovery](docs/RECOVERY.md) separates entry holds from per-position recovery and retains conservative exits by default. The optional bounded policy requires complete positive certification and a controlled restart; it is not automatically enabled.

See [production transition](docs/OPERATIONS.md) for the separately invoked, fail-closed operator procedure. Nothing in installation, `doctor`, tests or configuration validation activates LIVE.

## Risk model and execution

Defaults: $10 maximum capital per trade; 3 maximum positions; $30 aggregate exposure; 1.5% of per-trade capital planned loss ($0.15); 5% of configured aggregate exposure daily gross loss allowance ($1.50); 5 entry attempts/session; $20 unleveraged buying-power reserve. One open or pending position per symbol means the validated two-symbol universe can occupy at most two positions.

Sizing takes the smaller of notional capacity and entry-to-stop dollar-risk capacity, with configured slippage/fee buffers. It cannot promise the realized loss stays below the planned loss during gaps or outages. Profits do not refill the daily allowance or expand experiment capital. Risk increases do not refill an already-established daily loss budget. Losses can reduce remaining experiment allocation.

Atomic SQLite reservations include open and pending capital/risk before order review/submission. Each position owns its intents, order IDs, fills, stops, targets and deadlines. Intents persist before submission; actual broker executions drive position accounting. All submissions receive final health/risk/reconciliation checks. Unresolved submissions block new entries. Options, crypto, futures, shorts, leverage and averaging down are unsupported.

The exchange calendar handles holidays and early closes. Entry begins after the completed opening range; cutoff is close−60 minutes; liquidation starts close−15 minutes. Exits are evaluated before new entries. Hard READ_ONLY, closed markets, unknown ownership, ambiguous conflicting orders, stale position quotes and broker outages can prevent exits. **No local program can guarantee liquidation without power, network and broker availability.**

## Logs and analysis

SQLite (`runtime/persistent.sqlite3`) is the durable source of truth: configuration snapshots/fingerprints, session budgets, candidates, reservations, acknowledgements, executions, position ownership and failures. New records identify the applied configuration; older unversioned records remain labeled historical rather than fabricated. Rejected candidates also retain available features.

```sh
.venv/bin/python -m trader export-analysis
```

Exports default to `reports/persistent/`: events/candidates/fills/positions JSONL and `trades.csv`. They contain private financial information. Back up the database consistently using SQLite backup tooling, including all required state; do not copy only a live main database while ignoring its WAL. Database and journals grow over time; retention/archival is an operator responsibility.

## Clock health and hotspots

Offset is the estimated difference from a time source; uncertainty bounds confidence in that estimate; network RTT is request/response delay; quote age is time since the venue timestamp. They are different quantities. The current advanced policy remains **abs(offset) + uncertainty ≤ 50 ms**, with fresh evidence and strict future-quote rejection. Cellular paths can make uncertainty too large even when offset is modest. A passing quote-age check does not imply clock health.

```sh
.venv/bin/python -m trader clock-diagnostics --samples 20 --interval 10
```

This is query-only: no clock adjustment and no production health/latch writes. It records offset, uncertainty, combined bound, source and RTT variation separately under `reports/clock/`. Compare the same command on an independent stable network. Do not pick favorable samples, halve uncertainty or raise a threshold merely to trade. Clock changes are safety-critical, visible in validation/status and require restart/revalidation. A future uncertainty-aware timing redesign needs separate review and regression tests.

## Publication and security

Do not publish runtime, local configuration, OAuth files, reports, research datasets, CSV/JSONL exports or historical account-specific delivery notes. `.gitignore` preserves them locally but does not untrack files already committed. Run `python ops/repository_check.py`, then `python ops/repository_check.py --staged` and inspect the exact staging list before publication. The staged scan reads actual Git index blobs, including force-added ignored files. The scanner reports paths/counts, not secret values; a clean result is not proof that credentials never existed in Git history. No GitHub repository, upload or push is performed by these tools. Repository publication requires a separate privacy review and explicit approval.

[Architecture and audit](docs/ARCHITECTURE.md) · [Settings and invariants](docs/CONFIGURATION.md) · [Operator runbook](docs/OPERATIONS.md)
