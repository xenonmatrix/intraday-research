"""Predeclared, explainable labels; UNKNOWN is not favorable evidence."""
from dataclasses import dataclass

VERSION='rule-regimes-1'


@dataclass(frozen=True)
class Regime:
    labels: tuple
    reasons: tuple
    version: str=VERSION


def classify(frame):
    f=frame.values;labels=[];reasons=[]
    slope=f.get('vwap_slope_15');momentum=f.get('momentum_15');atr=f.get('atr_14')
    if slope is not None and momentum is not None:
        if f['price']>f['vwap'] and slope>0 and momentum>0:
            labels.append('TRENDING_UP');reasons.append('price>VWAP, 15-minute VWAP slope>0, 15-minute return>0')
        elif f['price']<f['vwap'] and slope<0 and momentum<0:
            labels.append('TRENDING_DOWN');reasons.append('price<VWAP, slope<0, return<0')
        elif atr and abs(slope)<=.25*atr and f.get('vwap_crosses',0)>=2:
            labels.append('RANGE_BOUND');reasons.append('absolute VWAP slope <= 0.25 ATR and at least two VWAP crosses')
    percentile=f.get('volatility_percentile')
    if percentile is not None and percentile>=.8:labels.append('HIGH_VOLATILITY');reasons.append('elapsed-window realized volatility >= prior-session 80th percentile')
    elif percentile is not None and percentile<=.2:labels.append('LOW_VOLATILITY');reasons.append('elapsed-window realized volatility <= prior-session 20th percentile')
    if f['minutes_since_open']<=30 and f.get('momentum_5') is not None and f['momentum_5']>0 and f['price']>f['vwap']:
        labels.append('OPENING_MOMENTUM');reasons.append('first 30 minutes, positive completed 5-minute return, price above VWAP')
    if f.get('gap_atr') is not None and abs(f['gap_atr'])>=.1:
        if f['gap_open'] and ((f['gap_atr']>0 and f['price']>f['open']) or (f['gap_atr']<0 and f['price']<f['open'])):
            labels.append('GAP_CONTINUATION');reasons.append('gap >= 0.1 prior ATR, still open and extending')
        elif f.get('gap_fill_fraction') is not None and f['gap_fill_fraction']>=.5:
            labels.append('GAP_FAILURE');reasons.append('at least half of material opening gap filled')
    if not set(labels)&{'TRENDING_UP','TRENDING_DOWN','RANGE_BOUND','OPENING_MOMENTUM','GAP_CONTINUATION','GAP_FAILURE'}:
        labels.append('UNCERTAIN');reasons.append('direction/range/gap evidence insufficient or conflicting')
    return Regime(tuple(labels),tuple(reasons))


ELIGIBLE={
    'orb':{'OPENING_MOMENTUM','TRENDING_UP'},
    'filtered_orb':{'OPENING_MOMENTUM','TRENDING_UP'},
    'orb_retest':{'TRENDING_UP','OPENING_MOMENTUM'},
    'vwap_trend':{'TRENDING_UP'},
    'vwap_reversion':{'RANGE_BOUND'},
    'gap_continuation':{'GAP_CONTINUATION','TRENDING_UP'},
    'gap_fade':{'GAP_FAILURE','TRENDING_UP'},
}


def eligible(strategy,regime):
    return 'UNCERTAIN' not in regime.labels and bool(ELIGIBLE.get(strategy,set())&set(regime.labels))
