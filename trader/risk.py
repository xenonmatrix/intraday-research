from .models import D, decimal, down, dt, Unsafe, valid_quote

TERMINAL = {"filled", "cancelled", "rejected", "failed", "voided", "partially_filled_rest_cancelled"}


def reconcile(snapshot, expected_account, now, cfg, expected_positions=None):
    age = (now - dt(snapshot["observed_at"])).total_seconds()
    if not 0 <= age <= cfg.account_age:
        raise Unsafe("Account snapshot stale")
    a, p = snapshot["account"], snapshot["portfolio"]
    if a["account_number"] != expected_account or not a.get("agentic_allowed") or a.get("nickname") != "Agentic":
        raise Unsafe("Account identity or eligibility changed")
    if a.get("state") != "active" or a.get("deactivated") or a.get("permanently_deactivated"):
        raise Unsafe("Account restricted/inactive")
    if a.get("type") not in ("cash", "limited_margin"):
        raise Unsafe("Borrowing-capable account not allowed")
    bp = p.get("buying_power")
    if not bp or bp.get("display_currency") != "USD" or p.get("currency") != "USD":
        raise Unsafe("Unknown buying power/currency")
    power = min(decimal(bp["buying_power"]), decimal(bp["unleveraged_buying_power"]))
    if power < 0 or decimal(p["cash"]) < 0:
        raise Unsafe("Cash deficit")
    for k in ("options_value", "crypto_value", "futures_value", "event_contracts_value", "fixed_income_value", "mutual_funds_value"):
        if decimal(p[k]) != 0:
            raise Unsafe("Unsupported account exposure")
    symbols = [r['symbol'] for r in snapshot['positions']]
    if len(symbols) != len(set(symbols)):
        raise Unsafe('Duplicate broker position rows; account state ambiguous')
    positions = {r["symbol"]: decimal(r["quantity"]) for r in snapshot["positions"] if decimal(r["quantity"]) != 0}
    expected = {s: decimal(q) for s, q in (expected_positions or {}).items() if decimal(q) != 0}
    if positions != expected:
        raise Unsafe("Broker/local position mismatch")
    for r in snapshot["positions"]:
        if decimal(r["quantity"]) < 0:
            raise Unsafe("Short position")
    if any(r.get("state") not in TERMINAL for r in snapshot["orders"]):
        raise Unsafe("Working or ambiguous broker order")
    return power


def eligible(t):
    return (t.get("tradeable") is True and t.get("state") == "active" and t.get("country") == "US"
            and t.get("fractional_tradability") == "tradable"
            and any(x.get("account_type") == "individual" and x.get("account_type_tradability") == "tradable" for x in t.get("account_type_tradabilities", []))
            and "regular_hours" not in t.get("internal_halt_sessions", []))


def size(candidate, quote, power, cash, daily, now, cfg):
    bid, ask = valid_quote(quote, now, cfg)
    if (ask-bid)/((ask+bid)/2) > cfg.max_spread:
        raise Unsafe("Spread exceeds configured maximum "+str(cfg.max_spread*100)+"%")
    if decimal(candidate["session_dollars"]) < cfg.min_session_dollars:
        raise Unsafe("Insufficient session dollar volume")
    trigger, stop = D(candidate["trigger"]), D(candidate["invalidation"])
    r = trigger-stop
    if r <= 0 or not trigger <= ask <= trigger+D("0.1")*r:
        raise Unsafe("Outside entry range")
    # Deterministic conservative paper model: buy ask + 1bp, sell bid - 1bp.
    entry = ask * (1+cfg.slip_bps/D(10000))
    cost_per_share = ask-bid + 2*ask*cfg.slip_bps/D(10000)
    if cost_per_share > D("0.1")*r:
        raise Unsafe("Friction too large relative to stop")
    remaining = max(D(0), cfg.daily_loss-D(daily["gross_losses"]))
    risk = min(cfg.trade_risk, remaining) - cfg.fee_buffer*2
    available = min(power, cash) - cfg.reserve - cfg.fee_buffer
    if daily["latched"] or risk <= 0 or available <= 0:
        raise Unsafe("Daily loss or cash reserve gate")
    qty = down(min(cfg.position_cap/entry, available/entry, risk/(entry-stop+entry*cfg.slip_bps/D(10000))))
    notional = down(qty*entry, "0.01")
    qty = down(notional/entry)  # round dollars and shares down, never up
    if qty <= 0 or qty*entry < 1:
        raise Unsafe("Below conservative $1 minimum")
    return qty, entry, qty*entry
