"""Deterministic arbitration, independent from arrival order and recent P&L."""
from dataclasses import dataclass
from ..models import dt


@dataclass(frozen=True)
class Arbitration:
    selected: tuple
    rejected: tuple


def resolve(signals,now,*,occupied=(),priorities=None,eligible_ids=None):
    priorities=priorities or {};occupied=set(occupied);groups={};rejected=[];selected=[]
    for signal in sorted(signals,key=lambda s:(s.symbol,s.strategy_id,s.setup_id)):
        reason=None
        if signal.symbol in occupied:reason='SYMBOL_OPEN_OR_PENDING'
        elif eligible_ids is not None and signal.strategy_id not in eligible_ids:reason='STRATEGY_INELIGIBLE'
        elif dt(signal.signal_at)>now or now>=dt(signal.valid_until):reason='SIGNAL_TIME_INVALID'
        elif signal.direction not in ('LONG','SHORT'):reason='DIRECTION_INVALID'
        if reason:rejected.append(dict(signal=signal.audit(),reason=reason))
        else:groups.setdefault(signal.symbol,[]).append(signal)
    for symbol,group in sorted(groups.items()):
        if len({s.direction for s in group})>1:reason='CONTRADICTORY_SIGNALS_NO_TRADE';winner=None
        elif group[0].direction!='LONG':reason='SHORT_EXECUTION_UNSUPPORTED';winner=None
        else:
            highest=min(priorities.get(s.strategy_id,100) for s in group)
            top=[s for s in group if priorities.get(s.strategy_id,100)==highest]
            winner=top[0] if len(top)==1 else None
            reason='LOWER_PREDECLARED_PRIORITY' if winner else 'EQUAL_PRIORITY_AMBIGUITY_NO_TRADE'
        for signal in group:
            if signal is winner:selected.append(signal)
            else:rejected.append(dict(signal=signal.audit(),reason=reason))
    return Arbitration(tuple(selected),tuple(rejected))
