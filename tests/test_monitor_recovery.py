import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
import pytest
import httpx
from trader import broker as broker_module, service
from trader.broker import Robinhood,BrokerToolError,failure_details
from trader.models import Mode,Unsafe
from trader.live import LiveEngine
from test_live import live,approve,NOW
from test_system import snapshot,ACCOUNT


def test_observe_caches_only_core_and_snapshot_remains_fresh(monkeypatch):
    b=Robinhood(None); calls=[];clock=[0];positions=[]
    monkeypatch.setattr(broker_module.time,'monotonic',lambda:clock[0])
    async def call(name,args):
        calls.append(name)
        if name=='get_accounts':return {'accounts':[snapshot()['account']]}
        if name=='get_portfolio':return snapshot()['portfolio']
        if name=='get_equity_positions':return {'positions':list(positions)}
        if name=='get_equity_orders':return {'orders':[]}
    b.call=call
    async def run():
        first=await b.observe(ACCOUNT)
        clock[0]=2
        positions.append({'symbol':'SPY','quantity':'.01'})
        second=await b.observe(ACCOUNT)
        assert second['positions'] and second['observed_at']==first['observed_at']
        assert calls.count('get_portfolio')==1
        # Actual order preflight always bypasses the core observation cache.
        await b.snapshot(ACCOUNT)
        assert calls.count('get_portfolio')==2
        clock[0]=11
        await b.observe(ACCOUNT)
        assert calls.count('get_portfolio')==3
        assert calls.count('get_equity_positions')==4
        assert calls.count('get_equity_orders')==4
    asyncio.run(run())


def test_failed_core_refresh_never_returns_old_snapshot(monkeypatch):
    b=Robinhood(None)
    b.account_cache[ACCOUNT]=(0,'2026-09-24T13:30:00Z',{}, {})
    monkeypatch.setattr(broker_module.time,'monotonic',lambda:9)
    async def fail(*_):raise ConnectionError('offline')
    b.snapshot=fail
    with pytest.raises(ConnectionError):asyncio.run(b.observe(ACCOUNT))


def test_slow_snapshot_does_not_extend_core_cache_lifetime(monkeypatch):
    b=Robinhood(None);clock=[0];portfolio_calls=[]
    monkeypatch.setattr(broker_module.time,'monotonic',lambda:clock[0])
    async def call(name,args):
        if name=='get_accounts':
            clock[0]+=6
            return {'accounts':[snapshot()['account']]}
        if name=='get_portfolio':
            portfolio_calls.append(1)
            return snapshot()['portfolio']
        return {'positions':[]} if name=='get_equity_positions' else {'orders':[]}
    b.call=call
    async def run():
        await b.observe(ACCOUNT)
        clock[0]=9
        await b.observe(ACCOUNT)
    asyncio.run(run())
    assert len(portfolio_calls)==2


def test_many_retries_are_one_episode_and_outage_latches(live):
    e,b=live;approve(e)
    for i in range(165):e.unattended.failure(e.store,b.time,'offline')
    assert e.store.get('unattended_failures:'+e.policy.day)==1
    with pytest.raises(Unsafe,match='recovery'):e.policy.authorize('buy')
    e.policy.authorize('sell')
    b.time+=timedelta(seconds=61)
    e.unattended.failure(e.store,b.time,'offline')
    assert (e.unattended.root/'halt-connectivity.json').exists()
    assert e.store.get('unattended_failures:'+e.policy.day)==1


def test_short_recovery_never_unblocks_entries(live):
    e,b=live;approve(e)
    e.unattended.failure(e.store,b.time,'offline')
    for i in range(3):e.unattended.success(e.store,b.time+timedelta(seconds=i+1))
    with pytest.raises(Unsafe):e.policy.authorize('buy')
    b.time+=timedelta(seconds=31);e.unattended.success(e.store,b.time)
    e.policy.authorize('buy')
    assert e.store.get('unattended_failures:'+e.policy.day)==1


def test_maintenance_blocks_both_sides(live):
    e,b=live;approve(e);(e.policy.runtime/'MAINTENANCE').write_text('read only')
    for side in ('buy','sell'):
        with pytest.raises(Unsafe,match='maintenance'):e.policy.authorize(side)


def test_backoff_is_bounded():
    assert [service.reconnect_delay(i) for i in range(1,8)]==[2,4,8,16,30,30,30]
    assert service.reconnect_delay(10,True)==5


def test_tool_error_keeps_details_inside_exception_group():
    result=SimpleNamespace(structuredContent={'error':{'code':'rate_limit','message':'retry later'}},content=[])
    error=BrokerToolError('get_portfolio',result)
    assert error.retry_after==60
    details=failure_details(ExceptionGroup('outer',[ExceptionGroup('inner',[error])]))
    assert 'get_portfolio' in details[0] and 'rate_limit' in details[0]


@pytest.mark.parametrize('transport_failure',[False,True,'goaway'])
def test_monitor_reuses_tool_error_session_but_reconnects_transport(live,monkeypatch,transport_failure):
    e,b=live;approve(e)
    root=e.policy.project;runtime=e.policy.runtime
    (runtime/'MAINTENANCE').write_text('test read only')
    b.connection_info={'connection_id':'fake'};b.call_counts={};b.cycle_error=None
    collection_order=[]
    async def observe(*args):
        collection_order.append('account')
        return await b.snapshot(*args)
    b.observe=observe
    b.bind_live_guard=lambda *args:None
    connections=[];delays=[];quote_calls=[]
    original=b.quotes
    async def quotes(*args):
        collection_order.append('quote')
        quote_calls.append(1)
        if len(quote_calls)==1:
            b.cycle_error=ConnectionError('offline') if transport_failure else BrokerToolError('get_portfolio',SimpleNamespace(structuredContent={'error':'temporary backend failure'},content=[]))
            if transport_failure == 'goaway':
                b.cycle_error=httpx.RemoteProtocolError('ConnectionTerminated error_code:0, last_stream_id:31')
            raise b.cycle_error
        (runtime/'STOP').write_text('finish test')
        return await original(*args)
    b.quotes=quotes
    @asynccontextmanager
    async def connect(*args,**kwargs):
        connections.append(1)
        yield b
    async def pause(*args):delays.append(args[2])
    class Clock:
        @staticmethod
        def now(*args):return b.time
    monkeypatch.setattr(service,'connect',connect)
    monkeypatch.setattr(service,'pause_monitor',pause)
    monkeypatch.setattr(service,'datetime',Clock)
    monkeypatch.setattr(service,'LiveEngine',lambda store,path:LiveEngine(store,path,e.cfg,lambda:b.time))
    asyncio.run(service.run(root,Mode.LIVE))
    assert len(connections)==(2 if transport_failure else 1)
    assert collection_order==['account','quote','account','quote']
    assert delays[0]==(2 if transport_failure else 5)
    assert not b.submissions
    assert e.store.get('unattended_failures:'+e.policy.day)==1


def test_reported_throttle_delay_is_respected():
    result=SimpleNamespace(structuredContent=None,content=[SimpleNamespace(type='text',text='buying power fetch: API error 429: Request was throttled. Expected available in 7 seconds.')])
    error=BrokerToolError('get_portfolio',result)
    assert error.retry_after==7.5


def test_supervisor_latches_sustained_outage_during_backoff(live):
    e,b=live;approve(e)
    e.unattended.failure(e.store,b.time,'offline')
    e.unattended.checkpoint(b.time+timedelta(seconds=61),123,{})
    assert (e.unattended.root/'halt-connectivity.json').exists()
