"""Validated durable settings. All money is Decimal, independent of account equity."""
from dataclasses import dataclass, asdict
from decimal import Decimal
from ..models import D, Unsafe

@dataclass(frozen=True)
class Settings:
    capital_per_trade: str = '10'
    max_open_positions: int = 3
    max_total_exposure: str = '30'
    max_loss_per_trade_percent: str = '1.5'
    max_daily_loss_percent: str = '5'
    max_entry_attempts_per_day: int = 5
    buying_power_reserve: str = '20'
    live_enabled: bool = False
    account_suffix: str = ''

    def validate(self):
        for key in ('capital_per_trade','max_total_exposure','max_loss_per_trade_percent','max_daily_loss_percent','buying_power_reserve'):
            try: value=D(getattr(self,key))
            except Exception as exc: raise Unsafe('Invalid numeric setting: '+key) from exc
            if not value.is_finite() or value <= 0: raise Unsafe('Setting must be finite and positive: '+key)
        if D(self.capital_per_trade)<1: raise Unsafe('Capital per trade must be at least $1')
        if any(D(getattr(self,k))>100 for k in ('max_loss_per_trade_percent','max_daily_loss_percent')): raise Unsafe('Loss percentages cannot exceed 100')
        if D(self.buying_power_reserve)<20: raise Unsafe('Minimum unleveraged reserve is $20')
        for key in ('max_open_positions','max_entry_attempts_per_day'):
            if type(getattr(self,key)) is not int or getattr(self,key)<1: raise Unsafe('Setting must be a positive integer: '+key)
        if type(self.live_enabled) is not bool or (self.account_suffix and (not self.account_suffix.isdigit() or len(self.account_suffix)!=4)): raise Unsafe('Invalid enablement/account setting')
        return self

    @property
    def trade_loss(self): return D(self.capital_per_trade)*D(self.max_loss_per_trade_percent)/100
    @property
    def daily_loss(self): return D(self.max_total_exposure)*D(self.max_daily_loss_percent)/100
    def display(self):
        return dict(asdict(self),max_loss_per_trade_dollars=str(self.trade_loss),
                    max_daily_loss_dollars=str(self.daily_loss),daily_loss_base='max_total_exposure')
