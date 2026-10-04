"""Deterministic minute-open execution with production Decimal sizing.

OHLCV cannot establish NBBO, tick order, queue priority or actual partial fills.
All spreads, latency and fill fractions below are declared scenario assumptions.
"""
from dataclasses import dataclass,asdict,replace
from collections import Counter
from datetime import timedelta
import math,statistics

from ..models import D,Unsafe,iso,dt,down
from ..configuration import DEFAULTS,market_config,fingerprint
from ..persistent.config import Settings
from ..persistent.risk import proposal
from .features import FeatureEngine


@dataclass(frozen=True)
class Costs:
    spread_bps: float = 1
    slippage_bps: float = 1
    fee_per_side: float = 0
    latency_minutes: int = 0
    fill_fraction: float = 1

    def __post_init__(self):
        if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v<0 for v in asdict(self).values()):raise Unsafe('Invalid execution scenario')
        if type(self.latency_minutes) is not int or not 0<=self.fill_fraction<=1 or max(self.spread_bps,self.slippage_bps)>=10000:raise Unsafe('Invalid latency/fill scenario')


def exit_cost(quantity,price,costs):
    """Synthetic spread plus slippage relative to the assumed mid execution level."""
    return quantity*(price/((1-D(str(costs.spread_bps))/20000)*(1-D(str(costs.slippage_bps))/10000))-price)+D(str(costs.fee_per_side))


def slippage_cost(position,quantity,exit_price,costs):
    slip=D(str(costs.slippage_bps))/10000
    entry=D(str(position['entry']))
    return quantity*((entry-entry/(1+slip))+(exit_price/(1-slip)-exit_price))


def quote(mid,now,costs):
    m=D(str(mid));half=D(str(costs.spread_bps))/20000
    return dict(bid_price=str(m*(1-half)),ask_price=str(m*(1+half)),venue_bid_time=iso(now),
                venue_ask_time=iso(now),state='active',has_traded=True)


def size_signal(signal,mid,now,costs,*,exposure=0,allocated_risk=0,gross_losses=0,net_to_date=0,cash=50):
    settings=Settings();candidate=signal.candidate()
    cfg=replace(market_config(DEFAULTS),slip_bps=D(str(costs.slippage_bps)),
                fee_buffer=max(D('.01'),D(str(costs.fee_per_side))),
                target_r=D(str(signal.target_r)),max_hold_minutes=signal.max_hold_minutes,vwap_exit=signal.vwap_exit)
    daily={'loss_budget':'1.5','gross_losses':str(gross_losses),'loss_latched':D(str(gross_losses))>=D('1.5')}
    available=max(D(0),D(30)+min(D(0),D(str(net_to_date)))-D(str(exposure)))
    return proposal(settings,daily,candidate,quote(mid,now,costs),D(str(cash)),now,
                    available,max(D(0),D('1.5')-D(str(gross_losses))-D(str(allocated_risk))),cfg)


def exit_decision(position,bar,index,session,frame,costs):
    half=D(str(costs.spread_bps))/20000;slip=D(str(costs.slippage_bps))/10000
    op,lo,hi=(D(str(bar[k]))*(1-half) for k in ('open_price','low_price','high_price'))
    stop,target=D(position['stop']),D(position['target']);now=session.open+timedelta(minutes=index)
    reason=None;raw=op
    if now>=session.close-timedelta(minutes=session.liquidation_minutes):reason='SESSION_CLOSE'
    elif op<=stop:reason='STOP_GAP'
    elif op>=target:reason='TARGET_GAP';raw=target
    elif now>=dt(position['deadline']):reason='TIME'
    elif position['vwap_exit'] and frame and frame.values['price']<frame.values['vwap']:reason='VWAP'
    elif lo<=stop:reason='STOP';raw=stop
    elif hi>=target:reason='TARGET';raw=target
    return (raw*(1-slip),reason) if reason else None


def build_tape(days, *, classify=None):
    """Only finalized bars enter signals; future minutes are inaccessible."""
    tape={};history=[]
    for day in days:
        engine=FeatureEngine(history);records={}
        length=len(day.minutes['SPY'])
        for index in range(15,length,5):
            now=day.session.open+timedelta(minutes=index);rows={s:day.five[s][:index//5] for s in ('SPY','QQQ')}
            frames={s:engine.snapshot(s,rows[s],day.session,now,rows) for s in rows}
            records[index]=frames
        tape[day.session.day]=records;history.append(day)
    return tape


def run(days,tape,strategy,costs=Costs(),*,run_id='research',code_commit='uncommitted',regime_classifier=None,record_rejections=False):
    trades=[];daily=[];rejections=[];used=set();cash=D(50);cumulative_net=D(0);peak=D(50);drawdown=D(0);diagnostics=Counter();examples={}
    def reject(stage,reason,signal=None):
        key=stage+':'+reason;diagnostics[key]+=1
        if key not in examples:examples[key]=dict(stage=stage,reason=reason,signal=signal.audit() if signal else None)
    for day in days:
        session=day.session;positions={};pending={};signals={};frames={};attempts=0;losses=D(0);start=cash;day_net=D(0)
        length=len(day.minutes['SPY']);last_regime={}
        for index in range(15,length):
            now=session.open+timedelta(minutes=index)
            if index in tape[session.day]:
                frames=tape[session.day][index]
                for sym,f in frames.items():
                    evaluation=strategy.evaluate(f,session);signals[sym]=evaluation.signal
                    diagnostics['signal_evaluations' if evaluation.signal else 'no_signal_evaluations']+=1
                    if not evaluation.signal:reject('STRATEGY',evaluation.reason)
                    last_regime[sym]=regime_classifier(f).labels if regime_classifier else ('UNCLASSIFIED',)
                    if record_rejections and evaluation.signal is None:
                        rejections.append(dict(strategy=strategy.id,symbol=sym,at=iso(now),stage='STRATEGY',reason=evaluation.reason,features=f.audit()))
            # All exits precede any new entry; SPY and QQQ have separate owners.
            for sym,pos in list(positions.items()):
                result=exit_decision(pos,day.minutes[sym][index],index,session,frames.get(sym),costs)
                total_mark=sum(max(D(p['cost']),D(p['quantity'])*D(quote(day.minutes[s][index]['open_price'],now,costs)['ask_price'])) for s,p in positions.items())
                if total_mark>D(30) or losses>=D('1.5'):
                    result=(D(quote(day.minutes[sym][index]['open_price'],now,costs)['bid_price'])*(1-D(str(costs.slippage_bps))/10000),'EXPOSURE_OR_DAILY_LOSS')
                if result:
                    price,reason=result;q=D(pos['quantity']);fee=D(str(costs.fee_per_side))
                    pnl=q*price-D(pos['cost'])-D(pos['entry_fee'])-fee
                    cash+=q*price-fee;day_net+=pnl;losses+=max(D(0),-pnl)
                    trades.append(dict(pos,exit_at=iso(now),exit=float(price),exit_reason=reason,pnl=float(pnl),
                        r_multiple=float(pnl/D(pos['planned_risk'])),hold_minutes=index-pos['entry_index'],
                        estimated_cost=float(D(pos['estimated_entry_cost'])+exit_cost(q,price,costs)),
                        estimated_slippage=float(slippage_cost(pos,q,price,costs)),
                        reconciliation='SIMULATED_FULL_OR_DECLARED_PARTIAL_FILL'))
                    del positions[sym]
            # Delayed entries reserve a symbol and capital from the initial attempt.
            for sym,item in list(pending.items()):
                if index<item['due']:continue
                sig=item['signal'];del pending[sym]
                if now>=dt(sig.valid_until) or not session.entry_window(now):reject('DELAYED_FILL','EXPIRED',sig);continue
                if any(float(b['low_price'])<=sig.stop for b in day.minutes[sym][item['reserved_index']:index]):
                    reject('DELAYED_FILL','STOP_TOUCHED_WHILE_PENDING',sig)
                    if record_rejections:rejections.append(dict(strategy=strategy.id,symbol=sym,at=iso(now),stage='DELAYED_FILL',reason='STOP_TOUCHED_WHILE_PENDING',setup_id=sig.setup_id))
                    continue
                mid=day.minutes[sym][index]['open_price']
                try:
                    exposure=sum(D(p['cost']) for p in positions.values())+sum(D(p['proposal']['notional']) for p in pending.values())
                    risk=sum(D(p['planned_risk']) for p in positions.values())+sum(D(p['proposal']['planned_risk']) for p in pending.values())
                    proposed=size_signal(sig,mid,now,costs,exposure=exposure,allocated_risk=risk,gross_losses=losses,net_to_date=cumulative_net+day_net,cash=cash-sum(D(p['proposal']['notional']) for p in pending.values()))
                    dollars=min(D(proposed['notional']),D(item['proposal']['notional']))
                    q=down(dollars/D(proposed['entry'])*D(str(costs.fill_fraction)))
                    if q<=0:reject('DELAYED_FILL','DECLARED_NO_FILL',sig);continue
                    entry=D(proposed['entry']);fee=D(str(costs.fee_per_side));cost=q*entry
                    risk=q*(entry-D(str(sig.stop))+entry*D(str(costs.slippage_bps))/10000)+2*max(D('.01'),fee)
                    if risk>D('.15') or risk>D(item['proposal']['planned_risk']):raise Unsafe('Fill exceeds reserved risk')
                    cash-=cost+fee
                    pos=dict(run_id=run_id,code_commit=code_commit,config_fingerprint=fingerprint(DEFAULTS),
                        feature_engine_version=frames[sym].version,strategy_id=strategy.id,strategy_version=strategy.version,strategy_fingerprint=strategy.fingerprint,
                        symbol=sym,direction='LONG',day=session.day,setup_id=sig.setup_id,signal_at=sig.signal_at,decision_at=iso(now),
                        regime=list(last_regime.get(sym,())),entry_reason=sig.reason,features=sig.features,
                        entry_at=iso(now),entry_index=index,proposed_entry=sig.entry,entry=float(entry),quantity=str(q),cost=str(cost),entry_fee=str(fee),
                        stop=str(sig.stop),target=str(entry+D(str(sig.target_r))*(entry-D(str(sig.stop)))),
                        planned_risk=str(risk),deadline=iso(now+timedelta(minutes=sig.max_hold_minutes)),vwap_exit=sig.vwap_exit,
                        estimated_entry_cost=str(q*(entry-D(str(mid)))+fee),fill_fraction=costs.fill_fraction,recovery_events=[])
                    positions[sym]=pos
                    # Bar extremes are used only after an entry at this bar's open.
                    same=exit_decision(pos,day.minutes[sym][index],index,session,frames.get(sym),costs)
                    if same:
                        price,reason=same;pnl=q*price-cost-2*fee;cash+=q*price-fee;day_net+=pnl;losses+=max(D(0),-pnl)
                        trades.append(dict(pos,exit_at=iso(now),exit=float(price),exit_reason=reason,pnl=float(pnl),r_multiple=float(pnl/risk),hold_minutes=0,
                            estimated_cost=float(D(pos['estimated_entry_cost'])+exit_cost(q,price,costs)),estimated_slippage=float(slippage_cost(pos,q,price,costs)),reconciliation='SIMULATED'))
                        del positions[sym]
                except Unsafe as exc:
                    reject('DELAYED_FILL',str(exc),sig)
                    if record_rejections:rejections.append(dict(strategy=strategy.id,symbol=sym,at=iso(now),stage='DELAYED_FILL',reason=str(exc),setup_id=sig.setup_id))
            for sym in sorted(signals):
                sig=signals[sym]
                if not sig or sym in positions or sym in pending or sig.setup_id in used or attempts>=5 or losses>=D('1.5') or not session.entry_window(now) or now>=dt(sig.valid_until):continue
                if len(positions)+len(pending)>=3:continue
                try:
                    exposure=sum(max(D(p['cost']),D(p['quantity'])*D(quote(day.minutes[s][index]['open_price'],now,costs)['ask_price'])) for s,p in positions.items())+sum(D(p['proposal']['notional']) for p in pending.values())
                    risk=sum(D(p['planned_risk']) for p in positions.values())+sum(D(p['proposal']['planned_risk']) for p in pending.values())
                    proposed=size_signal(sig,day.minutes[sym][index]['open_price'],now,costs,exposure=exposure,allocated_risk=risk,gross_losses=losses,net_to_date=cumulative_net+day_net,cash=cash-sum(D(p['proposal']['notional']) for p in pending.values()))
                    used.add(sig.setup_id);attempts+=1
                    # At least one next minute is required: no completed-bar-close fill.
                    pending[sym]=dict(signal=sig,proposal=proposed,due=index+1+costs.latency_minutes,reserved_index=index)
                except Unsafe as exc:
                    reject('RISK',str(exc),sig)
                    if record_rejections:rejections.append(dict(strategy=strategy.id,symbol=sym,at=iso(now),stage='RISK',reason=str(exc),setup_id=sig.setup_id))
            equity=cash+sum(D(p['quantity'])*D(quote(day.minutes[s][index]['close_price'],now,costs)['bid_price']) for s,p in positions.items())
            peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
            if cash<D(20):raise AssertionError('Buying power reserve violated')
        if positions:raise Unsafe('Unclosed simulated positions: data cannot justify overnight liquidation')
        cumulative_net+=day_net
        daily.append(dict(day=session.day,pnl=float(cash-start),gross_losses=float(losses),attempts=attempts))
    return dict(strategy=strategy.id,fingerprint=strategy.fingerprint,costs=asdict(costs),trades=trades,daily=daily,
                metrics=metrics(trades,daily,float(drawdown)),rejections=rejections,diagnostics=dict(diagnostics),rejection_examples=examples,
                limitations=['Synthetic spread/slippage and partial fills, no historical NBBO','At least one minute delayed fill after first observed eligible minute-open','Stop-first intrabar ambiguity','Reused data is not untouched final evidence'])


def metrics(trades,daily,drawdown=None):
    values=[t['pnl'] for t in trades];wins=[v for v in values if v>0];losses=[v for v in values if v<0]
    equity=peak=dd=0.;r_equity=r_peak=r_dd=0.;streak=longest=0
    for t in trades:
        equity+=t['pnl'];peak=max(peak,equity);dd=max(dd,peak-equity)
        r_equity+=t['r_multiple'];r_peak=max(r_peak,r_equity);r_dd=max(r_dd,r_peak-r_equity)
        streak=streak+1 if t['pnl']<0 else 0;longest=max(longest,streak)
    months=len({d['day'][:7] for d in daily});holds=[t['hold_minutes'] for t in trades]
    return dict(trades=len(trades),winners=len(wins),losers=len(losses),win_rate=len(wins)/len(values) if values else None,
        average_winner=statistics.mean(wins) if wins else None,average_loser=statistics.mean(losses) if losses else None,
        expectancy=statistics.mean(values) if values else None,profit_factor=sum(wins)/-sum(losses) if losses else None,
        pnl=sum(values),total_r=sum(t['r_multiple'] for t in trades),max_drawdown=max(dd,drawdown or 0),max_drawdown_r=r_dd,
        max_consecutive_losses=longest,sharpe=None,sharpe_note='Not asserted: small, dependent strategy sample; daily uncertainty intervals reported instead',
        average_hold=statistics.mean(holds) if holds else None,median_hold=statistics.median(holds) if holds else None,
        trades_per_month=len(trades)/months if months else None,estimated_transaction_costs=sum(t['estimated_cost'] for t in trades),
        estimated_slippage=sum(t.get('estimated_slippage',0) for t in trades),
        long_trades=len(trades),short_trades=0,short_support=False,sessions=len(daily),
        without_best_day=sum(values)-max((d['pnl'] for d in daily),default=0))
