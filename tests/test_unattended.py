"""Unattended policy tests use only temporary state, clocks and fake brokers."""
from datetime import timedelta
import json
import os
import pytest
from trader.live import LiveEngine
from trader.models import Unsafe,iso,dt
from trader.unattended import Unattended
from test_live import live,approve,tick,NOW


def opening_check(e,healthy=True,age=2,heartbeat_age=1,late=0):
    t=e.policy.session.open
    u=e.unattended
    host_evidence(e,t)
    u.health(t-timedelta(seconds=age),healthy,snapshot_at=iso(t-timedelta(seconds=age)))
    u.checkpoint(t+timedelta(seconds=late),os.getpid(),dict(pid=os.getpid(),at=(t-timedelta(seconds=heartbeat_age)).timestamp(),state='ENTRIES_BLOCKED'))


def host_evidence(e,t):
    r=e.policy.runtime
    (r/'supervisor.json').write_text(json.dumps(dict(pid=os.getpid())))
    (r/'host-health.json').write_text(json.dumps(dict(at=iso(t),supervisor_pid=os.getpid(),ac_power=True,battery_percent=100,lid_open=True,clock=dict(at=iso(t),offset_seconds=0,uncertainty_seconds=.001))))


def clear_opening(e):
    (e.unattended.root/'opening.json').unlink(missing_ok=True)


def test_unhealthy_opening_latches_entire_session(live):
    e,b=live; approve(e); clear_opening(e)
    opening_check(e,healthy=False)
    tick(e,b)
    assert not b.submissions
    e.unattended.health(b.time,True,snapshot_at=iso(b.time))
    e.unattended.checkpoint(b.time,os.getpid(),{})
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(restarted,b)
    assert not b.submissions
    assert (e.unattended.root/'halt-opening.json').exists()


@pytest.mark.parametrize('kwargs',[{'age':16},{'heartbeat_age':16},{'late':6}])
def test_stale_opening_or_missed_checkpoint_disables_session(live,kwargs):
    e,b=live; approve(e); clear_opening(e)
    opening_check(e,**kwargs)
    with pytest.raises(Unsafe): e.policy.authorize('buy')
    e.policy.authorize('sell')


def test_healthy_opening_allows_normal_preflight(live):
    e,b=live; approve(e); clear_opening(e)
    opening_check(e)
    tick(e,b)
    assert len(b.submissions)==1


def test_postopen_health_update_preserves_preopen_evidence(live):
    e,b=live; approve(e); clear_opening(e)
    t=e.policy.session.open
    host_evidence(e,t)
    e.unattended.health(t-timedelta(seconds=2),True,snapshot_at=iso(t-timedelta(seconds=3)))
    e.unattended.health(t+timedelta(milliseconds=100),True,snapshot_at=iso(t))
    e.unattended.checkpoint(t+timedelta(seconds=1),os.getpid(),dict(pid=os.getpid(),at=t.timestamp(),state='POLLING'))
    e.policy.authorize('buy')


def test_new_worker_cannot_reuse_old_worker_opening_health(live):
    e,b=live; approve(e); clear_opening(e)
    t=e.policy.session.open
    e.unattended.health(t-timedelta(seconds=1),True,snapshot_at=iso(t-timedelta(seconds=1)))
    e.unattended.checkpoint(t,os.getpid()+1,dict(pid=os.getpid()+1,at=t.timestamp(),state='POLLING'))
    with pytest.raises(Unsafe): e.policy.authorize('buy')


def test_no_opening_decision_blocks_entry(live):
    e,b=live; approve(e); clear_opening(e)
    tick(e,b)
    assert not b.submissions


def test_three_cumulative_failures_survive_success_and_restart(live):
    e,b=live; approve(e)
    for i in range(3):
        e.unattended.failure(e.store,b.time,'ConnectionError')
        for _ in range(3):
            b.time+=timedelta(seconds=11)
            e.unattended.success(e.store,b.time)
    assert e.store.get('unattended_failures:'+e.policy.day)==3
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(restarted,b)
    assert not b.submissions
    restarted.policy.authorize('sell')
    assert (e.unattended.root/'halt-connectivity.json').exists()


def test_tonight_failure_does_not_consume_tomorrow_allowance(live):
    e,b=live; approve(e)
    e.unattended.failure(e.store,dt('2026-09-24T00:00:00Z'),'ConnectionError')
    assert e.store.get('unattended_failures:'+e.policy.day,0)==0


def test_engine_reconciliation_failures_are_counted(live):
    e,b=live; approve(e); b.snapshot_error=PermissionError('Refresh unavailable')
    for _ in range(3):
        tick(e,b)
        b.time+=timedelta(seconds=30)
    assert e.store.get('unattended_failures:'+e.policy.day)==1
    assert (e.unattended.root/'halt-connectivity.json').exists()
    b.snapshot_error=None
    tick(e,b)
    assert not b.submissions


def test_cutoff_permanent_exit_allowed_then_no_reopening(live):
    e,b=live; approve(e); tick(e,b)
    b.time=dt('2026-09-24T19:45:00Z')
    e.unattended.checkpoint(b.time,os.getpid(),{})
    assert (e.unattended.root/'halt-cutoff.json').exists()
    assert (e.unattended.root/'liquidation-due.json').exists()
    tick(e,b)
    assert len(b.submissions)==2 and b.submissions[-1]['side']=='sell'
    assert e.store.get('position') is None
    tick(e,b)
    assert len(b.submissions)==2
    b.time=NOW  # even clock rollback cannot remove the persisted cutoff
    with pytest.raises(Unsafe): e.policy.authorize('buy')


def test_connection_latch_does_not_disable_verified_exit(live):
    e,b=live; approve(e); tick(e,b)
    for _ in range(3): e.unattended.failure(e.store,b.time,'offline')
    e.store.save('recovery_exit',True)
    tick(e,b)
    assert len(b.submissions)==2 and b.submissions[-1]['side']=='sell'
    assert e.store.get('position') is None


def test_close_expires_authorization_even_without_supervisor(live):
    e,b=live; approve(e); b.time=e.policy.session.close
    with pytest.raises(Unsafe,match='EXPIRED'): e.policy.approval()
    for side in ('buy','sell'):
        with pytest.raises(Unsafe): e.policy.authorize(side)
    e.unattended.checkpoint(b.time,os.getpid(),{})
    assert (e.unattended.root/'expired.json').exists()
    b.time=NOW
    with pytest.raises(Unsafe,match='EXPIRED'): e.policy.approval()


def test_sleep_prevention_covers_tonight_and_eight_am(live):
    e,b=live; approve(e)
    for when in ('2026-09-24T01:00:00Z','2026-09-24T12:00:00Z'):
        b.time=dt(when)
        assert e.unattended.keep_awake(b.time)
    b.time=e.policy.session.close
    assert not e.unattended.keep_awake(b.time)


def test_audit_retains_failures_and_one_time_decisions(live):
    e,b=live; approve(e)
    e.unattended.failure(e.store,b.time,'ConnectionError')
    e.unattended.checkpoint(e.policy.session.close,os.getpid(),{})
    e.unattended.checkpoint(e.policy.session.close,os.getpid(),{})
    events=[json.loads(line) for line in (e.policy.runtime/'unattended.jsonl').read_text().splitlines()]
    assert sum(x['kind']=='expired' for x in events)==1
    assert any(x['kind']=='CONNECTIVITY_OR_RECONCILIATION_FAILURE' for x in events)
