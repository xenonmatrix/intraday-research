from datetime import timedelta
from dataclasses import replace
import json
import pytest
from trader.calendar import Calendar
from trader.models import dt
from trader.research_signals import Bar,aggregate,candidates
from trader.replay import Day,Costs,position_size,exit_price,metrics,run,load_data

SESSION=Calendar().session(dt('2026-09-24T16:00:00Z'))


def signal(**changes):
    return dict(dict(symbol='SPY',strategy='orb',trigger=100.,stop=99.,dollars=20_000_000,hold=30,target=None,legacy_time=False,id='one'),**changes)


def day():
    bars=[Bar(100,100.1,99.9,100,10000) for _ in range(390)]
    return Day(SESSION,{'SPY':bars,'QQQ':list(bars)},{'SPY':aggregate(bars),'QQQ':aggregate(bars)})


def position(**changes):
    return dict(dict(symbol='SPY',strategy='orb',q=.08,entry=100.,stop=99.,target=102.,entered_index=15,hold=30,legacy_time=False,reached_1r=False),**changes)


def test_rejects_future_bar_and_aggregation_remainder():
    bars=[Bar(100,101,99,100,10000)]*4
    with pytest.raises(ValueError,match='Future'):candidates('SPY',bars,bars,SESSION,SESSION.open+timedelta(minutes=15))
    with pytest.raises(ValueError):aggregate(bars)


def test_prefix_signals_are_unchanged_by_future_prices():
    prefix=[Bar(100,101,99,100.5,10000)]*3
    future=[Bar(900,1000,800,999,90000)]*10
    now=SESSION.open+timedelta(minutes=15)
    assert candidates('SPY',prefix,prefix,SESSION,now)==candidates('SPY',(prefix+future)[:3],(prefix+future)[:3],SESSION,now)


@pytest.mark.parametrize('cash',[1,5,10,100,10000])
def test_sizing_never_uses_outside_capital(cash):
    result=position_size(signal(),100.,cash,0,Costs())
    if result:
        q,price,risk=result
        assert q*price<=9 and q*price<=cash and risk<=.10


def test_remaining_loss_budget_and_friction():
    assert position_size(signal(),100,10,.50,Costs()) is None
    assert position_size(signal(stop=99.999),100,10,0,Costs()) is None
    assert position_size(signal(),100,10,0,Costs(6,1)) is None
    assert position_size(signal(),105,10,0,Costs()) is None


def test_stop_target_collision_uses_stop_and_gap_gets_worse_price():
    d=day();p=position()
    fill,_,why=exit_price(p,Bar(100,103,98,101,1000),16,d,None,Costs())
    assert why=='STOP' and fill<99
    fill,_,why=exit_price(p,Bar(95,96,94,95,1000),16,d,None,Costs())
    assert why=='STOP_GAP' and fill<95


def test_calendar_cutoff_liquidation_and_hard_time_exit():
    d=day();p=position()
    assert exit_price(p,Bar(100,100,100,100,1),375,d,None,Costs())[2]=='SESSION_CLOSE'
    assert exit_price(p,Bar(100,100,100,100,1),45,d,None,Costs())[2]=='TIME'
    assert exit_price(position(legacy_time=True,reached_1r=True),Bar(100,100,100,100,1),45,d,None,Costs()) is None


def test_no_signal_no_trades_and_cost_metrics():
    d=day();r=run([d],{d.session.day:{}},'orb')
    assert r['metrics']['trade_count']==0 and r['metrics']['expectancy'] is None
    t=[dict(net_pnl=.04,gross_pnl=.05,estimated_cost=.01),dict(net_pnl=-.02,gross_pnl=-.01,estimated_cost=.01)]
    m=metrics(t,[dict(pnl=.02,trades=2)])
    assert m['profit_factor']==2 and m['expectancy']==.01 and m['estimated_costs']==.02


def test_one_signal_cannot_reenter_and_never_fills_from_high_alone():
    d=day();sig=signal();f=dict(closes=[100],vwap=99)
    tape={d.session.day:{15:{'SPY':({'orb':sig},f),'QQQ':({},f)}}}
    r=run([d],tape,'orb');assert r['metrics']['trade_count']==1
    assert r['checks']['max_entry']<=9 and r['checks']['max_planned_risk']<=.10
    below=[Bar(98,101,97,98,10000)]*390
    d.minutes={'SPY':below,'QQQ':below}
    assert run([d],tape,'orb')['metrics']['trade_count']==0


def test_no_new_entry_in_final_hour_and_flat_at_liquidation():
    d=day();sig=signal();f=dict(closes=[100],vwap=99)
    tape={d.session.day:{330:{'SPY':({'orb':sig},f),'QQQ':({},f)}}}
    assert run([d],tape,'orb')['metrics']['trade_count']==0


def test_missing_minutes_cannot_be_filled_forward(tmp_path):
    payload=dict(request=dict(interval='minute',bounds='regular',adjustment_type='split'),data=dict(results=[dict(symbol='SPY',interval='minute',bars=[]),dict(symbol='QQQ',interval='minute',bars=[])]))
    (tmp_path/'chunk.json').write_text(json.dumps(payload))
    days,manifest=load_data(tmp_path,'2026-09-24','2026-09-25')
    assert not days and manifest['coverage']==0 and manifest['excluded']


def test_interpolated_source_cannot_produce_a_backtest(tmp_path):
    from trader.models import iso
    rows=[dict(begins_at=iso(SESSION.open+timedelta(minutes=i)),open_price='100',high_price='100',low_price='100',close_price='100',volume=100,session='reg',interpolated=True) for i in range(390)]
    payload=dict(request=dict(interval='minute',bounds='regular',adjustment_type='split'),data=dict(results=[dict(symbol=s,interval='minute',bars=rows) for s in ('SPY','QQQ')]))
    (tmp_path/'chunk.json').write_text(json.dumps(payload))
    days,manifest=load_data(tmp_path,'2026-09-24','2026-09-25')
    assert not days and 'interpolated' in manifest['excluded'][0]['reasons'][0]


def test_changing_future_session_minutes_cannot_change_prior_signal_tape():
    from trader.replay import build_tape
    original=day();changed=day()
    for symbol in ('SPY','QQQ'):
        changed.minutes[symbol][60:]=[Bar(900,1000,800,950,90000)]*330
        changed.five[symbol]=aggregate(changed.minutes[symbol])
    a=build_tape([original])[SESSION.day];b=build_tape([changed])[SESSION.day]
    assert {k:v for k,v in a.items() if k<=60}=={k:v for k,v in b.items() if k<=60}


def test_empty_training_or_validation_cannot_qualify():
    from trader.replay import qualify
    empty=run([],{},'orb')
    reasons=qualify(empty,empty,50)
    assert 'too_few_trades' in reasons
    assert 'insufficient_positive_regimes' in reasons
    assert 'nonpositive_expectancy' in reasons
