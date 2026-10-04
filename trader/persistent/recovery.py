"""Versioned recovery decisions. This module never calls a broker or clears an intent.

The default policy is the historical verified-exit policy. Bounded recovery is
opt-in and requires fresh, positive evidence for every certification condition.
"""
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import math
import re
import hashlib
import json
from pathlib import Path
from dataclasses import asdict

import httpx

from ..diagnostics import exception_record
from ..models import Unsafe, dt, iso

VERSION = 'position-recovery-1'


class Failure(StrEnum):
    TRANSIENT_READ_TRANSPORT_FAILURE = 'TRANSIENT_READ_TRANSPORT_FAILURE'
    REPEATED_BROKER_COMMUNICATION_FAILURE = 'REPEATED_BROKER_COMMUNICATION_FAILURE'
    RECONCILIATION_FAILURE = 'RECONCILIATION_FAILURE'
    AMBIGUOUS_MUTATING_ORDER_STATE = 'AMBIGUOUS_MUTATING_ORDER_STATE'
    STALE_OR_UNAVAILABLE_MARKET_DATA = 'STALE_OR_UNAVAILABLE_MARKET_DATA'
    WORKER_PROCESS_FAILURE = 'WORKER_PROCESS_FAILURE'
    WATCHDOG_RESTART = 'WATCHDOG_RESTART'
    PROLONGED_CONNECTIVITY_LOSS = 'PROLONGED_CONNECTIVITY_LOSS'
    COMPLETE_INTERNET_LOSS = 'COMPLETE_INTERNET_LOSS'
    AUTHENTICATION_FAILURE = 'AUTHENTICATION_FAILURE'
    OWNERSHIP_POSITION_MISMATCH = 'OWNERSHIP_POSITION_MISMATCH'
    CLOCK_HEALTH_FAILURE = 'CLOCK_HEALTH_FAILURE'


class PositionState(StrEnum):
    REBUILDING = 'REBUILDING'
    NORMAL_MANAGEMENT = 'NORMAL_MANAGEMENT'
    VERIFIED_RISK_REDUCTION = 'VERIFIED_RISK_REDUCTION'


class RecoveryError(Unsafe):
    """Classify at the component that has evidence, retaining the cause."""
    def __init__(self, kind, message):
        self.failure_class = Failure(kind)
        super().__init__(message)


REQUIRED_EVIDENCE = (
    'worker_healthy', 'watchdog_healthy', 'connection_restored',
    'authenticated', 'account_confirmed', 'ownership_reconciled',
    'quantity_reconciled', 'available_to_sell_reconciled',
    'outstanding_orders_reconciled', 'no_ambiguous_mutation',
    'fresh_quote', 'quote_timestamp_valid', 'completed_bars_restored',
    'derived_state_restored', 'clock_healthy', 'durable_stop_known',
    'durable_target_known', 'original_hold_deadline_known',
    'eod_deadline_known', 'risk_consistent',
)


@dataclass(frozen=True)
class RecoveryPolicy:
    mode: str = 'conservative_exit'
    max_seconds: float = 15

    def __post_init__(self):
        if self.mode not in ('conservative_exit', 'bounded'):
            raise Unsafe('Unknown position recovery mode')
        if (type(self.max_seconds) not in (float, int)
                or not math.isfinite(self.max_seconds) or not 0 < self.max_seconds <= 15):
            raise Unsafe('Position recovery budget must be positive and at most 15 seconds')

    def budget(self, config):
        # Never more than three quote-validity windows or one monitored cycle.
        return min(self.max_seconds, 3 * config['market_data']['max_quote_age_seconds'],
                   config['watchdog']['cycle_deadline_seconds'])

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(dict(version=VERSION,**asdict(self)),sort_keys=True).encode()).hexdigest()


def load_policy(root):
    """Optional, private, startup-only policy. Missing file retains forced exits."""
    path=Path(root)/'config/position-recovery.json'
    if not path.exists():return RecoveryPolicy()
    if path.is_symlink():raise Unsafe('Recovery policy symlinks are unsupported')
    from ..configuration import _pairs
    try:
        value=json.loads(path.read_text(),object_pairs_hook=_pairs)
        if type(value) is not dict or set(value)!={'mode','max_seconds'}:
            raise Unsafe('Recovery policy requires exactly mode and max_seconds')
        return RecoveryPolicy(**value)
    except (OSError,ValueError,TypeError) as exc:
        raise Unsafe('Invalid position recovery policy') from exc


def _leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            yield from _leaves(child)
    else:
        yield exc
        if exc.__cause__ is not None and exc.__cause__ is not exc:
            yield from _leaves(exc.__cause__)


def classify(exc, context=None, *, repeated=False, explicit=None):
    """Unknown failures default to failed reconciliation, never a benign read.

    Internet loss requires explicit independent connectivity evidence; one
    connection exception alone cannot establish that the entire internet is down.
    """
    context = context or {}
    leaves = list(_leaves(exc))
    tool = context.get('tool') or getattr(exc,'tool',None)
    mutation = tool in ('place_equity_order', 'cancel_equity_order')
    operation = 'MUTATION' if mutation else 'READ' if tool and tool.startswith(('get_', 'review_')) else 'UNKNOWN'
    if explicit is not None:
        kind = Failure(explicit)
    elif mutation:
        kind = Failure.AMBIGUOUS_MUTATING_ORDER_STATE
    elif any(isinstance(e, RecoveryError) for e in leaves):
        kind = next(e.failure_class for e in leaves if isinstance(e, RecoveryError))
    elif any(isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403) for e in leaves):
        kind = Failure.AUTHENTICATION_FAILURE
    elif operation == 'READ' and any(isinstance(e, (httpx.TransportError, ConnectionError, TimeoutError)) for e in leaves):
        kind = Failure.REPEATED_BROKER_COMMUNICATION_FAILURE if repeated else Failure.TRANSIENT_READ_TRANSPORT_FAILURE
    else:
        kind = Failure.RECONCILIATION_FAILURE
    diagnostic = dict(version=VERSION, failure_class=kind.value, tool=tool,
                      operation=operation, exception=exception_record(exc))
    for key in ('connection_id', 'connection_age_seconds', 'successful_calls',
                'reconnect_count', 'http_phase'):
        diagnostic[key] = context.get(key)
    messages = ' '.join(str(e) for e in leaves)
    diagnostic['goaway'] = bool(re.search(r'GOAWAY|ConnectionTerminated', messages, re.I))
    match = re.search(r'last_stream_id\s*[:=]\s*(\d+)', messages)
    diagnostic['last_stream_id'] = int(match[1]) if match else None
    return diagnostic


def begin(existing, failure, now, monotonic_now, budget, *, conservative=False):
    """Original deadline is immutable within an episode, including retries."""
    if existing and existing['state'] != PositionState.NORMAL_MANAGEMENT:
        return dict(existing, failures=existing['failures'] + 1, last_failure=failure,
                    state=(PositionState.VERIFIED_RISK_REDUCTION.value
                           if conservative or failure['failure_class'] != Failure.TRANSIENT_READ_TRANSPORT_FAILURE
                           else existing['state']))
    immediate = conservative or failure['failure_class'] != Failure.TRANSIENT_READ_TRANSPORT_FAILURE
    return dict(version=VERSION, state=(PositionState.VERIFIED_RISK_REDUCTION.value if immediate
                                       else PositionState.REBUILDING.value),
                started_at=iso(now), deadline=iso(now + timedelta(seconds=budget)),
                started_monotonic=monotonic_now, budget_seconds=budget, failures=1,
                last_failure=failure, missing=list(REQUIRED_EVIDENCE))


def advance(state, now, monotonic_now, evidence, *, protective_reason=None):
    """Returns (durable state, exit reason). An exit decision never submits an order.

    Missing, stale, partial or non-boolean evidence cannot certify recovery.
    Clock reversal and host sleep conservatively exhaust the recovery budget.
    An expired episode cannot be rehabilitated by a later healthy observation.
    """
    if not state:
        return None, protective_reason
    state = dict(state)
    if protective_reason:
        state.update(state=PositionState.VERIFIED_RISK_REDUCTION.value,
                     exit_reason=protective_reason)
        return state, protective_reason
    if state['state'] == PositionState.NORMAL_MANAGEMENT:
        return state, None
    if state['state'] == PositionState.VERIFIED_RISK_REDUCTION:
        return state, state.get('exit_reason', 'RECOVERY_EXIT')
    elapsed = monotonic_now - state['started_monotonic']
    wall_elapsed = (now - dt(state['started_at'])).total_seconds()
    # Compare both clocks: neither a backward clock step nor sleep can buy time.
    if (not math.isfinite(elapsed) or elapsed < 0 or wall_elapsed < 0
            or abs(wall_elapsed - elapsed) > 1
            or elapsed >= state['budget_seconds'] or now >= dt(state['deadline'])):
        state.update(state=PositionState.VERIFIED_RISK_REDUCTION.value,
                     exit_reason='RECOVERY_DEADLINE')
        return state, 'RECOVERY_DEADLINE'
    missing = [key for key in REQUIRED_EVIDENCE if evidence.get(key) is not True]
    state['missing'] = missing
    if not missing:
        state.update(state=PositionState.NORMAL_MANAGEMENT.value, certified_at=iso(now))
    return state, None


def entry_blocked(session):
    """Position certification never clears the independent entry recovery hold."""
    return bool(session.get('recovery_active', True) or session.get('connectivity_halt', True))
