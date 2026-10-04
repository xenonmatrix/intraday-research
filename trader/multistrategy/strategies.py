"""Independent, long-only hypotheses. Strategy functions cannot submit orders."""
from dataclasses import dataclass,asdict,replace
from datetime import timedelta
import hashlib,json

from ..models import Config,D,dt,iso,Unsafe
from ..orb import evaluate as production_orb
from .features import Frame,VERSION as FEATURE_VERSION


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Parameters:
    target_r: float = 2
    hold_minutes: int = 30
    offset: float = .01
    rvol: float = 1.0
    retest_width: float = .1
    timeout_bars: int = 6
    reversion_z: float = 2
    gap_atr: float = .1

    def __post_init__(self):
        import math
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in asdict(self).values()):raise Unsafe('Invalid strategy parameter')
        if type(self.hold_minutes) is not int or type(self.timeout_bars) is not int:raise Unsafe('Integer strategy duration required')


@dataclass(frozen=True)
class CandidateSignal:
    strategy_id: str
    strategy_version: str
    fingerprint: str
    symbol: str
    direction: str
    signal_at: str
    setup_id: str
    entry: float
    stop: float
    target: float
    max_hold_minutes: int
    target_r: float
    vwap_exit: bool
    features: dict
    reason: str
    valid_until: str
    regime: tuple = ()
    entry_model: str = 'market_after_trigger_with_production_band_and_friction_checks'
    invalidation_rules: tuple = ('stop_touched_before_entry','trigger_already_crossed_in_completed_bar','expiry','session_cutoff')

    def audit(self):return asdict(self)

    def candidate(self):
        return dict(symbol=self.symbol,state='ARMED',strategy=self.strategy_id,setup_id=self.setup_id,
                    trigger=str(self.entry),invalidation=str(self.stop),target=str(self.target),
                    session_dollars=str(self.features['values']['session_dollars']),
                    vwap=str(self.features['values']['vwap']),last_bar=iso(dt(self.features['values']['last_completed_at'])-timedelta(minutes=5)),
                    strategy_id=self.strategy_id,strategy_version=self.strategy_version,
                    strategy_fingerprint=self.fingerprint,signal=self.audit())


@dataclass(frozen=True)
class Evaluation:
    strategy: str
    signal: object
    reason: str


class Strategy:
    id='abstract'
    version='1.0.0'
    def __init__(self,parameters=None):self.parameters=parameters or Parameters()
    @property
    def fingerprint(self):return fingerprint(dict(id=self.id,version=self.version,parameters=asdict(self.parameters),feature_version=FEATURE_VERSION))

    def signal(self,frame,session,entry,stop,reason,anchor=None,vwap_exit=True):
        p=self.parameters;now=dt(frame.at)
        if not session.entry_window(now):return Evaluation(self.id,None,'SESSION_WINDOW')
        if entry<=stop or stop<=0:return Evaluation(self.id,None,'INVALID_STOP')
        anchor=anchor or frame.values['last_completed_at']
        key=f'{session.day}:{frame.symbol}:{self.id}:{self.fingerprint}:{anchor}'
        end=min(now+timedelta(minutes=5),session.close-timedelta(minutes=session.entry_cutoff_minutes))
        c=CandidateSignal(self.id,self.version,self.fingerprint,frame.symbol,'LONG',frame.at,key,
            entry,stop,entry+p.target_r*(entry-stop),p.hold_minutes,p.target_r,vwap_exit,frame.audit(),reason,iso(end))
        return Evaluation(self.id,c,reason)

    def reject(self,reason):return Evaluation(self.id,None,reason)
    def evaluate(self,frame,session):raise NotImplementedError


class ORB(Strategy):
    id='orb'
    def evaluate(self,frame,session):
        p=self.parameters
        cfg=replace(Config(),breakout_offset=D(str(p.offset)),stop_offset=D(str(p.offset)),target_r=D(str(p.target_r)),max_hold_minutes=p.hold_minutes)
        c=production_orb(frame.symbol,frame.bars,session,dt(frame.at),cfg)
        if c['state']!='ARMED':return self.reject('PRODUCTION_ORB_'+c['state'])
        return self.signal(frame,session,float(c['trigger']),float(c['invalidation']),'Unconsumed production 15-minute range',anchor='opening-range')


class FilteredORB(ORB):
    id='filtered_orb'
    def __init__(self,parameters=None,omit_filter=None):
        super().__init__(parameters)
        if omit_filter not in (None,'vwap','slope','volume','momentum','gap','volatility','cross_asset'):raise Unsafe('Unknown ablation')
        self.omit_filter=omit_filter
    @property
    def fingerprint(self):return fingerprint(dict(base=super().fingerprint,omit_filter=self.omit_filter))
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters
        checks=dict(vwap=f['price']>f['vwap'],slope=f['vwap_slope_5'] is not None and f['vwap_slope_5']>0,
                volume=f['rvol_tod'] is not None and f['rvol_tod']>=p.rvol,
                momentum=f['momentum_5'] is not None and f['momentum_5']>0,
                gap=f['gap_pct'] is not None and f['gap_pct']>=0,
                volatility=f['volatility_percentile'] is not None and .1<=f['volatility_percentile']<=.9,
                cross_asset=f['both_above_vwap'] is True)
        if not all(value for name,value in checks.items() if name!=self.omit_filter):return self.reject('FILTERS_OR_REQUIRED_HISTORY_UNAVAILABLE')
        return super().evaluate(frame,session)


class ORBRetest(Strategy):
    id='orb_retest'
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters;bars=frame.bars
        if f['or15_high'] is None:return self.reject('OPENING_RANGE_INCOMPLETE')
        high=f['or15_high'];low=f['or15_low'];width=high-low
        breakout=next((i for i in range(3,len(bars)) if float(bars[i]['close_price'])>high+p.offset),None)
        if breakout is None:return self.reject('WAIT_BREAKOUT')
        if any(float(b['low_price'])<=low-p.offset for b in bars[3:]):return self.reject('INVALIDATED')
        anchor=None
        for i in range(breakout+1,min(len(bars),breakout+p.timeout_bars+1)):
            b=bars[i]
            if high-width*p.retest_width<=float(b['low_price'])<=high+width*p.retest_width and float(b['close_price'])>max(high,float(b['open_price'])):
                anchor=i;break
        if anchor is None:return self.reject('RETEST_TIMEOUT' if len(bars)-1>=breakout+p.timeout_bars else 'WAIT_RETEST')
        b=bars[anchor];entry=float(b['high_price'])+p.offset;stop=float(b['low_price'])-p.offset
        if len(bars)-1>anchor+2:return self.reject('CONFIRMATION_TIMEOUT')
        if any(float(x['high_price'])>=entry or float(x['low_price'])<=stop for x in bars[anchor+1:]):return self.reject('CONSUMED_OR_INVALIDATED')
        return self.signal(frame,session,entry,stop,'First causal OR breakout, retest and positive close',anchor=b['begins_at'])


class VWAPTrend(Strategy):
    id='vwap_trend'
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters;b=frame.bars[-1]
        if not (f['atr_14'] and f['vwap_slope_15'] is not None and f['vwap_slope_15']>0 and
                f['momentum_15'] is not None and f['momentum_15']>0 and f['rvol_tod'] is not None and f['rvol_tod']>=p.rvol):
            return self.reject('TREND_OR_VOLUME_UNCONFIRMED')
        if not float(b['low_price'])<=f['vwap']+p.retest_width*f['atr_14']<float(b['close_price']) or float(b['close_price'])<=float(b['open_price']):return self.reject('WAIT_VWAP_PULLBACK_CONFIRMATION')
        return self.signal(frame,session,float(b['high_price'])+p.offset,float(b['low_price'])-p.offset,'Positive VWAP slope and momentum; pullback holds')


class VWAPReversion(Strategy):
    id='vwap_reversion'
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters;b=frame.bars[-1]
        if f['vwap_distance_z'] is None or f['vwap_distance_z']>-p.reversion_z:return self.reject('NO_EXTREME_DEVIATION')
        if not (f['atr_14'] and abs(f['vwap_slope_15'])<=.25*f['atr_14'] and
                f['previous_bar_volume_ratio']<1 and f['momentum_acceleration']>0 and float(b['close_price'])>float(b['open_price'])):
            return self.reject('EXHAUSTION_UNCONFIRMED')
        entry=float(b['high_price'])+p.offset;stop=float(b['low_price'])-p.offset
        if f['vwap']<entry+p.target_r*(entry-stop):return self.reject('INSUFFICIENT_REVERSION_REWARD')
        return self.signal(frame,session,entry,stop,'Negative VWAP deviation with reversal and declining volume',vwap_exit=False)


class GapContinuation(Strategy):
    id='gap_continuation'
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters;b=frame.bars[-1]
        if not (f['gap_atr'] is not None and f['gap_atr']>=p.gap_atr and f['gap_open'] and f['minutes_since_open']<=90):return self.reject('NO_ELIGIBLE_GAP')
        if not (f['price']>f['vwap'] and f['momentum_15'] is not None and f['momentum_15']>0 and
                f['rvol_tod'] is not None and f['rvol_tod']>=p.rvol and f['both_above_vwap'] is True):return self.reject('GAP_CONTINUATION_UNCONFIRMED')
        if f['price']<=f['or15_high']:return self.reject('WAIT_RANGE_BREAKOUT')
        return self.signal(frame,session,float(b['high_price'])+p.offset,float(b['low_price'])-p.offset,'Up gap remains open with OR/VWAP/volume confirmation')


class GapFade(Strategy):
    id='gap_fade'
    def evaluate(self,frame,session):
        f=frame.values;p=self.parameters;b=frame.bars[-1]
        if not (f['gap_atr'] is not None and f['gap_atr']<=-p.gap_atr and f['gap_open'] and f['minutes_since_open']<=120):return self.reject('NO_DOWN_GAP_TO_FADE_LONG')
        if not (f['vwap_reclaim'] and f['momentum_5'] is not None and f['momentum_5']>0 and f['rvol_tod'] is not None and f['rvol_tod']>=p.rvol):return self.reject('GAP_RECLAIM_UNCONFIRMED')
        entry=float(b['high_price'])+p.offset;stop=float(b['low_price'])-p.offset
        if f['previous_close']<entry+p.target_r*(entry-stop):return self.reject('GAP_FILL_REWARD_TOO_SMALL')
        return self.signal(frame,session,entry,stop,'Down-gap VWAP reclaim toward previous close',vwap_exit=False)


STRATEGIES={c.id:c for c in (ORB,FilteredORB,ORBRetest,VWAPTrend,VWAPReversion,GapContinuation,GapFade)}


def strategies(parameters=None):return [cls(parameters) for cls in STRATEGIES.values()]
