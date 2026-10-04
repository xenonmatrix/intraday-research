"""Broker fake only: evidence gates, final risk veto and simulated namespaces."""
import asyncio,json
from dataclasses import replace
from datetime import timedelta
import pytest
from trader.models import Unsafe,D,iso
from trader.multistrategy.registry import Registry,REQUIRED
from trader.multistrategy.strategies import ORB,Parameters
from trader.multistrategy.integration import MultiCoordinator,make_coordinator
from trader.multistrategy.shadow import ShadowObserver
from trader.multistrategy.features import Frame
from trader.multistrategy.regimes import Regime
from trader.multistrategy.study import interval,decision
from test_persistent import env,raw,eligible_symbols,observe,tick,host
from test_multistrategy import day,frame,signal,Always
from test_system import quote


def document(status='RESEARCH_ONLY',enabled=False,mode='MULTI_LIVE'):
    return dict(version=1,mode=mode,shadow_enabled=False,strategies=[dict(id='orb',status=status,live_enabled=enabled,shadow_enabled=True,
        evidence=dict(strategy_fingerprint=ORB().fingerprint,protocol_hash='0'*64,data_digest='1'*64,report_digest='2'*64,checks=dict.fromkeys(REQUIRED,True)))])


@pytest.mark.parametrize('status',['REJECTED','RESEARCH_ONLY','SHADOW_ELIGIBLE','LIVE_CAPABLE_PENDING_MORE_EVIDENCE'])
def test_non_live_status_cannot_be_enabled(status):
    with pytest.raises(Unsafe):Registry(document(status,True))


@pytest.mark.parametrize('check',REQUIRED)
def test_each_validation_condition_required(check):
    doc=document('LIVE_ELIGIBLE',True);doc['strategies'][0]['evidence']['checks'][check]=False
    with pytest.raises(Unsafe):Registry(doc)


@pytest.mark.parametrize('mode',['SHADOW','BASELINE'])
def test_no_shadow_readonly_fallback_to_live(mode):
    with pytest.raises(Unsafe):Registry(document('LIVE_ELIGIBLE',True,mode))


def test_parameter_change_invalidates_evidence():
    doc=document('LIVE_ELIGIBLE',True);doc['strategies'][0]['parameters']={'target_r':2.5}
    with pytest.raises(Unsafe):Registry(doc)


def test_capable_disabled_has_no_live_permission():
    assert not Registry(document('LIVE_ELIGIBLE',False)).permitted('orb','LIVE')


@pytest.mark.parametrize('status',['RESEARCH_ONLY','SHADOW_ELIGIBLE','LIVE_ELIGIBLE'])
def test_disabled_strategy_cannot_submit(env,status):
    old,b,db=env;e=MultiCoordinator(old.root,db,lambda:b.time,registry=Registry(document(status,False)))
    e.account=old.account;b.guard=e.guard;b.cancel_guard=e.cancel_guard
    tick(e,b);assert not b.calls and not db.orders()


def enable_fixture(env,monkeypatch):
    import trader.multistrategy.integration as integration
    old,b,db=env;e=MultiCoordinator(old.root,db,lambda:b.time,registry=Registry(document('LIVE_ELIGIBLE',True)))
    e.account=old.account;b.guard=e.guard;b.cancel_guard=e.cancel_guard
    # Isolate execution wiring from regime definition, covered separately.
    monkeypatch.setattr(integration,'classify',lambda f:Regime(('TRENDING_UP',),('synthetic test evidence',)))
    return e,b,db


def test_enabled_validated_candidate_uses_existing_execution_and_durable_identity(env,monkeypatch):
    e,b,db=enable_fixture(env,monkeypatch);tick(e,b)
    assert len(b.calls)==2
    assert {t['symbol'] for t in db.trades()}=={'SPY','QQQ'}
    for t in db.trades():
        assert t['strategy_signal']['fingerprint']==ORB().fingerprint
        assert D(t['cost'])<=10 and D(t['planned_risk'])<=D('.15')
    tick(e,b);assert len(b.calls)==2


def test_final_risk_engine_veto_still_applies(env,monkeypatch):
    e,b,db=enable_fixture(env,monkeypatch)
    with db.tx():
        d=db.session(e.session().day);d.update(loss_latched=True,gross_losses='1.5');db.save_session(e.session().day,d)
    tick(e,b);assert not b.calls and not db.orders()


def test_registry_hot_change_blocks_new_entry(env,monkeypatch,tmp_path):
    e,b,db=enable_fixture(env,monkeypatch);path=tmp_path/'strategies.json';path.write_text(json.dumps(document('LIVE_ELIGIBLE',True)))
    e.registry_path=path;observe(e,b)
    path.write_text(json.dumps(document('LIVE_ELIGIBLE',False)))
    with pytest.raises(Unsafe,match='controlled restart'):e.health_check(b.time)


def test_default_factory_preserves_baseline_and_rejects_shadow_worker(env):
    e,b,db=env;assert type(make_coordinator(e.root,db)).__name__=='Coordinator'
    (e.root/'config').mkdir();(e.root/'config/multistrategy.json').write_text(json.dumps(document(mode='SHADOW')))
    with pytest.raises(Unsafe):make_coordinator(e.root,db)


def test_shadow_refuses_real_ledger(env):
    e,b,db=env
    with pytest.raises(Unsafe,match='separate'):ShadowObserver(e.root/'runtime/persistent.sqlite3',Registry(document()))
    assert not db.orders()


def test_shadow_has_no_broker_capability_is_durable_and_duplicate_safe(tmp_path):
    d=day();r=Registry(document(mode='SHADOW'));r.objects['orb']=Always()
    path=tmp_path/'shadow.sqlite3';s=ShadowObserver(path,r);now=d.session.open+timedelta(minutes=15)
    quotes={sym:quote(now,bid='100.005',ask='100.01') for sym in ('SPY','QQQ')}
    s.feed(d.session,now,d.five,quotes,healthy=True)
    with pytest.raises(Unsafe):s.feed(d.session,now,d.five,quotes,healthy=True)
    s.close();s=ShadowObserver(path,r);now+=timedelta(seconds=2)
    quotes={sym:quote(now,bid='100.005',ask='100.01') for sym in quotes}
    s.feed(d.session,now,d.five,quotes,healthy=True)
    book=json.loads(s.db.execute('SELECT data FROM shadow_books').fetchone()[0]);assert len(book['positions'])==2 and book['attempts']==2
    assert not hasattr(s,'broker') and not hasattr(s,'submit')
    assert all(row[0].startswith('shadow:') for row in s.db.execute('SELECT id FROM shadow_events'))
    before=s.db.total_changes
    with pytest.raises(Unsafe):s.feed(d.session,now+timedelta(seconds=10),d.five,quotes,healthy=False)
    assert s.db.total_changes==before
    s.close()


@pytest.mark.parametrize('offset',[-100,100])
def test_shadow_stale_future_quotes_fail_without_writes(tmp_path,offset):
    d=day();s=ShadowObserver(tmp_path/'shadow.sqlite3',Registry(document(mode='SHADOW')));now=d.session.open+timedelta(minutes=15)
    quotes={sym:quote(now+timedelta(seconds=offset)) for sym in ('SPY','QQQ')}
    with pytest.raises(Unsafe):s.feed(d.session,now,d.five,quotes,healthy=True)
    assert s.db.execute('SELECT count(*) FROM shadow_events').fetchone()[0]==0;s.close()


def test_bootstrap_insufficient_sample_returns_no_precision():
    gates={'minimum_trade_days_for_interval':20}
    assert interval([],[],gates)['lower'] is None
