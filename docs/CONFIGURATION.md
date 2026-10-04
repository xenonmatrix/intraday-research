# Configuration reference

Canonical local path: `config/trader.json` (ignored, 0600). Public example: `config/trader.example.json`. No secrets or OAuth tokens belong in either configuration. The private account suffix is a selector, not a credential; full identity is pinned in private runtime state and must not change silently.

`config init` creates defaults, or imports existing persistent risk/account settings once without resetting sessions. `config validate` rejects malformed values. Omitted supported fields use the documented safe defaults; missing files block production. Unknown sections/keys and duplicate JSON keys are rejected. JSON bool is not accepted as a number. Decimal values accept numeric strings/numbers and normalize deterministically; percentages use percent units (1.5 means 1.5%, not 150%). `config reset --dry-run` only previews defaults and never resets risk or controls.

All settings use one parser and validator. The CLI writes the same file you can edit. There are no environment-variable overrides or second permissive config source. SQLite holds the **applied snapshot** and persistent operator LIVE preference, not a competing editable configuration source.

## Reload contract

- **Hot reload:** not implemented; even logging changes take effect at restart. Conservative and explicit.
- **Restart required:** operational, risk, host, clock, logging and export settings. Direct file changes immediately block new entries when detected; final pre-submission guard compares desired/applied snapshots. Existing exits use the pinned applied snapshot.
- **Next session / restart:** strategy and session settings. CLI and startup reject applying them while a regular session is active. Make changes after close and review the new strategy fingerprint.
- **Locked with positions/orders:** all changes, including symbol/account selection, are rejected by CLI/apply while any owned position or pending/unresolved order exists. Do not restart into a changed configuration while managing positions.
- **Daily budgets:** config increases do not replenish the current session's established loss allowance; reductions tighten it. Profits never replenish it. Attempts/losses/ambiguity/cutoffs are not reset by configuration.

A displayed fingerprint identifies normalized public configuration, excluding the account selector. Every new event/trade/fill carries it; a stored public snapshot allows exact reconstruction. A separate strategy fingerprint covers strategy/session parameters. Historical records without fingerprints are labeled legacy/unversioned, never retroactively attributed to a new policy.

## Defaults and supported knobs

Settings marked **invariant/supported mode only** are shown for clarity; other values are rejected. Only a subset of SPY/QQQ, five-minute bars and a 15-minute range are validated. No arbitrary-symbol support is claimed. There is no toggle for blind retry, skipping reconciliation, accepting unknown orders, bypassing risk or treating ambiguous submission as unsubmitted.

| Key | Default | Classification |
|---|---|---|
| `trading.default_mode` | `READ_ONLY` | restart; flat account required |
| `trading.capital_per_trade` | `10` | restart; flat account required |
| `trading.max_open_positions` | `3` | restart; flat account required |
| `trading.max_total_exposure` | `30` | restart; flat account required |
| `trading.max_entry_attempts_per_session` | `5` | restart; flat account required |
| `trading.one_position_per_symbol` | `true` | invariant/supported mode only |
| `trading.buying_power_reserve` | `20` | restart; flat account required |
| `trading.symbols` | `['SPY', 'QQQ']` | restart; flat account required |
| `risk.max_loss_per_trade_percent` | `1.5` | restart; flat account required |
| `risk.max_daily_loss_percent` | `5` | restart; flat account required |
| `risk.daily_loss_base` | `max_total_exposure` | invariant/supported mode only |
| `risk.profits_replenish_allowance` | `false` | invariant/supported mode only |
| `strategy.name` | `orb` | invariant/supported mode only |
| `strategy.bar_interval_minutes` | `5` | invariant/supported mode only |
| `strategy.opening_range_minutes` | `15` | invariant/supported mode only |
| `strategy.breakout_offset` | `0.01` | strategy/session change; next session + restart |
| `strategy.stop_offset` | `0.01` | strategy/session change; next session + restart |
| `strategy.target_r` | `2` | strategy/session change; next session + restart |
| `strategy.max_hold_minutes` | `30` | strategy/session change; next session + restart |
| `strategy.vwap_exit` | `true` | strategy/session change; next session + restart |
| `strategy.retrospective_entries` | `false` | invariant/supported mode only |
| `market_data.max_quote_age_seconds` | `5` | advanced / safety critical; restart |
| `market_data.max_spread_percent` | `0.05` | advanced / safety critical; restart |
| `market_data.min_session_dollar_volume` | `10000000` | advanced / safety critical; restart |
| `market_data.future_timestamp_tolerance_seconds` | `0` | invariant/supported mode only |
| `market_data.bars_refresh_seconds` | `20` | advanced / safety critical; restart |
| `market_data.tradability_refresh_seconds` | `60` | advanced / safety critical; restart |
| `session.entry_cutoff_minutes_before_close` | `60` | strategy/session change; next session + restart |
| `session.liquidation_minutes_before_close` | `15` | strategy/session change; next session + restart |
| `session.monitor_warmup_minutes` | `10` | strategy/session change; next session + restart |
| `execution.order_type` | `market` | invariant/supported mode only |
| `execution.allow_fractional` | `true` | invariant/supported mode only |
| `execution.review_required` | `true` | invariant/supported mode only |
| `execution.preflight_max_age_seconds` | `5` | advanced / safety critical; restart |
| `execution.pending_entry_timeout_seconds` | `30` | advanced / safety critical; restart |
| `execution.slippage_bps` | `1` | advanced / safety critical; restart |
| `execution.fee_buffer` | `0.01` | advanced / safety critical; restart |
| `connectivity.request_timeout_seconds` | `8` | advanced / safety critical; restart |
| `connectivity.connect_timeout_seconds` | `8` | advanced / safety critical; restart |
| `connectivity.stream_read_timeout_seconds` | `300` | advanced / safety critical; restart |
| `connectivity.initialize_timeout_seconds` | `30` | advanced / safety critical; restart |
| `connectivity.backoff_initial_seconds` | `2` | advanced / safety critical; restart |
| `connectivity.backoff_max_seconds` | `30` | advanced / safety critical; restart |
| `connectivity.position_backoff_max_seconds` | `5` | advanced / safety critical; restart |
| `connectivity.outage_threshold` | `3` | advanced / safety critical; restart |
| `connectivity.halt_after_seconds` | `60` | advanced / safety critical; restart |
| `connectivity.recovery_observations` | `30` | advanced / safety critical; restart |
| `connectivity.recovery_min_seconds` | `300` | advanced / safety critical; restart |
| `connectivity.ordinary_recovery_observations` | `3` | advanced / safety critical; restart |
| `connectivity.ordinary_recovery_seconds` | `30` | advanced / safety critical; restart |
| `connectivity.poll_seconds` | `2` | advanced / safety critical; restart |
| `connectivity.idle_poll_seconds` | `30` | advanced / safety critical; restart |
| `connectivity.account_cache_seconds` | `8` | advanced / safety critical; restart |
| `connectivity.account_max_age_seconds` | `15` | advanced / safety critical; restart |
| `connectivity.keepalive_expiry_seconds` | `60` | advanced / safety critical; restart |
| `clock.health_mode` | `combined_bound` | invariant/supported mode only |
| `clock.max_combined_bound_ms` | `50` | advanced / safety critical; restart |
| `clock.sample_count` | `1` | invariant/supported mode only |
| `clock.sample_interval_seconds` | `60` | advanced / safety critical; restart |
| `clock.time_sources` | `['time.apple.com']` | invariant/supported mode only |
| `clock.sample_timeout_seconds` | `2` | advanced / safety critical; restart |
| `clock.sample_max_age_seconds` | `120` | advanced / safety critical; restart |
| `host.min_battery_percent` | `50` | advanced / safety critical; restart |
| `host.require_ac_power` | `false` | advanced / safety critical; restart |
| `host.require_lid_open` | `true` | advanced / safety critical; restart |
| `host.require_sleep_prevention` | `true` | advanced / safety critical; restart |
| `host.health_max_age_seconds` | `15` | advanced / safety critical; restart |
| `host.poll_seconds` | `5` | advanced / safety critical; restart |
| `watchdog.heartbeat_interval_seconds` | `5` | advanced / safety critical; restart |
| `watchdog.heartbeat_stale_seconds` | `15` | advanced / safety critical; restart |
| `watchdog.check_interval_seconds` | `1` | advanced / safety critical; restart |
| `watchdog.restart_after_seconds` | `45` | advanced / safety critical; restart |
| `watchdog.startup_timeout_seconds` | `45` | advanced / safety critical; restart |
| `watchdog.restart_limit` | `3` | advanced / safety critical; restart |
| `watchdog.restart_window_seconds` | `300` | advanced / safety critical; restart |
| `watchdog.cycle_deadline_seconds` | `15` | advanced / safety critical; restart |
| `logging.level` | `INFO` | restart; flat account required |
| `analysis.export_directory` | `reports/persistent` | restart; flat account required |
| `analysis.features_only` | `true` | invariant/supported mode only |
| `broker.provider` | `robinhood` | invariant/supported mode only |
| `broker.account_suffix` | `unset (private local selector)` | restart; flat account required |

## Validation relationships

Capital must be at least $1 and no greater than total exposure; reserve remains at least $20. Loss percentages must be finite and positive; supported limits are per-trade ≤10% and daily ≤25%. Position/attempt counts are positive integers. Battery is an integer 0–100. Nondefault host/clock/execution/connectivity/market-data policies visibly warn; warnings are not automatic approvals.

Heartbeat stale threshold must be at least twice heartbeat interval; watchdog restart threshold exceeds heartbeat/cycle deadlines; startup timeout exceeds MCP initialization timeout. Request timeout is shorter than the cycle deadline. Polling is shorter than heartbeat staleness. Host polling is shorter than host age. Account cache is shorter than account age. Keepalive exceeds idle polling; reconnect delays have valid ordering. Halt recovery is at least as strict as ordinary recovery. Clock sample max age covers sampling interval plus timeout. Session offsets require 0 < liquidation < entry cutoff < 180 minutes, protecting early-close sessions. Unsupported future timestamp tolerance, strategy modes and unsafe execution modes are rejected. Export paths must remain relative under `reports/`.

`MAX_CONSECUTIVE_LOSSES` is not implemented. There is no meaningless configuration key for it. Reconciliation/fill verification occurs every observation cycle, with full broker refresh around each order. `connectivity.poll_seconds` is the active account/position/fill polling target; calls can take longer and cycle/heartbeat gates then fail closed. Initialization may retry indefinitely with bounded delay while read-only/blocked; there is no blind mutation retry count. The watchdog restart budget bounds process restarts.

## Common settings

All changes use `python -m trader config set KEY VALUE`. Strings/decimals need shell quoting as normal; arrays and booleans are JSON. Examples below do not enable LIVE and do not clear any controls.

```sh
python -m trader config show
python -m trader config validate
python -m trader config path
python -m trader config reset --dry-run
python -m trader config set-capital 10
python -m trader config set-max-exposure 30
python -m trader config set-max-positions 3
python -m trader config set-trade-loss-percent 1.5
python -m trader config set-daily-loss-percent 5
python -m trader config set-max-attempts 5
python -m trader config set trading.buying_power_reserve 20
python -m trader config set-min-battery 50
python -m trader config set host.require_ac_power false
python -m trader config set host.require_lid_open true
python -m trader config set host.require_sleep_prevention true
python -m trader config set-heartbeat-interval 5
python -m trader config set watchdog.heartbeat_stale_seconds 15
python -m trader config set watchdog.restart_limit 3
python -m trader config set watchdog.restart_window_seconds 300
python -m trader config set connectivity.recovery_observations 30
python -m trader config set connectivity.recovery_min_seconds 300
python -m trader config set market_data.max_quote_age_seconds 5
python -m trader config set market_data.max_spread_percent 0.05
python -m trader config set clock.max_combined_bound_ms 50
python -m trader config set clock.sample_interval_seconds 60
python -m trader config set session.entry_cutoff_minutes_before_close 60
python -m trader config set session.liquidation_minutes_before_close 15
python -m trader config set trading.symbols '["SPY", "QQQ"]'
```

The clock source/count/mode are fixed to the supported production measurement implementation; alternative sources/counts are diagnostic/research work, not silently activated alternatives. The configurable combined bound is **ADVANCED / SAFETY CRITICAL**; any nondefault value raises a visible warning and changes the fingerprint. The default remains 50 ms; a network change does not automatically relax this limit. There is no separately applied offset/uncertainty cap, so misleading unused keys are not exposed.

Market data uses venue timestamps, not the request completion time. Quote age, NTP offset, NTP uncertainty and network RTT must remain distinct. Clock failure blocks entries/opening health but not unrelated account reads, feature logging or independently safe exits.
