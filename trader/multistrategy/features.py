"""Deterministic completed-bar features; no broker calls or fitted normalization."""
from dataclasses import dataclass
from datetime import timedelta
import math
from statistics import mean, pstdev

from ..calendar import Calendar
from ..models import dt, iso, Unsafe

VERSION = 'pit-features-1'


def completed(rows, session, now, interval=5):
    if now.tzinfo is None: raise Unsafe('Naive decision timestamp')
    stop=min(now,session.close)
    result=sorted((b for b in rows if session.open<=dt(b['begins_at'])
                   and dt(b['begins_at'])+timedelta(minutes=interval)<=stop),key=lambda b:dt(b['begins_at']))
    expected=max(0,int((stop-session.open).total_seconds()//(interval*60)))
    if len(result)!=expected:raise Unsafe('Missing completed bars')
    for i,b in enumerate(result):
        if dt(b['begins_at'])!=session.open+timedelta(minutes=interval*i):raise Unsafe('Duplicate, missing or misaligned bar')
        o,h,l,c,v=(float(b[k]) for k in ('open_price','high_price','low_price','close_price','volume'))
        if (not all(math.isfinite(x) for x in (o,h,l,c,v)) or not 0<l<=min(o,c)<=max(o,c)<=h or v<=0
                or b.get('interpolated') or b.get('session') not in (None,'','reg')):raise Unsafe('Invalid OHLCV')
    return result


def realized(closes):
    return math.sqrt(sum(math.log(b/a)**2 for a,b in zip(closes,closes[1:]))) if len(closes)>1 else None


@dataclass(frozen=True)
class Frame:
    symbol: str
    at: str
    session: str
    values: dict
    bars: tuple
    unavailable: tuple
    version: str = VERSION

    def audit(self):
        return dict(symbol=self.symbol,at=self.at,session=self.session,version=self.version,
                    values=dict(self.values),unavailable=list(self.unavailable))


class FeatureEngine:
    def __init__(self, history=()):
        self.history=tuple(history)
        self.calendar=Calendar()
        self._previous_days={}

    def previous_day(self, session):
        if session.day not in self._previous_days:
            previous=session.open-timedelta(days=1)
            while self.calendar.session(previous) is None:previous-=timedelta(days=1)
            self._previous_days[session.day]=self.calendar.session(previous).day
        return self._previous_days[session.day]

    def prior(self, symbol, session):
        days=sorted((d for d in self.history if d.session.close<=session.open),key=lambda d:d.session.day)
        if len({d.session.day for d in days})!=len(days):raise Unsafe('Duplicate historical session')
        return days[-21:]

    def snapshot(self,symbol,rows,session,now,peers=None,minute_rows=None):
        if symbol not in ('SPY','QQQ'):raise Unsafe('Unsupported feature symbol')
        bars=completed(rows,session,now)
        if not bars:raise Unsafe('No completed feature bars')
        o,h,l,c,v=([float(b[k]) for b in bars] for k in ('open_price','high_price','low_price','close_price','volume'))
        n=len(bars);price=c[-1];vol=sum(v);dollars=sum((a+b+z)/3*q for a,b,z,q in zip(h,l,c,v))
        vwaps=[];num=den=0
        for high,low,close,volume in zip(h,l,c,v):
            num+=(high+low+close)/3*volume;den+=volume;vwaps.append(num/den)
        tr=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) if i else h[i]-l[i] for i in range(n)]
        atr=mean(tr[-14:]) if n>=14 else None
        sd=pstdev(c[-20:]) if n>=20 else None
        rv=realized(c)
        signs=[1 if close>vw else -1 if close<vw else 0 for close,vw in zip(c,vwaps)]
        crosses=[i for i in range(1,n) if signs[i] and signs[i-1] and signs[i]!=signs[i-1]]
        span=h[-1]-l[-1];direction=1 if c[-1]>o[-1] else -1 if c[-1]<o[-1] else 0
        consecutive=0
        for op,close in zip(reversed(o),reversed(c)):
            if not direction or (1 if close>op else -1 if close<op else 0)!=direction:break
            consecutive+=direction
        f=dict(price=price,open=o[0],vwap=vwaps[-1],vwap_basis='OHLCV typical-price approximation',
            vwap_distance=price-vwaps[-1],vwap_distance_pct=price/vwaps[-1]-1,
            vwap_distance_atr=(price-vwaps[-1])/atr if atr else None,
            vwap_distance_z=(price-vwaps[-1])/sd if sd else None,
            vwap_slope_5=vwaps[-1]-vwaps[-2] if n>=2 else None,
            vwap_slope_15=vwaps[-1]-vwaps[-4] if n>=4 else None,
            vwap_slope_30=vwaps[-1]-vwaps[-7] if n>=7 else None,
            vwap_reclaim=n>=2 and signs[-2]<=0<signs[-1],vwap_loss=n>=2 and signs[-2]>=0>signs[-1],
            vwap_crosses=len(crosses),minutes_since_vwap_cross=(n-1-crosses[-1])*5 if crosses else None,
            volume=v[-1],cumulative_volume=vol,session_dollars=dollars,
            previous_bar_volume_ratio=v[-1]/v[-2] if n>=2 else None,
            volume_acceleration=v[-1]/v[-2]-v[-2]/v[-3] if n>=3 else None,
            momentum_5=c[-1]/c[-2]-1 if n>=2 else None,momentum_15=c[-1]/c[-4]-1 if n>=4 else None,
            momentum_1=None,momentum_3=None,session_return=price/o[0]-1,
            consecutive_directional_bars=consecutive,body_fraction=abs(c[-1]-o[-1])/span if span else None,
            close_location=(c[-1]-l[-1])/span if span else None,
            momentum_acceleration=(c[-1]/c[-2]-1)-(c[-2]/c[-3]-1) if n>=3 else None,
            atr_14=atr,realized_volatility=rv,opening_volatility=realized(c[:3]) if n>=3 else None,
            intraday_range=max(h)-min(l),range_expansion=(h[-1]-l[-1])/mean(tr[:-1]) if n>=2 and mean(tr[:-1]) else None,
            minutes_since_open=(now-session.open).total_seconds()/60,
            last_completed_at=iso(dt(bars[-1]['begins_at'])+timedelta(minutes=5)),bar_count=n)
        if minute_rows is not None:
            minute=completed(minute_rows,session,now,1);mc=[float(b['close_price']) for b in minute]
            for length in (1,3):f['momentum_'+str(length)]=mc[-1]/mc[-length-1]-1 if len(mc)>length else None
        for length in (5,15,30):
            count=length//5;available=n>=count;high=max(h[:count]) if available else None;low=min(l[:count]) if available else None
            width=high-low if available else None
            f.update({f'or{length}_high':high,f'or{length}_low':low,f'or{length}_width':width,
                f'or{length}_width_pct':width/price if available else None,
                f'or{length}_width_atr':width/atr if available and atr else None,
                f'or{length}_width_volatility':width/(price*rv) if available and rv else None,
                f'or{length}_distance':price-high if available else None,
                f'or{length}_breakout_strength':(price-high)/width if available and width else None})
        history=self.prior(symbol,session)
        comparable=[d.five[symbol][:n] for d in history[-20:] if len(d.five[symbol])>=n]
        f['rvol_history_sessions']=len(comparable)
        f['rvol_tod']=v[-1]/mean(float(b[-1]['volume']) for b in comparable) if len(comparable)>=5 else None
        f['cumulative_rvol_tod']=vol/mean(sum(float(x['volume']) for x in b) for b in comparable) if len(comparable)>=5 else None
        f['opening_rvol']=sum(v[:3])/mean(sum(float(x['volume']) for x in d.five[symbol][:3]) for d in history[-20:]) if n>=3 and len(history)>=5 else None
        f['breakout_volume_rvol']=f['rvol_tod'] if f['or15_high'] is not None and price>f['or15_high'] else None
        historical_vol=[realized([float(x['close_price']) for x in b]) for b in comparable]
        historical_vol=[x for x in historical_vol if x is not None]
        f['volatility_percentile']=sum(x<=rv for x in historical_vol)/len(historical_vol) if rv is not None and len(historical_vol)>=5 else None
        previous_day=self.previous_day(session)
        previous_close=float(history[-1].five[symbol][-1]['close_price']) if history and history[-1].session.day==previous_day else None
        daily_tr=[];daily_returns=[]
        for previous,current in zip(history,history[1:]):
            if previous.session.day!=self.previous_day(current.session):
                daily_tr=[];daily_returns=[]
                continue
            pc=float(previous.five[symbol][-1]['close_price']);bs=current.five[symbol]
            high=max(float(x['high_price']) for x in bs);low=min(float(x['low_price']) for x in bs)
            daily_tr.append(max(high-low,abs(high-pc),abs(low-pc)));daily_returns.append(math.log(float(bs[-1]['close_price'])/pc))
        daily_atr=mean(daily_tr[-14:]) if len(daily_tr)>=14 else None
        daily_vol=pstdev(daily_returns[-20:]) if len(daily_returns)>=20 else None
        gap=o[0]-previous_close if previous_close else None
        f.update(previous_close=previous_close,gap=gap,gap_pct=gap/previous_close if previous_close else None,
                 gap_direction=('UP' if gap>0 else 'DOWN' if gap<0 else 'FLAT') if gap is not None else None,
                 prior_daily_atr=daily_atr,prior_daily_realized_volatility=daily_vol,
                 gap_atr=gap/daily_atr if gap is not None and daily_atr else None,
                 gap_volatility=(gap/previous_close)/daily_vol if gap is not None and previous_close and daily_vol else None,
                 gap_fill_fraction=min(1,max(0,(o[0]-min(l))/gap if gap>0 else (max(h)-o[0])/-gap)) if gap else None,
                 gap_open=(min(l)>previous_close if gap>0 else max(h)<previous_close) if gap else False)
        other='QQQ' if symbol=='SPY' else 'SPY';peer=(peers or {}).get(other)
        f.update(both_above_vwap=None,both_below_vwap=None,both_above_or=None,both_below_or=None,
                 cross_relative_strength=None,cross_relative_momentum=None,cross_disagreement=None,simultaneous_breakout=None)
        if peer:
            try:
                pb=completed(peer,session,now)
                if len(pb)==n:
                    pc=[float(x['close_price']) for x in pb];pv=sum(float(x['volume']) for x in pb)
                    pvw=sum((float(x['high_price'])+float(x['low_price'])+float(x['close_price']))/3*float(x['volume']) for x in pb)/pv
                    pr=pc[-1]/float(pb[0]['open_price'])-1
                    f.update(both_above_vwap=price>f['vwap'] and pc[-1]>pvw,both_below_vwap=price<f['vwap'] and pc[-1]<pvw,
                        cross_relative_strength=f['session_return']-pr,cross_disagreement=(price>f['vwap'])!=(pc[-1]>pvw),
                        cross_relative_momentum=f['momentum_15']-(pc[-1]/pc[-4]-1) if n>=4 else None)
                    if n>=3:
                        poh=max(float(x['high_price']) for x in pb[:3]);pol=min(float(x['low_price']) for x in pb[:3])
                        f.update(both_above_or=price>f['or15_high'] and pc[-1]>poh,both_below_or=price<f['or15_low'] and pc[-1]<pol)
                        f['simultaneous_breakout']=n>=4 and f['both_above_or'] and c[-2]<=f['or15_high'] and pc[-2]<=poh
            except Unsafe:pass
        return Frame(symbol,iso(now),session.day,f,tuple(dict(b) for b in bars),tuple(sorted(k for k,v in f.items() if v is None)))
