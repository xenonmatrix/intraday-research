import asyncio
from datetime import datetime, timedelta
from dataclasses import replace
import json
import pytest
from trader import broker as broker_module
from trader.broker import Robinhood
from trader.calendar import Calendar
from trader.live import LiveEngine
from trader.models import UTC, D, Config, Unsafe, dt, iso
from trader.orb import evaluate
from trader.risk import reconcile
from test_live import live, approve, tick
from test_system import ACCOUNT, bars, snapshot


def opening():
    return [dict(b,begins_at=b['begins_at'].replace('2026-09-23','2026-09-25')) for b in bars()[:3]]


def test_orb_exact_opening_and_causal_expiry():
    now=dt('2026-09-25T13:45:00Z');s=Calendar().session(now);raw=opening()
    assert evaluate('SPY',raw,s,now-timedelta(microseconds=1))['state']=='WAIT_OR'
    c=evaluate('SPY',raw,s,now)
    assert (c['state'],c['trigger'],c['invalidation'],c['setup_id'])==('ARMED','100.01','98.99','2026-09-25:SPY:orb:2')
    future=dict(raw[-1],begins_at=iso(now),high_price='105')
    assert evaluate('SPY',raw+[future],s,now)['state']=='ARMED'
    assert evaluate('SPY',raw+[future],s,now+timedelta(minutes=5))['state']=='MISSED'


@pytest.mark.parametrize('kind',['open','duplicate','missing','zero_volume','placeholder'])
def test_orb_rejects_invalid_source_bars(kind):
    now=dt('2026-09-25T13:45:00Z');s=Calendar().session(now);raw=opening()
    if kind=='open':raw[0]['open_price']='500'
    elif kind=='duplicate':raw.append(dict(raw[0]))
    elif kind=='missing':raw.pop(1)
    elif kind=='zero_volume':raw[0]['volume']=0
    elif kind=='placeholder':raw[0]['interpolated']=True
    with pytest.raises(Unsafe):evaluate('SPY',raw,s,now)


def test_duplicate_positions_cannot_hide_exposure():
    now=datetime.now(UTC);s=snapshot(now)
    s['positions']=[{'symbol':'SPY','quantity':'.2'},{'symbol':'SPY','quantity':'.1'}]
    with pytest.raises(Unsafe,match='Duplicate'):reconcile(s,ACCOUNT,now,Config(),{'SPY':'.1'})


def test_wall_clock_invalidates_cache_after_sleep(monkeypatch):
    b=Robinhood(None);now=datetime.now(UTC);calls=[]
    monkeypatch.setattr(broker_module.time,'monotonic',lambda:100)
    b.account_cache[ACCOUNT]=(99,iso(now-timedelta(seconds=60)),{}, {})
    async def refresh(account):calls.append(account);return {'refreshed':True}
    b.snapshot=refresh
    assert asyncio.run(b.observe(ACCOUNT))=={'refreshed':True}
    assert calls==[ACCOUNT]


def test_orb_live_adapter_entry_and_hard_time_exit(live):
    old,b=live;p=old.policy.project/'live-session.json';settings=json.loads(p.read_text());settings['strategy']='orb';p.write_text(json.dumps(settings))
    e=LiveEngine(old.store,old.root,old.cfg,lambda:b.time)
    b.time=dt('2026-09-24T13:45:01Z');b.bid=D('100.005');b.ask=D('100.01')
    async def raw(*_):return {'SPY':[dict(x,begins_at=x['begins_at'].replace('2026-09-25','2026-09-24')) for x in opening()]}
    b.bars=raw;approve(e)
    session=e.policy.session
    async def step():await e.tick(b,b.time,session,None,{},await b.bars(),{'SPY':dict(tradeable=True,state='active',country='US',fractional_tradability='tradable',account_type_tradabilities=[dict(account_type='individual',account_type_tradability='tradable')])})
    asyncio.run(step());assert len(b.submissions)==1
    pos=e.store.get('position');assert pos['strategy']=='orb';pos['reached_1r']=True;e.store.save('position',pos)
    b.time+=timedelta(minutes=30);b.bid=D('101.1');b.ask=D('101.11')
    # Direct verified exit path; reaching +1R must not extend plain ORB's hold.
    asyncio.run(e.exit_if_needed(b,session,asyncio.run(b.snapshot()),asyncio.run(b.quotes()),{}))
    assert not e.store.get('position') and len(b.submissions)==2
    payload=json.loads(e.store.db.execute("select payload from intents where intent_key like 'exit:%'").fetchone()[0]);assert payload['reason']=='TIME'


def test_account_reserve_not_spent_by_experiment(live):
    e,b=live;approve(e);original=b.snapshot
    async def low(*args):
        s=await original(*args);s['portfolio']['buying_power'].update(buying_power='20',unleveraged_buying_power='20');return s
    b.snapshot=low;tick(e,b);assert not b.submissions


def test_actual_fill_above_entry_ceiling_requests_verified_liquidation(live):
    e,b=live
    payload=dict(symbol='SPY',side='buy',dollar_amount='9',setup_id='test',stop='100',target='102')
    e.store.intent('oversized-fill','LIVE',payload)
    with e.store.db:e.store.db.execute("update intents set broker_id='test-order',status='SUBMITTED'")
    order=dict(id='test-order',symbol='SPY',side='buy',market_hours='regular_hours',type='market',
               state='filled',quantity='.095',cumulative_quantity='.095',
               executions=[dict(id='test-fill',quantity='.095',price='100',fees='0',timestamp=iso(b.time))])
    e.ingest('oversized-fill',order)
    assert D(e.store.get('position')['cost'])==D('9.5')
    assert (e.policy.runtime/'LIQUIDATE').exists()
    assert not b.submissions
