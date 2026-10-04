"""Causal, cost-aware, long-only minute replay of frozen five-minute signals.

No MCP imports, broker credentials or order submission capability.
"""
from dataclasses import dataclass,asdict
from datetime import datetime,timedelta
import gzip,hashlib,json,math
from pathlib import Path
import statistics
import numpy as np
from .calendar import Calendar,Session
from .models import dt,NY,iso
from .research_signals import Bar,aggregate,candidates,STRATEGIES,COMPLEXITY


@dataclass
class Day:
    session:Session
    minutes:dict
    five:dict
    regime:str='UNKNOWN'


def load_data(directory,start,end):
    """Strict aligned session validation; missing data is never filled forward."""
    calendar=Calendar();group={};files=[];duplicates=[]
    for path in sorted(Path(directory).glob('*.json')):
        raw=path.read_bytes();payload=json.loads(raw);req=payload['request']
        if req['interval']!='minute' or req['bounds']!='regular' or req['adjustment_type']!='split':raise ValueError('Wrong research data contract')
        files.append(dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest(),request=req))
        for result in payload['data']['results']:
            if result['interval']!='minute':raise ValueError('Unexpected interval')
            symbol=result['symbol']
            if symbol not in ('SPY','QQQ'):raise ValueError('Unsupported research symbol')
            for b in result['bars'] or []:
                when=dt(b['begins_at']);day=when.astimezone(NY).date().isoformat()
                if not start<=day<end:raise ValueError('Out-of-protocol history')
                rows=group.setdefault(day,{}).setdefault(symbol,{})
                if when in rows:duplicates.append((day,symbol,iso(when)))
                rows[when]=b
    bad=[];days=[];expected_days=[];date=dt(start+'T16:00:00Z')
    while date<dt(end+'T00:00:00Z'):
        session=calendar.session(date);date+=timedelta(days=1)
        if not session:continue
        expected_days.append(session.day);length=int((session.close-session.open).total_seconds()/60)
        expected=[session.open+timedelta(minutes=i) for i in range(length)];norm={};errors=[]
        for symbol in ('SPY','QQQ'):
            rows=group.get(session.day,{}).get(symbol,{})
            if set(rows)!=set(expected):errors.append(f'{symbol}:missing={len(set(expected)-set(rows))},extra={len(set(rows)-set(expected))}');continue
            bars=[]
            for when in expected:
                b=rows[when];x=Bar(*(float(b[k]) for k in ('open_price','high_price','low_price','close_price','volume')))
                if (b.get('interpolated') or b.get('session') not in ('reg',None,'') or not all(math.isfinite(v) for v in asdict(x).values())
                    or x.v<=0 or not 0<x.l<=min(x.o,x.c)<=max(x.o,x.c)<=x.h):
                    errors.append(f'{symbol}:invalid/interpolated:{iso(when)}');break
                bars.append(x)
            if len(bars)==length:norm[symbol]=bars
        if errors:bad.append(dict(day=session.day,reasons=errors));continue
        days.append(Day(session,norm,{s:aggregate(b) for s,b in norm.items()}))
    if duplicates:raise ValueError(f'Duplicate source minutes: {duplicates[:3]}')
    # Regimes use previous complete SPY sessions only, never current day's close.
    past=[]
    for day in days:
        if len(past)>=21:
            returns=[past[i]/past[i-1]-1 for i in range(len(past)-20,len(past))]
            vol=statistics.pstdev(returns)*math.sqrt(252)
            day.regime=('UP' if past[-1]>statistics.mean(past[-20:]) else 'DOWN')+('_HIGH' if vol>.20 else '_LOW')
        past.append(day.minutes['SPY'][-1].c)
    manifest=dict(files=files,expected_sessions=len(expected_days),complete_sessions=len(days),coverage=len(days)/len(expected_days),excluded=bad,
        minute_bars=sum(len(x) for d in days for x in d.minutes.values()),first=days[0].session.day if days else None,last=days[-1].session.day if days else None)
    return days,manifest


@dataclass(frozen=True)
class Costs:
    spread_bps:float=1.
    slippage_bps:float=1.
    fee:float=0.
    latency_minutes:int=0


def position_size(signal,mid,cash,daily_losses,costs):
    """Mirrors conservative LIVE sizing; no access to outside account capital."""
    bid=mid*(1-costs.spread_bps/20000);ask=mid*(1+costs.spread_bps/20000)
    r=signal['trigger']-signal['stop']
    if costs.spread_bps>5 or r<=0 or not signal['trigger']<=ask<=signal['trigger']+.1*r:return None
    if signal['dollars']<10_000_000 or (ask-bid+2*ask*costs.slippage_bps/10000)>.1*r:return None
    price=ask*(1+costs.slippage_bps/10000)
    remaining=min(.10,.50-daily_losses)-.02
    available=min(10.,cash)-.01
    per_share=price-signal['stop']+price*costs.slippage_bps/10000
    if min(remaining,available,per_share)<=0:return None
    q=math.floor(min(9/price,available/price,remaining/per_share)*1e6)/1e6
    dollars=math.floor(q*price*100+1e-10)/100
    q=math.floor(dollars/price*1e6)/1e6
    if q*price<1:return None
    if signal.get('target') is not None and signal['target']-price<price-signal['stop']:return None
    planned=q*per_share+.02
    assert q*price<=9+1e-9 and q*price<=cash+1e-9 and planned<=.10+1e-9
    return q,price,planned


def exit_price(pos,bar,index,day,features,costs,latched=False):
    """Minute-open decisions; intraminute barriers use pessimistic ordering."""
    half=costs.spread_bps/20000;slip=costs.slippage_bps/10000
    bid=bar.o*(1-half);low=bar.l*(1-half);high=bar.h*(1-half)
    liquidation=len(day.minutes[pos['symbol']])-15
    timed=index>=pos['entered_index']+pos['hold'] and (not pos['legacy_time'] or not pos['reached_1r'])
    reason=None;raw=bar.o
    if pos['q']*bar.o*(1+half)>=10:reason='EXPOSURE'
    elif latched:reason='DAILY_LOSS'
    elif index>=liquidation:reason='SESSION_CLOSE'
    elif bid<=pos['stop']:reason='STOP_GAP'
    elif bid>=pos['target']:reason='TARGET_GAP';raw=pos['target']/(1-half)
    elif pos['strategy']!='mean_reversion' and features and features['closes'][-1]<features['vwap']:reason='VWAP'
    elif timed:reason='TIME'
    elif low<=pos['stop']:reason='STOP';raw=pos['stop']/(1-half)
    elif high>=pos['target']:reason='TARGET';raw=pos['target']/(1-half)
    elif pos['q']*bar.h*(1+half)>=10:reason='EXPOSURE';raw=10/pos['q']/(1+half)
    if reason:return raw*(1-half)*(1-slip),raw,reason
    return None


def build_tape(days):
    """Every record contains only the history available at that timestamp."""
    tape={}
    for day in days:
        records={}
        for n in range(3,len(day.five['SPY'])+1):
            now=day.session.open+timedelta(minutes=n*5)
            records[n*5]={}
            for symbol in ('SPY','QQQ'):
                other='QQQ' if symbol=='SPY' else 'SPY'
                records[n*5][symbol]=candidates(symbol,day.five[symbol][:n],day.five[other][:n],day.session,now)
        tape[day.session.day]=records
    return tape


def run(days,tape,strategy,costs=Costs()):
    if strategy not in STRATEGIES:raise ValueError('Unknown frozen strategy')
    cash=10.;trades=[];daily=[];peak=10.;drawdown=0.;min_cash=10.;max_entry=0.;max_risk=0.;skips=0
    for day in days:
        start_cash=cash;losses=0.;latched=False;pos=None;used=set();signals={};feats={};activated={};count=0
        length=len(day.minutes['SPY'])
        for index in range(15,length):
            if index in tape[day.session.day]:
                for symbol,(found,f) in tape[day.session.day][index].items():
                    signals[symbol]=found.get(strategy);feats[symbol]=f
                    if signals[symbol]:activated.setdefault(signals[symbol]['id'],index)
            now=day.session.open+timedelta(minutes=index)
            exited=False
            if pos:
                bar=day.minutes[pos['symbol']][index]
                # Realized+unrealized loss gate also stays in force.
                open_bid=bar.o*(1-costs.spread_bps/20000)
                if cash+pos['q']*open_bid-start_cash-.01<=-.50:latched=True
                exit_=exit_price(pos,bar,index,day,feats.get(pos['symbol']),costs,latched)
                if exit_:
                    price,raw,reason=exit_;pnl=pos['q']*(price-pos['entry'])-2*costs.fee
                    cash+=pos['q']*price-costs.fee;losses+=max(0,-pnl);latched=latched or losses>=.50
                    gross=pos['q']*(raw-pos['entry_mid'])
                    trades.append(dict(day=day.session.day,strategy=strategy,symbol=pos['symbol'],entry_at=pos['entered'],exit_at=iso(now),quantity=pos['q'],entry_price=pos['entry'],exit_price=price,position_value=pos['q']*pos['entry'],planned_risk=pos['planned'],net_pnl=pnl,gross_pnl=gross,estimated_cost=gross-pnl,reason=reason,regime=day.regime,hold_minutes=index-pos['entered_index']))
                    pos=None;exited=True;count+=1
                else:
                    if bar.h*(1-costs.spread_bps/20000)>=pos['entry']+(pos['entry']-pos['stop']):pos['reached_1r']=True
            if not pos and not exited and not latched and index<length-60:
                choices=[]
                for symbol,sig in signals.items():
                    if not sig or sig['id'] in used or index<activated[sig['id']]+costs.latency_minutes:continue
                    b=day.minutes[symbol][index];size=position_size(sig,b.o,cash,losses,costs)
                    if size:
                        q,price,planned=size;friction=(price*costs.spread_bps/10000+2*price*costs.slippage_bps/10000)/(sig['trigger']-sig['stop'])
                        choices.append((friction,-sig['dollars'],symbol,sig,q,price,planned,b.o))
                    else:skips+=1
                if choices:
                    _,_,symbol,sig,q,price,planned,mid=sorted(choices,key=lambda x:x[:3])[0]
                    used.add(sig['id']);cash-=q*price+costs.fee
                    pos=dict(sig,q=q,entry=price,entry_mid=mid,entered=iso(now),entered_index=index,planned=planned,reached_1r=False,
                        target=sig['target'] if sig['target'] is not None else price+2*(price-sig['stop']))
                    max_entry=max(max_entry,q*price);max_risk=max(max_risk,planned)
                    # Entry is at minute OPEN, so both barriers in this minute
                    # occur after entry; stop first if their order is unknown.
                    b=day.minutes[symbol][index];same=exit_price(pos,b,index,day,feats.get(symbol),costs)
                    if same:
                        price_out,raw,reason=same;pnl=q*(price_out-price)-2*costs.fee
                        cash+=q*price_out-costs.fee;losses+=max(0,-pnl);latched=latched or losses>=.50;count+=1
                        gross=q*(raw-mid)
                        trades.append(dict(day=day.session.day,strategy=strategy,symbol=symbol,entry_at=iso(now),exit_at=iso(now),quantity=q,entry_price=price,exit_price=price_out,position_value=q*price,planned_risk=planned,net_pnl=pnl,gross_pnl=gross,estimated_cost=gross-pnl,reason=reason,regime=day.regime,hold_minutes=0))
                        pos=None
                    elif b.h*(1-costs.spread_bps/20000)>=pos['entry']+(pos['entry']-pos['stop']):
                        pos['reached_1r']=True
            equity=cash+(pos['q']*day.minutes[pos['symbol']][index].c*(1-costs.spread_bps/20000)-costs.fee if pos else 0)
            peak=max(peak,equity);drawdown=max(drawdown,peak-equity);min_cash=min(min_cash,cash)
            assert cash>=-1e-8
        assert pos is None,'Replay cannot intentionally carry overnight'
        daily.append(dict(day=day.session.day,pnl=cash-start_cash,trades=count,gross_losses=losses,latched=latched,equity=cash,regime=day.regime))
    return dict(strategy=strategy,costs=asdict(costs),trades=trades,daily=daily,metrics=metrics(trades,daily,drawdown),checks=dict(max_entry=max_entry,max_planned_risk=max_risk,min_cash=min_cash,rejected_quote_observations=skips))


def metrics(trades,daily,drawdown=None):
    values=[x['net_pnl'] for x in trades];wins=[x for x in values if x>0];losses=[x for x in values if x<0]
    profit=sum(wins);loss=-sum(losses);n=len(values)
    if drawdown is None:
        equity=peak=10.;drawdown=0.
        for x in values:equity+=x;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    interval=None
    if n and daily:
        rng=np.random.default_rng(20260924);p=np.array([d['pnl'] for d in daily]);counts=np.array([d['trades'] for d in daily])
        sampled=rng.integers(0,len(daily),size=(2000,len(daily)));den=counts[sampled].sum(axis=1);numer=p[sampled].sum(axis=1)
        valid=den>0
        interval=[float(x) for x in np.quantile(numer[valid]/den[valid],[.025,.975])] if valid.any() else None
    return dict(trade_count=n,win_rate=len(wins)/n if n else None,average_win=statistics.mean(wins) if wins else None,
        average_loss=statistics.mean(losses) if losses else None,expectancy=sum(values)/n if n else None,
        profit_factor=profit/loss if loss else None,profit_factor_note='no losing trades' if n and not loss else None,
        max_drawdown=drawdown,net_pnl=sum(values),gross_pnl=sum(t['gross_pnl'] for t in trades),estimated_costs=sum(t['estimated_cost'] for t in trades),
        expectancy_day_bootstrap_95=interval,net_without_best_day=sum(values)-max([d['pnl'] for d in daily],default=0),
        sessions=len(daily),days_with_trades=sum(d['trades']>0 for d in daily))


def regime_metrics(result):
    return {r:metrics([x for x in result['trades'] if x['regime']==r],[x for x in result['daily'] if x['regime']==r])
        for r in sorted({x['regime'] for x in result['daily']})}


def qualify(base,stress,minimum):
    m=base['metrics'];reasons=[];ci=m['expectancy_day_bootstrap_95'];groups=regime_metrics(base)
    if m['trade_count']<minimum:reasons.append('too_few_trades')
    if m['profit_factor'] is None or m['profit_factor']<1.10:reasons.append('profit_factor_below_1.10_or_undefined')
    if m['expectancy'] is None or m['expectancy']<=0:reasons.append('nonpositive_expectancy')
    if not ci or ci[0]<=0:reasons.append('confidence_interval_includes_zero')
    if stress['metrics']['expectancy'] is None or stress['metrics']['expectancy']<=0:reasons.append('stress_expectancy_not_positive')
    if m['max_drawdown']>2:reasons.append('drawdown_over_2_dollars')
    if m['net_without_best_day']<=0:reasons.append('depends_on_best_day')
    if sum(g['trade_count']>=10 and g['net_pnl']>0 for r,g in groups.items() if r!='UNKNOWN')<2:reasons.append('insufficient_positive_regimes')
    return reasons
