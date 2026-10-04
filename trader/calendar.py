from dataclasses import dataclass
from datetime import timedelta
import pandas_market_calendars as mcal
from .models import NY, Unsafe


@dataclass(frozen=True)
class Session:
    day: str
    open: object
    close: object
    opening_minutes: int = 15
    entry_cutoff_minutes: int = 60
    liquidation_minutes: int = 15

    def contains(self, now):
        return self.open <= now < self.close

    def entry_window(self, now):
        return self.open + timedelta(minutes=self.opening_minutes) <= now < self.close - timedelta(minutes=self.entry_cutoff_minutes)


class Calendar:
    """Intersection of NYSE/Nasdaq sessions, including holidays and early closes."""
    def __init__(self, config=None):
        self.config = config
        self.calendars = [mcal.get_calendar("NYSE"), mcal.get_calendar("NASDAQ")]
        self.cache = {}

    def session(self, now):
        day = now.astimezone(NY).date().isoformat()
        if day not in self.cache:
            rows = [c.schedule(day, day) for c in self.calendars]
            if any(r.empty for r in rows):
                self.cache[day] = None
            else:
                opens = [r.iloc[0].market_open.to_pydatetime() for r in rows]
                closes = [r.iloc[0].market_close.to_pydatetime() for r in rows]
                opening, closing = max(opens), min(closes)
                if opening >= closing:
                    raise Unsafe("Conflicting exchange calendars")
                self.cache[day] = Session(day, opening, closing, **(dict(opening_minutes=self.config["strategy"]["opening_range_minutes"],entry_cutoff_minutes=self.config["session"]["entry_cutoff_minutes_before_close"],liquidation_minutes=self.config["session"]["liquidation_minutes_before_close"]) if self.config else {}))
        return self.cache[day]
