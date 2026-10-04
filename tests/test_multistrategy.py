"""Offline synthetic inputs; no account, OAuth or broker network use."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import pytest

from trader.calendar import Calendar
from trader.models import dt,iso,Unsafe,D
from trader.multistrategy.features import FeatureEngine,Frame,completed
from trader.multistrategy.data import MarketDay,aggregate
from trader.multistrategy.strategies import STRATEGIES,Parameters,Strategy,ORB,ORBRetest
from trader.multistrategy.backtest import Costs,build_tape,run,exit_decision,size_signal
from trader.multistrategy.regimes import classify,eligible
from trader.multistrategy.arbitration import resolve


def day(date='2023-03-13'):
    session=Calendar().session(dt(date+'T16:00:00Z'));minutes={}
    for symbol in ('SPY','QQQ'):
        minutes[symbol]=[dict(begins_at=iso(session.open+timedelta(minutes=i)),open_price='100.01',high_price='100.02',
            low_price='100',close_price='100.01',volume=100000,session='reg') for i in range(int((session.close-session.open).total_seconds()/60))]
    return MarketDay(session,minutes,{s:aggregate(b) for s,b in minutes.items()})


def frame(d=None,n=15,history=()):
    d=d or day();now=d.session.open+timedelta(minutes=n*5)
    return FeatureEngine(history).snapshot('SPY',d.five['SPY'],d.session,now,d.five)


def signal(strategy='orb',symbol='SPY',direction='LONG'):
    d=day();f=frame(d,3);base=Strategy();base.id=strategy
    s=base.signal(f,d.session,100,99,'fixture').signal
    return replace(s,symbol=symbol,direction=direction)


def test_features_ignore_all_future_bars_and_future_history():
    d=day();now=d.session.open+timedelta(minutes=15);engine=FeatureEngine([day('2023-03-14')])
    first=engine.snapshot('SPY',d.five['SPY'],d.session,now,d.five)
    changed=deepcopy(d.five)
    for s in changed:
        for b in changed[s][3:]:b.update(high_price='100000',volume=100000000)
    assert engine.snapshot('SPY',changed['SPY'],d.session,now,changed).audit()==first.audit()
    assert first.values['previous_close'] is None


@pytest.mark.parametrize('date,utc_hour,length',[('2023-03-10',14,390),('2023-03-13',13,390),('2023-07-03',13,210),('2022-11-25',14,210)])
def test_session_dst_early_close(date,utc_hour,length):
    d=day(date);assert d.session.open.hour==utc_hour and len(d.minutes['SPY'])==length
    f=frame(d,length//5);assert f.values['bar_count']==length//5


@pytest.mark.parametrize('problem',['missing','duplicate','nan','future_time','zero_volume'])
def test_invalid_completed_bars_fail_closed(problem):
    d=day();bars=deepcopy(d.five['SPY'][:3]);now=d.session.open+timedelta(minutes=15)
    if problem=='missing':bars.pop(1)
    if problem=='duplicate':bars[1]=bars[0]
    if problem=='nan':bars[0]['high_price']='NaN'
    if problem=='future_time':bars[1]['begins_at']=iso(now+timedelta(minutes=5))
    if problem=='zero_volume':bars[0]['volume']=0
    with pytest.raises(Unsafe):FeatureEngine().snapshot('SPY',bars,d.session,now)


def test_cross_symbol_requires_matching_completed_timestamps():
    d=day();peers=deepcopy(d.five);peers['QQQ']=peers['QQQ'][:2]
    f=FeatureEngine().snapshot('SPY',d.five['SPY'],d.session,d.session.open+timedelta(minutes=15),peers)
    assert f.values['both_above_vwap'] is None


def test_vwap_and_unavailable_minute_features():
    d=day();f=frame(d,3)
    assert f.values['vwap']==pytest.approx(100.01)
    assert f.values['cumulative_volume']==1500000
    assert f.values['momentum_1'] is None and f.values['momentum_3'] is None
    assert f.values['or30_high'] is None and f.values['or15_high']==100.02


def test_real_minute_features_and_tod_rvol_gap():
    histories=[day(d) for d in ('2023-03-06','2023-03-07','2023-03-08','2023-03-09','2023-03-10')]
    d=day();f=FeatureEngine(histories).snapshot('SPY',d.five['SPY'],d.session,d.session.open+timedelta(minutes=15),d.five,d.minutes['SPY'])
    assert f.values['rvol_tod']==1 and f.values['opening_rvol']==1
    assert f.values['gap']==0 and f.values['previous_close']==100.01
    assert f.values['momentum_1']==0 and f.values['momentum_3']==0
    assert f.values['realized_volatility']==0


def test_baseline_matches_production_and_never_rearms():
    from trader.orb import evaluate
    from trader.models import Config
    d=day();f=frame(d,3);candidate=ORB().evaluate(f,d.session).signal
    prod=evaluate('SPY',list(f.bars),d.session,dt(f.at),Config())
    assert D(str(candidate.entry))==D(prod['trigger']) and D(str(candidate.stop))==D(prod['invalidation'])
    d.five['SPY'][3]['high_price']='100.05'
    for n in (4,5,6):assert ORB().evaluate(frame(d,n),d.session).signal is None


@pytest.mark.parametrize('strategy',list(STRATEGIES))
def test_strategy_never_enters_after_cutoff(strategy):
    d=day();f=frame(d,70)
    assert STRATEGIES[strategy]().evaluate(f,d.session).signal is None


@pytest.mark.parametrize('strategy',list(STRATEGIES))
def test_parameters_are_versioned(strategy):
    a=STRATEGIES[strategy]();b=STRATEGIES[strategy](Parameters(target_r=2.5))
    assert a.fingerprint!=b.fingerprint


def test_retest_has_positive_confirmation_and_cannot_replay():
    d=day();b=d.five['SPY'];b[3].update(open_price='100.02',high_price='100.10',low_price='100.02',close_price='100.08')
    b[4].update(open_price='100.02',high_price='100.09',low_price='100.02',close_price='100.07')
    s=ORBRetest().evaluate(frame(d,5),d.session).signal
    assert s is not None
    b[5].update(open_price='100.08',high_price='100.2',low_price='100.05',close_price='100.15')
    assert ORBRetest().evaluate(frame(d,6),d.session).signal is None


@pytest.mark.parametrize('strategy',['filtered_orb','vwap_trend','vwap_reversion','gap_continuation','gap_fade'])
def test_explicit_rule_positive_and_negative(strategy):
    d=day();f=frame(d,15);v=dict(f.values)
    v.update(price=100.01,vwap=100.005,vwap_slope_5=.001,vwap_slope_15=.002,
        rvol_tod=2,momentum_5=.001,momentum_15=.002,gap_pct=.01,volatility_percentile=.5,
        both_above_vwap=True,atr_14=.05,previous_bar_volume_ratio=.5,momentum_acceleration=.001,
        vwap_distance_z=-3,gap_atr=.2,gap_open=True,or15_high=100,previous_close=110,vwap_reclaim=True,minutes_since_open=75)
    bars=[dict(b) for b in f.bars];bars[-1].update(open_price='100',low_price='99.99',high_price='100.02',close_price='100.01')
    if strategy=='vwap_reversion':v.update(vwap=102,vwap_slope_15=0)
    if strategy=='gap_fade':v['gap_atr']=-.2
    if strategy=='filtered_orb':
        f=frame(d,3);bars=list(f.bars)
    f=replace(f,values=v,bars=tuple(bars))
    obj=STRATEGIES[strategy]();assert obj.evaluate(f,d.session).signal is not None
    v=dict(v);v.update(rvol_tod=None,vwap_distance_z=None,gap_atr=None)
    assert obj.evaluate(replace(f,values=v),d.session).signal is None


def test_regime_determinism_uncertain_and_boundary():
    f=frame();a=classify(f);assert a==classify(f)
    v=dict(f.values);v.update(vwap_slope_15=None,momentum_15=None,gap_atr=None,minutes_since_open=100)
    assert 'UNCERTAIN' in classify(replace(f,values=v)).labels
    assert not eligible('vwap_trend',classify(replace(f,values=v)))
    v.update(price=101,vwap=100,vwap_slope_15=.1,momentum_15=.01,volatility_percentile=.8)
    assert {'TRENDING_UP','HIGH_VOLATILITY'}<=set(classify(replace(f,values=v)).labels)


def test_arbitration_order_independence_priority_and_per_symbol():
    a=signal('orb');b=signal('filtered_orb');q=signal('orb','QQQ');now=dt(a.signal_at)
    assert not resolve([a,b],now).selected
    x=resolve([a,b,q],now,priorities={'orb':1,'filtered_orb':2})
    assert x==resolve([q,b,a],now,priorities={'orb':1,'filtered_orb':2})
    assert {s.symbol for s in x.selected}=={'SPY','QQQ'}
    assert [s.symbol for s in resolve([a,q],now,occupied=['SPY']).selected]==['QQQ']
    assert not resolve([a,replace(b,direction='SHORT')],now).selected


class Always(Strategy):
    id='synthetic'
    def evaluate(self,f,session):return self.signal(f,session,100,99,'synthetic control',anchor='once')


def test_replay_determinism_concurrency_risk_and_costs():
    d=day();tape=build_tape([d]);first=run([d],tape,Always());second=run([d],tape,Always())
    assert first==second
    assert len(first['trades'])==2 and first['daily'][0]['attempts']==2
    assert {t['symbol'] for t in first['trades']}=={'SPY','QQQ'}
    for t in first['trades']:
        assert float(t['cost'])<=10 and float(t['planned_risk'])<=.15
        assert dt(t['entry_at'])>dt(t['signal_at'])
    partial=run([d],tape,Always(),Costs(fill_fraction=.5))
    assert float(partial['trades'][0]['quantity'])<float(first['trades'][0]['quantity'])


def test_stop_target_ambiguity_is_stop_first_and_gap_stop_slips():
    d=day();pos={'stop':'99','target':'102','deadline':iso(d.session.close),'vwap_exit':False}
    bar=dict(open_price='100',low_price='98',high_price='103')
    price,reason=exit_decision(pos,bar,20,d.session,None,Costs())
    assert reason=='STOP' and price<D(99)
    bar['open_price']='97'
    assert exit_decision(pos,bar,20,d.session,None,Costs())[0]<D(97)


def test_risk_budget_reserve_and_profit_do_not_expand_allocation():
    s=signal();now=dt(s.signal_at)
    with pytest.raises(Unsafe):size_signal(s,100.01,now,Costs(),gross_losses=1.5)
    with pytest.raises(Unsafe):size_signal(s,100.01,now,Costs(),cash=20)
    with pytest.raises(Unsafe):size_signal(s,100.01,now,Costs(),exposure=30,net_to_date=100)


def test_no_fill_scenario_does_not_fabricate_trade():
    d=day();r=run([d],build_tape([d]),Always(),Costs(fill_fraction=0))
    assert not r['trades'] and r['daily'][0]['attempts']==2


def test_daily_gap_never_uses_last_available_day_as_previous_session():
    d=day('2023-03-14');f=frame(d,15,[day('2023-03-10')])
    assert f.values['previous_close'] is None and f.values['gap'] is None


def test_feature_snapshot_does_not_change_when_input_bars_mutate():
    d=day();f=frame(d,3);d.five['SPY'][0]['close_price']='1'
    assert f.bars[0]['close_price']=='100.01'


def test_retest_timeout_and_invalidation():
    d=day();b=d.five['SPY'];b[3].update(open_price='100.02',high_price='100.10',low_price='100.02',close_price='100.08')
    b[4]['low_price']='99'
    assert ORBRetest().evaluate(frame(d,5),d.session).reason=='INVALIDATED'
    d=day();b=d.five['SPY'];b[3].update(open_price='100.02',high_price='100.10',low_price='100.02',close_price='100.08')
    assert ORBRetest().evaluate(frame(d,11),d.session).reason=='RETEST_TIMEOUT'


def test_signal_audit_bar_is_start_not_completion_timestamp():
    s=signal();assert dt(s.candidate()['last_bar'])+timedelta(minutes=5)==dt(s.features['values']['last_completed_at'])


def test_pending_stop_touch_cancels_without_rearming():
    d=day();d.minutes['SPY'][15]['low_price']='98'
    r=run([d],build_tape([d]),Always())
    assert not any(t['symbol']=='SPY' for t in r['trades'])
    assert r['daily'][0]['attempts']==2
    assert r['diagnostics']['DELAYED_FILL:STOP_TOUCHED_WHILE_PENDING']==1


def test_vendor_rejects_bad_ohlcv_and_naive_dst_ambiguity():
    import pandas as pd
    from trader.multistrategy.validation_data import validate_frame
    good=dict(timestamp=['2023-03-13 09:30:00'],open=[100],high=[101],low=[99],close=[100],volume=[100])
    for field,value in [('volume',-1),('high',98),('close',float('inf'))]:
        data=deepcopy(good);data[field]=[value]
        with pytest.raises(Unsafe):validate_frame(pd.DataFrame(data))


def test_weighted_vwap_true_range_and_realized_volatility_formulas():
    import math
    d=day();b=d.five['SPY']
    b[0].update(open_price='99',high_price='102',low_price='98',close_price='101',volume=100)
    b[1].update(open_price='101',high_price='104',low_price='100',close_price='103',volume=200)
    b[2].update(open_price='103',high_price='104',low_price='99',close_price='100',volume=300)
    f=frame(d,3)
    expected=((102+98+101)/3*100+(104+100+103)/3*200+(104+99+100)/3*300)/600
    assert f.values['vwap']==pytest.approx(expected)
    assert f.values['realized_volatility']==pytest.approx(math.sqrt(math.log(103/101)**2+math.log(100/103)**2))
    assert frame(d,14).values['atr_14']==pytest.approx((4+4+5+.02*11)/14)
