from datetime import timedelta
from .models import D, decimal, dt, iso, Unsafe


def evaluate(symbol, raw_bars, session, now):
    """Deterministically replay completed bars; persist result on every poll.

    VWAP is an OHLCV typical-price approximation, not tick-exact exchange VWAP.
    Missing/interpolated opening or later bars invalidate the candidate.
    """
    bars = sorted([b for b in raw_bars if session.open <= dt(b["begins_at"]) and dt(b["begins_at"]) + timedelta(minutes=5) <= min(now, session.close)], key=lambda b: b["begins_at"])
    state = dict(symbol=symbol, session=session.day, state="WAIT_OR", opening_high=None,
                 opening_low=None, vwap=None, pullback_high=None, pullback_low=None,
                 trigger=None, invalidation=None, target=None, time_exit=None,
                 session_dollars="0", last_bar=None, setup_id=None)
    cumulative, volume, vwaps = D(0), D(0), []
    opening = []
    for i, b in enumerate(bars):
        start = dt(b["begins_at"])
        if start != session.open + timedelta(minutes=5*i) or b.get("interpolated") or b.get("session") not in ("reg", None, ""):
            raise Unsafe(f"Incomplete regular-session bars: {symbol}")
        op, high, low, close, vol = [decimal(b[k]) for k in ("open_price", "high_price", "low_price", "close_price", "volume")]
        if not (0 < low <= min(op, close) <= max(op, close) <= high) or vol <= 0:
            raise Unsafe("Invalid OHLCV")
        cumulative += (high + low + close) / 3 * vol
        volume += vol
        vwap = cumulative / volume
        vwaps.append(vwap)
        state.update(vwap=str(vwap), session_dollars=str(cumulative), last_bar=iso(start))
        if i < 3:
            opening.append((high, low))
            if i == 2:
                state.update(opening_high=str(max(x[0] for x in opening)), opening_low=str(min(x[1] for x in opening)), state="WAIT_BREAKOUT")
            continue
        oh = D(state["opening_high"])
        if state["state"] in ("INVALIDATED", "MISSED"):
            state.update(state="WAIT_BREAKOUT", pullback_high=None, pullback_low=None,
                         trigger=None, invalidation=None, target=None, time_exit=None, setup_id=None)
        if state["state"] == "ARMED":
            if low <= D(state["invalidation"]) or close < vwap:
                state["state"] = "INVALIDATED"
            elif high >= D(state["trigger"]):
                state["state"] = "MISSED"  # No retrospective entry into a completed bar.
            continue
        if state["state"] == "WAIT_PULLBACK":
            if close < vwap or close < oh:
                state["state"] = "WAIT_BREAKOUT"
            elif low <= oh and close >= oh and close > vwap:
                trigger, stop = high + D("0.01"), low - D("0.01")
                state.update(state="ARMED", pullback_high=str(high), pullback_low=str(low),
                             trigger=str(trigger), invalidation=str(stop), target=str(trigger+2*(trigger-stop)),
                             time_exit=iso(start+timedelta(minutes=35)), setup_id=f"{session.day}:{symbol}:{iso(start)}")
            continue
        if state["state"] == "WAIT_BREAKOUT" and close > oh and close > vwap and vwap > vwaps[i-3]:
            state["state"] = "WAIT_PULLBACK"
    if bars and now < session.close and (now - (dt(bars[-1]["begins_at"])+timedelta(minutes=5))).total_seconds() > 330:
        raise Unsafe(f"Stale bars: {symbol}")
    return state
