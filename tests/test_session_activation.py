"""Scheduled activation invokes only fake readiness/transition functions."""
import asyncio,json
import pytest
from trader import configuration as C
from trader.models import dt,Unsafe
from trader.persistent.db import Ledger
from ops import activate_session as A

@pytest.fixture
def authorized(tmp_path):
    C.write(tmp_path,C.validate({}));r=tmp_path/'runtime';r.mkdir(exist_ok=True)
    db=Ledger(r/'persistent.sqlite3');db.close()
    holds={'READ_ONLY':'testing','MAINTENANCE':'testing','KILL':'Worker exited; reconciliation/recovery required\n'}
    for name,value in holds.items():(r/name).write_text(value)
    return tmp_path,dict(day='2026-10-02',config_fingerprint=C.fingerprint(C.load(tmp_path)),event_id=0,holds=holds)

@pytest.mark.parametrize('at,state',[('2026-10-02T13:44:59Z','WAIT'),('2026-10-02T13:45:00Z','DUE')])
def test_activation_respects_opening_window(authorized,at,state):
    root,auth=authorized;assert A.scope(root,auth,dt(at))==state

@pytest.mark.parametrize('at',['2026-10-02T19:00:00Z','2026-10-03T13:45:00Z'])
def test_activation_expires(authorized,at):
    root,auth=authorized
    with pytest.raises(Unsafe):A.scope(root,auth,dt(at))

@pytest.mark.parametrize('change',['config','hold','stop','transition','disable'])
def test_operator_changes_revoke_activation(authorized,change):
    root,auth=authorized;r=root/'runtime'
    if change=='config':C.write(root,C.validate({'clock':{'max_combined_bound_ms':'90'}}))
    elif change=='hold':(r/'MAINTENANCE').write_text('operator pause')
    elif change in ('stop','transition'):(r/change.upper()).write_text('operator')
    else:
        db=Ledger(r/'persistent.sqlite3');db.configure('live_enabled',False);db.close()
    with pytest.raises(Unsafe):A.scope(root,auth,dt('2026-10-02T13:45:00Z'))

@pytest.mark.parametrize('ready',[False,True])
def test_every_gate_required_before_controlled_transition(authorized,monkeypatch,ready):
    root,auth=authorized;calls=[]
    async def doctor(*args):return {'ready_for_transition':ready,'blockers':[] if ready else ['clock failed']}
    async def transition(*args,**kwargs):calls.append(kwargs)
    monkeypatch.setattr(A,'doctor',doctor);monkeypatch.setattr(A,'transition',transition)
    result=asyncio.run(A.attempt(root,auth,dt('2026-10-02T13:45:00Z')))
    assert result['state']==('DONE' if ready else 'WAIT')
    assert calls==([dict(activate=True,acknowledge=True,recover_kill=True)] if ready else [])

def test_no_broker_checks_before_opening_range(authorized,monkeypatch):
    root,auth=authorized
    async def forbidden(*args):raise AssertionError('Premature broker/activation call')
    monkeypatch.setattr(A,'doctor',forbidden);monkeypatch.setattr(A,'transition',forbidden)
    assert asyncio.run(A.attempt(root,auth,dt('2026-10-02T12:00:00Z')))['state']=='WAIT'
