# Multi-strategy research and guarded integration

Version 0.2 implements seven independent long-only hypotheses, deterministic
features, explainable regime labels, a minute-bar replay, quote-based shadow
books and an opt-in coordinator. No new strategy is enabled by installation.
The original Coordinator remains the default when private
`config/multistrategy.json` is absent. The documented software risk defaults apply.

## Evidence and reproduction

`RESEARCH_PROTOCOL.json` freezes dates, costs, defaults, sensitivity and gates.
The FirstRate 2022-09-30–2023-09-29 samples were previously researched. Results
are chronological replication, **not an untouched final holdout**. Original CSVs
are private/local and not distributed. Run from the project root:

```sh
.venv/bin/python -m trader.multistrategy.study \
  --raw research/extended/raw \
  --output reports/multistrategy-study
.venv/bin/python -m pytest -q
```

The runner freezes the protocol before calculating returns, hashes source/data,
excludes incomplete paired sessions, saves every simulated trade and daily book,
reports diagnostics and representative rejected candidates, and separates
train, validation and test. It does not infer validation from the best result.
New study runs record their code commit and source digest. The distributed
aggregate results are a sanitized summary: original build identifiers are omitted,
and they are not represented as a fresh run of this release. Numerical results
and methodological limitations are retained without deployment attestations.

Fixed defaults are evaluated through expanding prior-session history and monthly
forward blocks. There is no randomly shuffled split or fitted normalization.
Sensitivity runs exclude the final period. Additional filter ablations and retest
timeout comparisons are explicitly exploratory development-only diagnostics,
not predeclared gate evidence. No primary parameter was selected from final results.

## Features

All inputs use timezone-aware session times from the NYSE/Nasdaq intersection.
A five-minute bar becomes available at its start plus five minutes. Missing,
misaligned, duplicate, nonfinite, nonpositive-volume and invalid OHLC bars fail
closed. Only bars ending at or before the evaluation time enter calculations.
Cross-symbol features require both symbols to have the same completed coverage.
The current minute and future session totals never enter a five-minute signal.

* OR5/15/30: max high and min low of the first 1/3/6 completed bars;
  width, width/price, width/ATR, width/(price × realized volatility), distance
  above high and distance/width breakout strength.
* VWAP approximation: sum[((H+L+C)/3) × V]/sum(V). This is not trade VWAP.
  Slopes are changes over 5/15/30 minutes; distance in dollars, percent, ATR
  and trailing 20-close standard deviations; crossings, reclaim/loss and age.
* Volume: completed and cumulative volume; previous-bar ratio and change in
  that ratio; relative volume against the same elapsed slot of up to 20 prior
  complete sessions, requiring at least five comparables. Opening RVOL compares
  the first three bars. These are distinct from previous-bar volume ratios.
* Gap: actual previous calendar-session regular close; dollars, percent,
  direction, fill fraction, open/closed and normalization by prior daily ATR
  or return standard deviation. Missing previous sessions never substitute the
  last observed session. Daily ATR needs 14 contiguous daily true ranges;
  daily return volatility needs 20 contiguous prior returns.
* Momentum: completed-close 5/15-minute returns, signed consecutive candles,
  body/range, close position and return acceleration. One/three-minute returns
  require actual minute inputs; they are unavailable in the live five-minute feed.
* Volatility: mean last 14 intraday true ranges (simple ATR, not Wilder smoothing),
  sqrt(sum(log return²)) realized volatility, opening volatility, range expansion,
  and percentile against prior sessions at the same elapsed time. No future days
  or training/test mixing enters these reference distributions.
* Cross-asset: both above/below VWAP/OR, simultaneous breakout, disagreement,
  session relative strength and 15-minute relative momentum. Missing peer
  coverage makes these unavailable.

Unavailable values are null and listed explicitly. Strategies cannot silently
impute missing history. Current factory integration has no certified live prior-day
history feed; this is an explicit live-feature-parity gate failure for dependent
strategies. FeatureEngine accepts verified historical MarketDay records for a
future certified feed; old vendor data is not automatically used as live context.

## Strategy rules and parameters

All seven use an objectively completed signal, a long market-entry trigger,
$0.01 offsets, 2R target, 30-minute maximum hold and session cutoff/EOD policy.
Existing entry band, spread/friction, fractional minimum and deterministic sizing
still apply. Stops are fixed on the setup; fill prices determine the actual 2R
price. No shorts are claimed or implemented.

| ID | Required evidence and entry | Stop and invalidation |
|---|---|---|
| orb | Existing production `orb.evaluate`, first 15-minute high + $0.01 | OR low − $0.01; any completed prior trigger/stop touch consumes setup |
| filtered_orb | ORB plus above VWAP, positive 5-minute VWAP slope/return, TOD RVOL ≥1, nonnegative gap, prior volatility percentile 10–90%, both assets above VWAP | Same ORB stop/consumption |
| orb_retest | First completed close above OR high + $0.01; within next 6 bars a low within 0.1 OR-width of high and a positive candle closing above OR high; trigger retest-bar high + $0.01 | Retest low − $0.01; OR low violation invalidates; no confirmation after two additional completed bars or prior trigger/stop touch |
| vwap_trend | Positive 15-minute VWAP slope/return, TOD RVOL ≥1, completed low touches VWAP +0.1 intraday ATR, positive close above that level; trigger bar high +$0.01 | Signal bar low −$0.01 |
| vwap_reversion | Below VWAP by ≥2 trailing-close standard deviations; absolute 15-minute VWAP slope ≤0.25 ATR; declining volume, positive acceleration and positive candle; VWAP must allow 2R reward | Signal bar low −$0.01; no below-VWAP exit, which would invalidate this hypothesis by construction |
| gap_continuation | Positive gap ≥0.1 prior daily ATR, still open, first 90 minutes; price above VWAP and OR, positive 15-minute return, TOD RVOL ≥1, both above VWAP | Signal bar low −$0.01 |
| gap_fade | Down gap ≤−0.1 prior daily ATR, still open, first 120 minutes; VWAP reclaim, positive 5-minute return, TOD RVOL ≥1; previous close permits 2R | Signal bar low −$0.01; no below-VWAP exit |

Each setup contains strategy/version/parameter fingerprint, session, symbol and
causal anchor. A consumed setup cannot be rearmed. The existing ORB is the control;
minute-open observations cannot reproduce a live two-second quote stream. The
replay must therefore not be treated as exact historical production performance.

## Replay assumptions and limits

Signals use completed five-minute bars; eligibility is observed at minute opens.
A reservation is followed by at least one minute delay (plus scenario latency),
then current entry-band, risk and validity checks. Pending reservations consume
capital, risk, attempt and per-symbol capacity. A stop touch during the pending
interval cancels it without rearming. Prices must never rely on a favorable bar
extreme. Stop/target ambiguity uses stop first; gaps through stops fill worse.
Remaining partial quantity is assumed cancelled without another submission.
Actual broker cancel latency and queue priority cannot be inferred from OHLCV.

Base: 1bp total spread, 1bp slippage each side, zero assumed cash fee. Stress:
5bp spread, 2bp slippage, $0.01 each side, one additional minute. Partial:
base spread/slippage, $0.01 each side, additional minute, 50% fill. The risk engine
retains at least its $0.01-per-side planning buffer even when assumed fees are zero.
These scenarios are not measured historical NBBO/fees. Drawdown includes marked
unrealized equity for full runs; period/subgroup drawdowns use closed trades and
are explicitly weaker bounds. Sharpe is not asserted on these sparse samples.
Day-cluster bootstrap intervals preserve within-day dependence but do not model
serial day dependence; sparse active-day samples yield no numerical interval.

## Regimes and arbitration

`rule-regimes-1` uses multi-label output with exact reasons. Trending up/down
requires agreement of price versus VWAP, 15-minute VWAP slope and 15-minute
return. Range requires ≥2 VWAP crosses and absolute slope ≤0.25 ATR. Historical
80th/20th percentiles label high/low volatility. Opening momentum needs a positive
5-minute return above VWAP within 30 minutes. Material (≥0.1 ATR) gaps label
continuation when still open and extending, failure when ≥50% filled. Missing or
conflicting directional/range/gap evidence adds UNCERTAIN, which blocks all entries.

ORB/filtered/retest allow opening momentum or uptrend; VWAP trend requires
uptrend; VWAP reversion requires range; gap continuation allows its gap label or
uptrend; gap fade allows failure or uptrend. Independent replay reports unfiltered
strategy performance with regime breakdowns. It **does not validate the additional
regime-filtered live combination**; live-feature-parity/validation remain false.

Arbitration groups signals per symbol and sorts deterministically. Existing/pending
ownership blocks a new setup. Opposing directions yield no trade; unsupported
shorts yield no trade. Unique lowest declared priority wins; equal-priority ties
yield no trade. All default priorities are equal. Arrival order and recent profit
never decide. SPY and QQQ remain independent slots under shared atomic limits.

## Shadow and live capability

ShadowObserver receives only bars, quotes and health evidence. It has no broker
object, submit/cancel method or network path. SQLite tables and IDs have `shadow_`
and `shadow:` namespaces in a separate `runtime/shadow/observations.sqlite3`.
It refuses an existing production ledger. Independent virtual books apply the
same $10/$0.15/$30/$1.50/five-attempt/$20-reserve envelope. Duplicate/backward
observations, stale/future quotes, missing bars and unhealthy input fail closed.
Quotes plus assumed slippage generate hypothetical fills, never assertions of
actual execution. Missing close observations cannot fabricate a clean next session.

To observe candidate diagnostics with the same future live feed, copy the safe
example to private `config/multistrategy.json`, keep `mode: BASELINE` and every
`live_enabled: false`, set top-level and desired strategy `shadow_enabled: true`,
and use the controlled restart procedure below. This preserves the existing ORB
broker path and attaches only the simulation observer. Installation does not grant strategy enablement. Standalone ShadowObserver uses `mode: SHADOW`; the LIVE
worker explicitly refuses that mode instead of falling back to live.

MULTI_LIVE selects MultiCoordinator. Each live-enabled row must have LIVE_ELIGIBLE
status, exact parameter fingerprint, protocol/data/report provenance and every
predefined evidence check true. A mere label is insufficient. The same checks run
again during the existing atomic reservation and final broker transport gate.
Research-only, shadow, disabled, stale-version and incomplete-evidence candidates
cannot pass. Final risk/reconciliation/authentication/clock health still veto.
Evidence artifacts require genuine reviewed results; these fields are not an
instruction to assert success. The distributed aggregate backtests do not establish the independent execution
evidence needed to authorize this mode.

## Deployment boundary

The supervisor can restart workers autonomously; operators must inspect its
event log to identify the running generation. Before deployment, independently
reconcile broker positions/orders, ensure the ledger has no ambiguous mutation,
preserve its consistent snapshot and inspect health. If a real position exists,
defer restart. Load new configuration only during a controlled flat-state restart,
then verify service PID, heartbeat, account identity, reconciliation, consumed
setups and effective risk/config fingerprints. Configuration changes during a
MultiCoordinator lifetime block new entries. Position exits continue using durable
state. Newly developed strategies' live flags require explicit operator approval.
The conservative recovery default remains available and unchanged.
