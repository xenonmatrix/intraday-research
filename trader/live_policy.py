"""Date-bound authorization and non-expandable live limits."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from .calendar import Calendar
from .models import UTC, NY, D, Unsafe, dt


class LivePolicy:
    def __init__(self, project, clock=None):
        self.project = Path(project)
        self.runtime = self.project / "runtime"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.calendar = Calendar()
        try:
            raw = (self.project / "live-session.json").read_bytes()
        except FileNotFoundError as exc:
            raise Unsafe("Reviewed live-session.json is missing") from exc
        self.digest = hashlib.sha256(raw).hexdigest()
        self.settings = json.loads(raw)
        expected = {"capital_envelope":"10.00", "entry_notional_ceiling":"9.00", "planned_loss_per_trade":"0.10", "gross_realized_loss_limit":"0.50", "maximum_positions":1, "approval_required":True}
        if any(self.settings.get(k) != v for k,v in expected.items()):
            raise Unsafe("Live policy limits changed; a new reviewed implementation is required")
        self.day = self.settings["session_date"]
        self.strategy = self.settings.get('strategy', 'existing_orb_pullback_vwap')
        if self.strategy not in ('orb', 'existing_orb_pullback_vwap'):
            raise Unsafe('Unknown reviewed strategy')
        self.session = self.calendar.session(dt(self.day+"T16:00:00Z"))
        if not self.session:
            raise Unsafe("Configured live date is not an exchange session")

    def approval(self):
        if self.clock() >= self.session.close or (self.runtime/'unattended'/self.day/'expired.json').exists():
            raise Unsafe("LIVE authorization EXPIRED at regular-session close")
        path = self.runtime / "live-approval.json"
        if not path.exists():
            raise Unsafe("LIVE DISARMED: explicit approval is absent")
        a = json.loads(path.read_text())
        if a.get("approved") is not True or a.get("session_date") != self.day or a.get("policy_sha256") != self.digest:
            raise Unsafe("Approval does not match reviewed session/policy")
        if a.get("account_suffix") != self.settings.get("account_suffix"):
            raise Unsafe("Approval account mismatch")
        return a

    def authorize(self, side, now=None):
        now = now or self.clock()
        a = self.approval()
        if (self.runtime / 'MAINTENANCE').exists():
            raise Unsafe('Read-only maintenance: all broker mutations disabled')
        if (self.runtime / "STOP").exists():
            raise Unsafe("Trading process is stopped")
        actual_session = self.calendar.session(now)
        if not actual_session or actual_session.day != self.day or not actual_session.contains(now):
            raise Unsafe("Outside the authorized regular session")
        if side == "buy":
            if not actual_session.entry_window(now):
                raise Unsafe("Outside live entry window")
            if any((self.runtime / p).exists() for p in ("KILL", "ENTRIES_DISABLED", "LIQUIDATE")):
                raise Unsafe("New live entries disabled")
            from .unattended import Unattended
            Unattended(self).entry_check()
        elif side != "sell":
            raise Unsafe("Only long equity buy/sell actions permitted")
        return a

    def validate_order_shape(self, args):
        allowed = {"account_number", "symbol", "side", "type", "market_hours", "time_in_force", "ref_id", "dollar_amount", "quantity"}
        if set(args)-allowed or args.get("type") != "market" or args.get("market_hours") != "regular_hours" or args.get("time_in_force") != "gfd":
            raise Unsafe("Only regular-session GFD equity market orders allowed")
        if not args.get("ref_id"):
            raise Unsafe("Order UUID required")
        if args.get("side") == "buy":
            if "quantity" in args or not D("1") <= D(args.get("dollar_amount", "0")) <= D("9"):
                raise Unsafe("Live entry must be dollar-based, $1 through $9")
        elif args.get("side") == "sell":
            q = D(args.get("quantity", "0"))
            if "dollar_amount" in args or not q.is_finite() or q <= 0 or q.as_tuple().exponent < -6:
                raise Unsafe("Exit must specify verified positive share quantity")
        else:
            raise Unsafe("Invalid side")
