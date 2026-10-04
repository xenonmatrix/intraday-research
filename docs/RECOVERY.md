# Position recovery version 1

The deployed service must be distinguished from the source tree. Installing this
source does not replace a running Python worker. No restart or policy activation
is part of the recovery implementation tests. The default remains
`conservative_exit`; the bounded policy has deterministic test evidence, not
live execution evidence.

## Failure classification

`persistent.recovery.Failure` enumerates transient read transport failure,
repeated communication failure, reconciliation failure, ambiguous mutation,
unavailable market data, worker failure, watchdog restart, prolonged loss,
complete internet loss, authentication failure, ownership mismatch and clock
failure. Classification retains redacted exception trees, tool/operation,
connection age, successful calls, reconnect count, HTTP phase and GOAWAY stream
ID where available. Unknown evidence remains failed reconciliation. Complete
internet loss requires independent evidence; one failed endpoint cannot prove it.

Transport diagnostics capture the HTTP phase separately from local heartbeat.
HTTP/2 GOAWAY is connection-level signaling and does not establish whether a
particular business mutation completed. See [RFC 9113 section 6.8](https://www.rfc-editor.org/rfc/rfc9113.html#name-goaway)
and the [HTTPX exception hierarchy](https://www.python-httpx.org/exceptions/).
The local heartbeat interval is unchanged.

## Independent state

Entry recovery continues to use durable session `recovery_active` and
`connectivity_halt` holds, timed healthy observations and full existing entry
gates. The stability timer starts at the first healthy observation, not the last
failure. Position certification never clears these entry holds.

Every owned trade independently stores `position_recovery`, its original failure
time, monotonic start, immutable wall deadline, failure history/diagnostics,
missing certification evidence and transition state:

```
transient READ failure -> REBUILDING -> NORMAL_MANAGEMENT
                              |
                deadline / repeated failure / protection
                              |
                    VERIFIED_RISK_REDUCTION
```

Nontransient failures and the default conservative policy go directly to verified
risk reduction. This is an exit decision, not permission to send an order.
Existing review, ownership, sellable quantity, quote and mutation guards still
apply. A blocked exit remains recorded; the state machine does not promise an
exit during network loss, closed markets or unknown ownership.

## Bounded policy

Optional startup-only private file `config/position-recovery.json`:

```json
{"mode": "conservative_exit", "max_seconds": 15}
```

Missing file retains the conservative policy. The example is safe to commit;
the local policy is ignored. `bounded` is the explicit experimental alternative.
Unknown keys, duplicate keys, nonfinite/boolean durations, nonpositive budgets,
symlinks and budgets above 15 seconds are rejected. The policy fingerprint is
recorded at worker startup and in status. Changes require a controlled restart;
there is no hot reload.

Actual budget is the minimum of the configured duration, three configured quote
freshness windows, and one monitor-cycle deadline. At baseline settings that is
15 seconds. This cap limits the period without fully rebuilt strategy context;
it is not inferred from a single observed reconnect and is not an empirical
guarantee against loss. If connection initialization takes longer, the policy
expires. Both monotonic elapsed time and the original wall deadline are checked;
clock reversal, a clock discrepancy over one second, or sleep consuming the
budget causes verified risk reduction. Retries never extend an episode.

Normal management requires all twenty positive conditions named in
`REQUIRED_EVIDENCE`. These cover worker/watchdog, connection/auth/account,
ownership/quantity/available quantity/orders, absence of ambiguous mutations,
fresh valid quotes, latest completed contiguous regular-session bars, rebuilt
VWAP when required, host/clock health, durable stop/target/hold/EOD deadlines,
and current risk consistency. Cached account observation alone cannot certify:
the broker snapshot must be freshly rebuilt. Missing or non-boolean evidence
fails certification. VWAP remains an OHLCV typical-price approximation.

Stop, target, VWAP, maximum hold, EOD, emergency and risk exits are evaluated
before recovery. The original position parameters never move. A restart always
retains the conservative exit requirement, even if an earlier process stored a
bounded episode. A later healthy observation cannot erase an expired exit decision.

## Ambiguous mutations

Buy, sell and cancel acknowledgements are never blindly retried. Existing UUID
intents and known broker IDs survive failures. For an unknown submission ID,
reconciliation may identify an order only by the exact broker-returned `ref_id`,
with matching account, symbol, side, regular-hours market/GFD shape and original
dollar amount or sell quantity. Duplicate or inconsistent matches fail closed.
Absence of a match never proves that submission failed. Actual executions are
ingested idempotently before position reconciliation. A cancel already attempted
is queried until terminal truth is available; it is not replayed automatically.

## Verification and deployment boundary

`tests/test_position_recovery.py` exercises every mandatory proof, read and
mutation GOAWAY, retries, expired recovery, per-symbol isolation, incomplete
derived data, clock/sleep discontinuities, partial fills, ownership/available
quantity problems, protective precedence, exact-ID acknowledgement resolution,
and duplicate prevention with temporary databases and fake brokers. Existing
process crash, watchdog, HTTP replay and consumed-setup tests remain required.

Before deployment: run the full suite, inspect the exact source/version and
policy, obtain fresh broker positions/orders and reconciliation, and defer any
restart while an owned real position exists. Preserve all production state.
Retain the conservative default until the bounded policy has been reviewed
against execution evidence. No test submits a real broker order.
