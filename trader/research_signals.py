"""Frozen research hypotheses. Pure completed-bar inputs; no broker access."""
from dataclasses import dataclass
from math import sqrt
from statistics import mean,pstdev
from .models import iso
from .strategy import evaluate as existing_evaluate

STRATEGIES=('orb','orb_pullback','existing_orb_pullback_vwap','vwap_continuation',
 'vwap_reclaim','vwap_support_rejection','momentum_volume','mean_reversion',
 'relative_strength_orb','relative_strength_momentum')
COMPLEXITY=dict(zip(STRATEGIES,(1,2,4,4,3,3,4,6,7,7)))


@dataclass(frozen=True)
class Bar:
    o:float
    h:float
    l:float
    c:float
    v:float


def aggregate(minutes):
    if len(minutes)%5:raise ValueError('Only complete five-minute groups')
    return [Bar(x[0].o,max(b.h for b in x),min(b.l for b in x),x[-1].c,sum(b.v for b in x))
            for i in range(0,len(minutes),5) for x in [minutes[i:i+5]]]


def features(bars):
    dollars=volume=0.;vwaps=[]
    for b in bars:
        dollars+=(b.h+b.l+b.c)/3*b.v;volume+=b.v;vwaps.append(dollars/volume)
    closes=[b.c for b in bars]
    tr=[max(b.h-b.l,abs(b.h-bars[i-1].c),abs(b.l-bars[i-1].c)) if i else b.h-b.l for i,b in enumerate(bars)]
    return dict(vwap=vwaps[-1],vwaps=vwaps,dollars=dollars,closes=closes,
                atr=mean(tr[-14:]),slope=vwaps[-1]-vwaps[-4] if len(bars)>=4 else 0)


def pullback_without_vwap(bars):
    oh=max(b.h for b in bars[:3]);state='BREAK';armed=None
    for i,b in enumerate(bars[3:],3):
        if state in ('INVALID','MISSED'):state='BREAK';armed=None
        if state=='ARMED':
            if b.l<=armed['stop']:state='INVALID'
            elif b.h>=armed['trigger']:state='MISSED'
            continue
        if state=='PULLBACK':
            if b.c<oh:state='BREAK'
            elif b.l<=oh and b.c>=oh:
                state='ARMED';armed=dict(trigger=b.h+.01,stop=b.l-.01,index=i)
        elif b.c>oh:state='PULLBACK'
    return armed if state=='ARMED' else None


def candidates(symbol,bars,other_bars,session,now):
    """Caller supplies only bars whose right edge <= now (checked here too)."""
    n=len(bars)
    if n*300>(now-session.open).total_seconds():raise ValueError('Future/incomplete signal bar')
    if n<3:return {},None
    f=features(bars);b=bars[-1];out={};oh=max(x.h for x in bars[:3]);ol=min(x.l for x in bars[:3])
    def add(name,trigger=None,stop=None,index=None,hold=30,target=None,legacy=False):
        t=trigger if trigger is not None else b.h+.01;s=stop if stop is not None else b.l-.01
        if t<=s:return
        out[name]=dict(symbol=symbol,strategy=name,trigger=t,stop=s,hold=hold,
            target=target,legacy_time=legacy,id=f'{session.day}:{symbol}:{name}:{index if index is not None else n-1}',
            vwap=f['vwap'],dollars=f['dollars'],formed_at=iso(now))
    if not any(x.h>=oh+.01 or x.l<=ol-.01 for x in bars[3:]):
        add('orb',oh+.01,ol-.01,index=2)
    p=pullback_without_vwap(bars)
    if p:add('orb_pullback',**p)
    # Invoke the existing production state machine without rewriting its rules.
    from datetime import timedelta
    raw=[dict(begins_at=iso(session.open+timedelta(minutes=5*i)),open_price=str(x.o),high_price=str(x.h),low_price=str(x.l),close_price=str(x.c),volume=x.v,session='reg',interpolated=False) for i,x in enumerate(bars)]
    old=existing_evaluate(symbol,raw,session,now)
    if old['state']=='ARMED':
        add('existing_orb_pullback_vwap',float(old['trigger']),float(old['invalidation']),index=old['setup_id'],legacy=True)
    if n>=14 and all(bars[i].c>f['vwaps'][i] for i in range(n-3,n)) and f['slope']>0 and b.l<=f['vwap']<b.c:
        add('vwap_continuation')
    if n>=3 and all(bars[i].c<f['vwaps'][i] for i in (n-3,n-2)) and b.c>f['vwap'] and b.c>bars[-2].h:
        add('vwap_reclaim',stop=min(b.l,bars[-2].l)-.01,hold=20)
    if n>=4 and bars[-2].c>f['vwaps'][-2] and b.l<=f['vwap']<b.c and b.c>b.o and f['slope']>=0:
        add('vwap_support_rejection')
    if n>=7 and b.c>max(x.h for x in bars[-7:-1]) and b.v>=1.5*mean(x.v for x in bars[-7:-1]) and b.c>f['vwap'] and f['slope']>0:
        add('momentum_volume',stop=min(x.l for x in bars[-3:])-.01)
    if n>=20:
        change=sum(abs(bars[i].c-bars[i-1].c) for i in range(n-6,n))
        efficiency=abs(b.c-bars[-7].c)/change if change else 0
        if (b.c<f['vwap']-2*pstdev(f['closes'][-20:]) and b.c>b.o and efficiency<=.25
                and abs(f['slope'])<=.25*f['atr']):
            add('mean_reversion',hold=15,target=f['vwap'])
    if len(other_bars)==n:
        other=features(other_bars);r=b.c/bars[0].o-1;other_r=other_bars[-1].c/other_bars[0].o-1
        strong=r>0 and other_r>0 and r-other_r>=.0005 and b.c>f['vwap'] and other_bars[-1].c>other['vwap']
        if strong:
            for source,dest in [('existing_orb_pullback_vwap','relative_strength_orb'),('momentum_volume','relative_strength_momentum')]:
                if source in out:
                    x=out[source];add(dest,x['trigger'],x['stop'],index=x['id'],legacy=False)
    return out,f
