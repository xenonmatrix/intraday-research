"""Adapter for the frozen plain ORB rules; no parameter optimization."""
from datetime import timedelta
from .models import D, dt, iso, Config
from .strategy import evaluate as validate_and_vwap


def evaluate(symbol, raw_bars, session, now, cfg=None):
    cfg = cfg or Config()
    # Reuse strict regular-session OHLCV validation and causal VWAP calculation.
    state = validate_and_vwap(symbol, raw_bars, session, now)
    state.update(strategy='orb', pullback_high=None, pullback_low=None,
                 trigger=None, invalidation=None, target=None, time_exit=None, setup_id=None)
    if state['opening_high'] is None:
        return state
    trigger = D(state['opening_high']) + cfg.breakout_offset
    stop = D(state['opening_low']) - cfg.stop_offset
    completed = [b for b in raw_bars if session.open+timedelta(minutes=15) <= dt(b['begins_at'])
                 and dt(b['begins_at'])+timedelta(minutes=5) <= min(now, session.close)]
    crossed = any(D(b['high_price']) >= trigger or D(b['low_price']) <= stop for b in completed)
    state.update(state='MISSED' if crossed else 'ARMED', trigger=str(trigger), invalidation=str(stop),
                 target=str(trigger+cfg.target_r*(trigger-stop)), setup_id=f'{session.day}:{symbol}:orb:2',
                 time_exit=None)  # Hard 30-minute deadline starts at actual fill.
    return state
