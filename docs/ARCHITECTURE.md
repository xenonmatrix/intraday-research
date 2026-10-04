# Production architecture and hardening audit

## Lifecycle

Canonical JSON is validated and applied to an immutable runtime snapshot. The calendar intersects NYSE/Nasdaq sessions. The reused MCP client reads the pinned account, positions, orders, quotes, tradability and completed regular-session bars. ORB validates contiguous positive-volume OHLCV and computes range/VWAP. Causal analysis records supporting/conflicting features without extra filters. The deterministic risk engine has final veto.

A BEGIN IMMEDIATE transaction reserves symbol slot, capital, planned risk and a daily attempt, persisting a UUID intent. Review checks broker response shape/warnings. Fresh broker/data/health/risk checks run again. RESERVED becomes DISPATCHING durably before one guarded network call. Acknowledgement is persisted, then actual order state/executions are queried. A timeout produces AMBIGUOUS, not a retry. Partial executions accumulate immutably under the owning position; broker truth is reconciled after actions.

Positions retain original configuration fingerprint, strategy fingerprint, target multiple, hold duration, stop/target and exit state. Stop, target, completed-bar VWAP, time, session and emergency exits are evaluated independently before candidates. Exits bypass entry-only clock/KILL/maintenance/connectivity holds where ownership, current quotes and broker state suffice. They still obey hard READ_ONLY, market hours and ambiguity protections. Fill-based P&L and gross losing fills update durable session budgets conservatively. SQLite audit events and exports retain decision context.

## Invariants

These are not optional settings: persist intent before dispatch; never blindly replay writes; never assume fills; risk final veto; broker ownership reconciliation; unique intent and broker IDs; atomic aggregate reservations; one open/pending position per symbol; long regular-hours equity only; no averaging down; no account-balance-based expansion; profits do not replenish daily loss allowance. Order review and fractional equity support are required. Features remain observation-only. Fixed supported mode values are shown in configuration for clarity but alternatives are rejected.

## Repairs and hardening

- Added one canonical configuration source, strict typed validation, applied snapshot comparison and safe reload rules. Previously risk lived in SQLite while timings/strategy/host values were scattered.
- Removed private account suffixes from production defaults and lookups; account selection is explicit and private. A changed selector cannot silently switch the pinned account.
- Added independent configuration/strategy fingerprints and public snapshots to new session/event/trade/fill/export records. Existing historical records are not rewritten.
- Exit parameters are captured per position; updated defaults cannot change an already-open trade's target or maximum hold.
- Risk calculation now uses the configured slippage buffer consistently in sizing and final checks, rather than a hidden fixed one-basis-point term.
- Replaced unrestricted persistent enable/protection-off CLI paths with a separate readiness-checked transition. A durable TRANSITION marker blocks entries across interrupted activation. Activation requires a separate explicit operator action.
- Added actual sleep-inhibitor evidence, host fingerprint matching and an explicit macOS-only production adapter rather than pretending unsupported hosts pass.
- Added a durable rolling worker restart budget. Budget exhaustion retains KILL and pauses restarts until capacity returns; it is not a successful recovery.
- Status distinguishes desired versus applied configuration and reports exact current markers. It rechecks heartbeat/watchdog deadlines so a cached report cannot extend liveness eligibility.
- Worker restart invalidates opening health and starts fresh timed connectivity observations while preserving losses, attempts, outages and MISSED candidates. Backward clock steps cannot erase the durable restart budget.
- Retained MCP connection reuse, independent RPC deadlines, bounded reconnect backoff and HTTP mutation replay guard. No mutation retry option exists.
- Preserved the completed-interval historical request fix: before 09:35 normal-session time, the broker adapter returns an empty range locally instead of sending start=end.
- Consumed MISSED states and historical session loss/attempt/outage records remain durable. Clock failure does not increment connectivity outages.
- Replaced private local policy dependencies in historical tests with synthetic checked-in fixtures; default observation remains mutation-disabled.

## Limits

Only SPY/QQQ subsets, five-minute bars and a 15-minute range are validated. Integer-share alternatives, alternative bar intervals, arbitrary symbols and multiple same-symbol positions are unsupported. Configuration changes are not automatically hot-reloaded. No setting makes an unresolved order safe. Daily loss tracking is conservative across partial losing fills. Whole-account broker positions/orders outside this isolated experiment cause reconciliation failure rather than being adopted. Full account identifiers and execution details remain in private local state; configuration fingerprints omit the private account selector.

The 50 ms combined clock rule is unchanged. It is conservative on hotspots and is not a claim that network RTT equals clock error. Production still uses exact wall-clock boundaries and strict future-quote rejection; an interval-aware replacement requires a separate validated design.
