import json,os
from datetime import timedelta
import pytest
from trader.models import iso,Unsafe
from trader.session_report import update
from test_live import live,approve
from test_system import snapshot
from test_unattended import clear_opening,host_evidence


def marker(e):
    r=e.policy.runtime
    (r/'MAINTENANCE').write_text('read only')
    (r/'KILL').write_text('keep blocked')
    (r/'READ_ONLY_SESSION.json').write_text(json.dumps(dict(read_only=True,session_date=e.policy.day,start=iso(e.policy.session.open-timedelta(hours=4)),expires=iso(e.policy.session.close+timedelta(minutes=5)))))


def test_observation_awake_without_authorizing_orders(live):
    e,b=live;marker(e)
    assert e.unattended.keep_awake(e.policy.session.open-timedelta(hours=1))
    with pytest.raises(Unsafe):e.policy.authorize('buy')
    with pytest.raises(Unsafe):e.policy.authorize('sell')
    assert not e.unattended.keep_awake(e.policy.session.close+timedelta(minutes=5))
    (e.policy.runtime/'STOP').write_text('stop')
    assert not e.unattended.keep_awake(e.policy.session.open)


def test_opening_requires_valid_clock_even_with_account_health(live):
    e,b=live;approve(e);clear_opening(e);t=e.policy.session.open;host_evidence(e,t)
    p=e.policy.runtime/'host-health.json';h=json.loads(p.read_text());h['clock']['offset_seconds']=.3;p.write_text(json.dumps(h))
    e.unattended.health(t-timedelta(seconds=1),True,snapshot_at=iso(t-timedelta(seconds=1)))
    e.unattended.checkpoint(t,os.getpid(),dict(pid=os.getpid(),at=t.timestamp(),state='POLLING'))
    assert (e.unattended.root/'halt-opening.json').exists()


def test_premarket_report_does_not_require_future_bars(live):
    e,b=live;marker(e);b.time=e.policy.session.open-timedelta(hours=1)
    e.store.save('broker_snapshot',snapshot(b.time));e.report(b.time,e.policy.session)
    p=e.policy.project/'reports'/e.policy.day/'latest.json';r=json.loads(p.read_text())
    assert r['opening_health']['phase']=='PENDING_REGULAR_OPEN'
    assert r['end_of_day_state']=='PENDING' and not r['orders'] and not r['fills']
    assert r['gates']['MAINTENANCE'] and r['gates']['KILL']
    assert not e.store.get('observation_opening:'+e.policy.day)


def test_eod_requires_post_close_reconciliation(live):
    e,b=live;marker(e);b.time=e.policy.session.close
    e.store.save('broker_snapshot',snapshot(b.time-timedelta(seconds=1)));e.report(b.time,e.policy.session)
    p=e.policy.project/'reports'/e.policy.day/'end-of-day.json'
    assert json.loads(p.read_text())['end_of_day_state']=='UNVERIFIED_OR_NOT_FLAT'
    e.store.save('broker_snapshot',snapshot(b.time));e.report(b.time,e.policy.session)
    assert json.loads(p.read_text())['end_of_day_state']=='FLAT_READ_VERIFIED'
    assert not e.store.rows() and not e.store.unresolved() and not b.submissions
