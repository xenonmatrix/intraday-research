import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import pytest
from trader.host_health import parse_ntp, parse_power, problems, require_host
from trader.broker import Robinhood
from trader.models import UTC, Unsafe, PreSubmissionBlocked, iso, Config, valid_quote, dt


def evidence(now):
    return dict(at=iso(now), supervisor_pid=123, ac_power=True, battery_percent=100, lid_open=True,
                clock=dict(at=iso(now), offset_seconds=.001, uncertainty_seconds=.002))


@pytest.mark.parametrize('ac_power',[True,False])
@pytest.mark.parametrize('percent,allowed', [(100,True),(51,True),(50,True),(49,False),(0,False),(None,False),(101,False),('99',False),(True,False)])
def test_permanent_battery_minimum_on_ac_and_battery(percent, allowed, ac_power):
    now=dt('2026-09-25T16:00:00Z');r=evidence(now)
    r.update(ac_power=ac_power,battery_percent=percent)
    assert (not problems(r,now)) is allowed


@pytest.mark.parametrize('time', [
    '2026-09-25T03:59:59Z', '2026-09-26T04:00:00Z',
    '2026-09-28T14:00:00Z', '2027-01-04T15:00:00Z',
])
def test_battery_minimum_has_no_date_exception(time):
    now=dt(time);r=evidence(now);r.update(ac_power=False,battery_percent=50)
    assert not problems(r,now)
    r['battery_percent']=49
    assert problems(r,now)


@pytest.mark.parametrize('fault',['clock','lid','stale','unknown_source'])
def test_battery_allowance_preserves_other_host_checks(fault):
    now=dt('2026-09-25T16:00:00Z');r=evidence(now);r.update(ac_power=False,battery_percent=100)
    if fault=='clock':r['clock']['offset_seconds']=.1
    elif fault=='lid':r['lid_open']=False
    elif fault=='stale':r['at']=iso(now-timedelta(seconds=16))
    elif fault=='unknown_source':r['ac_power']=None
    assert problems(r,now)


def test_pmset_power_parsing_and_missing_battery_evidence():
    output="Now drawing from 'Battery Power'\n -InternalBattery-0 (id=23396451)\t100%; discharging; 7:10 remaining present: true\n"
    assert parse_power(output)==dict(ac_power=False,battery_percent=100)
    assert parse_power(output.replace('100%;','25%;'))['battery_percent']==25
    assert parse_power(output.replace('Battery Power','AC Power'))['ac_power'] is True
    assert parse_power(output.replace('present: true','present: false'))['battery_percent'] is None
    assert parse_power(output.replace('100%;','101%;'))['battery_percent'] is None
    assert parse_power('unavailable')==dict(ac_power=None,battery_percent=None)


def test_ntp_ignores_failed_exchange_debug_numbers():
    output='sntp: Exchange failed: Timeout\n offset: -1999640875\n+0.345287 +/- 0.049135 time.apple.com ::1\n'
    assert parse_ntp(output)['offset_seconds']==.345287
    with pytest.raises(Unsafe): parse_ntp('Exchange failed: Timeout')


@pytest.mark.parametrize('change', ['battery','lid','clock_offset','clock_uncertainty','clock_missing','stale','future','stale_clock','nan'])
def test_host_blocks_uncertain_state(change):
    now=datetime.now(UTC);r=evidence(now)
    if change=='battery':r.pop('battery_percent')
    elif change=='lid':r['lid_open']=False
    elif change=='clock_offset':r['clock']['offset_seconds']=.345
    elif change=='clock_uncertainty':r['clock']['uncertainty_seconds']=.1
    elif change=='clock_missing':r['clock']={}
    elif change=='stale':r['at']=iso(now-timedelta(seconds=16))
    elif change=='future':r['at']=iso(now+timedelta(milliseconds=1))
    elif change=='stale_clock':r['clock']['at']=iso(now-timedelta(seconds=121))
    elif change=='nan':r['clock']['offset_seconds']=float('nan')
    assert problems(r,now)


def test_restart_requires_current_supervisor_evidence(tmp_path):
    now=datetime.now(UTC);r=evidence(now)
    (tmp_path/'host-health.json').write_text(json.dumps(r))
    (tmp_path/'supervisor.json').write_text(json.dumps(dict(pid=123)))
    require_host(tmp_path,now)
    (tmp_path/'supervisor.json').write_text(json.dumps(dict(pid=456)))
    with pytest.raises(Unsafe):require_host(tmp_path,now)


def test_real_transport_gate_blocks_buy_but_does_not_block_guarded_exit(tmp_path):
    calls=[]
    class Client:
        async def call_tool(self,name,args,**kwargs):
            calls.append(args['side'])
            return SimpleNamespace(isError=False,structuredContent={'data':{'ok':True}})
    b=Robinhood(Client());b.diagnostic_runtime=tmp_path;b.bind_live_guard(lambda args:None)
    async def run():
        with pytest.raises(PreSubmissionBlocked):await b.submit({'side':'buy'})
        assert calls==[]
        assert await b.submit({'side':'sell'})=={'ok':True}
    asyncio.run(run());assert calls==['sell']


@pytest.mark.parametrize('percent',[49,None])
@pytest.mark.parametrize('ac_power',[True,False])
def test_insufficient_battery_blocks_entry_but_not_guarded_exit(tmp_path,percent,ac_power):
    calls=[]
    class Client:
        async def call_tool(self,name,args,**kwargs):
            calls.append(args['side'])
            return SimpleNamespace(isError=False,structuredContent={'data':{'ok':True}})
    now=datetime.now(UTC);record=evidence(now)
    record.update(ac_power=ac_power,battery_percent=percent)
    (tmp_path/'host-health.json').write_text(json.dumps(record))
    (tmp_path/'supervisor.json').write_text(json.dumps(dict(pid=123)))
    broker=Robinhood(Client());broker.diagnostic_runtime=tmp_path
    broker.bind_live_guard(lambda args:None)
    async def run():
        with pytest.raises(PreSubmissionBlocked,match='battery at least 50%'):
            await broker.submit({'side':'buy'})
        assert calls==[]
        assert await broker.submit({'side':'sell'})=={'ok':True}
    asyncio.run(run());assert calls==['sell']


def test_future_quote_rejection_is_not_relaxed():
    now=datetime.now(UTC)
    q=dict(bid_price='100',ask_price='100.01',has_traded=True,state='active',
           venue_bid_time=iso(now+timedelta(milliseconds=1)),venue_ask_time=iso(now))
    with pytest.raises(Unsafe):valid_quote(q,now,Config())
