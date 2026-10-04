"""LIVE safety tests: clocks, broker and authorization files are isolated fakes."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shutil
import uuid
import pytest
from trader.broker import Robinhood
from trader.live import LiveEngine
from trader.models import Config,D,Unsafe,dt,iso,down
from trader.store import Store,atomic_text
from test_system import snapshot,quote,bars,tradability,ACCOUNT

NOW=dt("2026-09-24T13:55:01Z")


class Broker:
    def __init__(self):
        self.time=NOW
        self.orders={}
        self.submissions=[]
        self.cancels=[]
        self.bid=D("101.005")
        self.ask=D("101.01")
        self.quote_time=None
        self.snapshot_error=None
        self.ambiguous=False
        self.pending=False
        self.partial=False
        self.guard=None
        self.cancel_guard=None

    def positions(self):
        qty=sum((sum(D(e["quantity"]) for e in o["executions"])*(1 if o["side"]=="buy" else -1) for o in self.orders.values()),D(0))
        return [] if not qty else [dict(symbol="SPY",quantity=str(qty),shares_available_for_sells=str(qty))]

    async def snapshot(self,*_):
        if self.snapshot_error: raise self.snapshot_error
        s=snapshot(self.time)
        s["positions"]=self.positions()
        s["orders"]=list(self.orders.values())
        return s

    async def quotes(self,*_):
        return {"SPY":quote(self.quote_time or self.time,bid=str(self.bid),ask=str(self.ask))}

    async def bars(self,*_):
        return {"SPY":[dict(b,begins_at=b["begins_at"].replace("2026-09-23","2026-09-24")) for b in bars()]}

    async def tradability(self,*_): return tradability()

    async def review_order(self,args):
        return dict(args,order_checks={},market_data_disclosure="TEST QUOTE ONLY")

    async def submit(self,args):
        if self.guard: self.guard(args)
        self.submissions.append(dict(args))
        oid=str(uuid.uuid4())
        price=self.ask if args["side"]=="buy" else self.bid
        qty=down(D(args["dollar_amount"])/price) if args["side"]=="buy" else D(args["quantity"])
        filled=D(0) if self.pending else down(qty/2) if self.partial else qty
        executions=[] if not filled else [dict(id=str(uuid.uuid4()),quantity=str(filled),price=str(price),fees="0",timestamp=iso(self.time))]
        state="confirmed" if self.pending else "partially_filled" if self.partial else "filled"
        self.orders[oid]=dict(id=oid,symbol=args["symbol"],side=args["side"],quantity=str(qty),cumulative_quantity=str(filled),
                              executions=executions,state=state,market_hours="regular_hours",type="market",fees="0")
        if self.ambiguous: raise TimeoutError("Transport lost after broker acceptance")
        return {"order":{"id":oid}}

    async def order(self,account,oid): return self.orders[oid]

    async def cancel_owned(self,args):
        if self.cancel_guard: self.cancel_guard(args)
        self.cancels.append(args)
        o=self.orders[args["order_id"]]
        o["state"]="partially_filled_rest_cancelled" if D(o["cumulative_quantity"]) else "cancelled"
        return {"accepted":True}


@pytest.fixture
def live(tmp_path):
    # Historical fixture must not inherit the active production day/strategy.
    settings=json.loads((Path(__file__).resolve().parent/"fixtures/legacy-session.json").read_text())
    settings.update(session_date='2026-09-24',strategy='existing_orb_pullback_vwap')
    (tmp_path/'live-session.json').write_text(json.dumps(settings))
    root=tmp_path/"runtime"/"LIVE"
    store=Store(root/"state.sqlite3")
    b=Broker()
    e=LiveEngine(store,root,replace(Config(),universe=("SPY",)),lambda:b.time)
    e.account=ACCOUNT
    store.save("account_number",ACCOUNT)
    yield e,b
    store.close()


def approve(e):
    # Never used with the actual project/runtime. Every fixture lives in tmp_path.
    atomic_text(e.policy.runtime/"live-approval.json",json.dumps(dict(approved=True,session_date=e.policy.day,policy_sha256=e.policy.digest,account_suffix="0000")))
    # Existing execution tests assume the separately-tested opening check passed.
    e.unattended.mark('opening',e.policy.session.open,healthy=True)


def tick(e,b):
    b.guard=e.write_guard
    b.cancel_guard=e.cancel_guard
    async def go():
        # Supply a previous snapshot; engine MUST fetch its own fresh one.
        await e.tick(b,b.time,e.policy.calendar.session(b.time),snapshot(b.time),await b.quotes(),await b.bars(),tradability())
    asyncio.run(go())


def test_disarmed_never_submits(live):
    e,b=live
    tick(e,b)
    assert not b.submissions
    assert "DISARMED" in e.store.get("gate")["reason"]


def test_qualified_entry_with_actual_fill(live):
    e,b=live; approve(e); tick(e,b)
    assert len(b.submissions)==1
    assert D(b.submissions[0]["dollar_amount"]) <= 9
    p=e.store.get("position")
    assert p and D(p["cost"]) <= 10
    row=e.store.db.execute("SELECT payload,status FROM intents").fetchone()
    assert D(json.loads(row[0])["planned_risk"]) <= D(".10") and row[1]=="FILLED"
    assert len(e.store.rows())==1
    assert e.store.rows()[0]["fill_price"]==str(b.ask)
    tick(e,b)
    assert len(b.submissions)==1  # no top-up


@pytest.mark.parametrize("when",["2026-09-23T14:00:00Z","2026-09-24T13:40:00Z","2026-09-24T19:01:00Z","2026-09-24T20:01:00Z","2026-09-25T14:00:00Z"])
def test_session_date_and_entry_windows(live,when):
    e,b=live; approve(e); b.time=dt(when); tick(e,b)
    assert not b.submissions


@pytest.mark.parametrize("case",["stale","outage","auth","mismatch","margin","short","crypto"])
def test_fail_closed_live(live,case):
    e,b=live; approve(e)
    if case=="stale": b.quote_time=NOW-timedelta(seconds=6)
    if case=="outage": b.snapshot_error=ConnectionError("offline")
    if case=="auth": b.snapshot_error=PermissionError("expired authentication")
    original=b.snapshot
    async def altered(*args):
        s=await original(*args)
        if case=="mismatch": s["positions"]=[dict(symbol="SPY",quantity=".1",shares_available_for_sells=".1")]
        if case=="short": s["positions"]=[dict(symbol="SPY",quantity="-.1",shares_available_for_sells="0")]
        if case=="margin": s["account"]["type"]="margin"
        if case=="crypto": s["portfolio"]["crypto_value"]="1"
        return s
    b.snapshot=altered
    tick(e,b)
    assert not b.submissions and not e.store.rows()


def test_duplicate_live_order_prevention(live):
    e,b=live; approve(e); tick(e,b)
    row=e.store.db.execute("SELECT * FROM intents").fetchone()
    with pytest.raises(Unsafe): asyncio.run(e.dispatch(b,row["intent_key"],json.loads(row["payload"])))
    assert len(b.submissions)==1


def test_ambiguous_acceptance_never_retried(live):
    e,b=live; approve(e); b.ambiguous=True; tick(e,b)
    assert len(b.submissions)==1 and not e.store.get("position")
    assert e.store.unresolved()[0]["status"]=="AMBIGUOUS"
    b.ambiguous=False
    tick(e,b)
    assert len(b.submissions)==1 and "manual reconciliation" in e.store.get("gate")["reason"]


def test_restart_known_live_position_exits(live):
    e,b=live; approve(e); tick(e,b)
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(restarted,b)
    assert len(b.submissions)==2 and b.submissions[-1]["side"]=="sell"
    assert not e.store.get("position")
    tick(restarted,b)
    assert len(b.submissions)==2


def test_crash_in_dispatch_no_id_stays_ambiguous(live):
    e,b=live; approve(e)
    e.store.intent("crash","LIVE",dict(symbol="SPY",side="buy"))
    with e.store.db: e.store.db.execute("UPDATE intents SET status='DISPATCHING'")
    recovered=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(recovered,b)
    assert not b.submissions and e.store.unresolved()[0]["status"]=="AMBIGUOUS"


def test_kill_controls_and_exit_continues(live):
    e,b=live; approve(e); tick(e,b)
    atomic_text(e.policy.runtime/"ENTRIES_DISABLED","operator")
    tick(e,b)
    assert len(b.submissions)==1 and e.store.get("position")
    atomic_text(e.policy.runtime/"LIQUIDATE","operator")
    tick(e,b)
    assert len(b.submissions)==2 and not e.store.get("position")


def test_absolute_exposure_observation_triggers_exit(live):
    e,b=live; approve(e); tick(e,b)
    assert e.store.get("position")
    b.bid=D("201"); b.ask=D("201.01")
    tick(e,b)
    assert len(b.submissions)==2 and b.submissions[-1]["side"]=="sell"
    assert (e.policy.runtime/"ENTRIES_DISABLED").exists()
    assert not e.store.get("position")


def test_end_of_day_liquidation(live):
    e,b=live; approve(e); tick(e,b)
    b.time=e.policy.session.close-timedelta(minutes=15)
    tick(e,b)
    assert len(b.submissions)==2 and not e.store.get("position")


@pytest.mark.parametrize("partial",[False,True])
def test_working_entry_cancel_is_once_then_verified(live,partial):
    e,b=live; approve(e); b.pending=not partial; b.partial=partial; tick(e,b)
    assert e.store.unresolved()
    b.time+=timedelta(seconds=31)
    tick(e,b)
    assert len(b.cancels)==1
    b.pending=b.partial=False
    tick(e,b)
    assert len(b.cancels)==1 and not e.store.get("position")
    assert len(b.submissions)==(2 if partial else 1)


def roundtrip(e,key,sell_price):
    # Synthetic executions; no call to any order-submission transport.
    for side,price in (("buy",D("50")),("sell",D(sell_price))):
        oid=f"{key}-{side}"
        payload=dict(symbol="SPY",side=side,quantity=".1",stop="49.5",target="51",setup_id=key)
        e.store.intent(oid,"LIVE",payload)
        with e.store.db: e.store.db.execute("UPDATE intents SET broker_id=? WHERE intent_key=?",(oid,oid))
        order=dict(id=oid,symbol="SPY",side=side,state="filled",quantity=".1",cumulative_quantity=".1",type="market",market_hours="regular_hours",
                   executions=[dict(id="fill-"+oid,quantity=".1",price=str(price),fees="0",timestamp=iso(NOW))])
        e.ingest(oid,order)
        e.store.save("accounted:"+oid,True)


def test_daily_50_cent_latch_profits_do_not_replenish(live):
    e,b=live; approve(e)
    roundtrip(e,"loss1","47.5")
    assert D(e.daily(e.policy.day)["gross_losses"])==D(".25")
    roundtrip(e,"win","55")
    assert D(e.daily(e.policy.day)["gross_losses"])==D(".25")
    roundtrip(e,"loss2","47.5")
    d=e.daily(e.policy.day)
    assert D(d["gross_losses"])==D(".50") and D(d["net"])==0 and d["latched"]
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(restarted,b)
    assert not b.submissions and e.daily(e.policy.day)["latched"]


def test_profit_cannot_expand_allocated_capital(live):
    e,b=live; approve(e); roundtrip(e,"bigwin","70")
    assert D(e.store.get("cash"))==12
    tick(e,b)
    assert len(b.submissions)==1 and D(b.submissions[0]["dollar_amount"]) <= 9


@pytest.mark.parametrize("amount",["10","10.01","100"])
def test_oversize_order_rejected_at_boundary(live,amount):
    e,b=live; approve(e)
    with pytest.raises(Unsafe): e.policy.validate_order_shape(dict(account_number=ACCOUNT,symbol="SPY",side="buy",dollar_amount=amount,type="market",market_hours="regular_hours",time_in_force="gfd",ref_id="x"))
    assert not b.submissions


@pytest.mark.parametrize("change",[{"symbol":"BTC"},{"symbol":"SPY_CALL"},{"side":"sell_short"},{"market_hours":"all_day_hours"},{"leverage":"2"},{"type":"option"}])
def test_forbidden_order_types_cannot_reach_transport(live,change):
    e,b=live; approve(e)
    args=dict(account_number=ACCOUNT,symbol="SPY",side="buy",dollar_amount="5",type="market",market_hours="regular_hours",time_in_force="gfd",ref_id="x")
    args.update(change)
    with pytest.raises(Unsafe): e.write_guard(args)
    assert not b.submissions


def test_unbound_transport_remains_read_only():
    with pytest.raises(Unsafe): asyncio.run(Robinhood(None).submit({}))
    with pytest.raises(Unsafe): asyncio.run(Robinhood(None).cancel_owned({}))

def test_planned_risk_boundary_rejects_over_ten_cents(live):
    e,b=live; approve(e); tick(e,b)
    row=e.store.db.execute("SELECT * FROM intents").fetchone()
    payload=json.loads(row["payload"])
    assert D(payload["planned_risk"])<=D(".10")
    with e.store.db:
        payload["planned_risk"]="0.100001"
        e.store.db.execute("UPDATE intents SET status='DISPATCHING',payload=? WHERE ref_id=?",(json.dumps(payload),row["ref_id"]))
    args=dict(payload["args"],ref_id=row["ref_id"])
    e.proof=dict(args=args,at=iso(b.time))
    e.store.save("position",None)
    e.store.save("cash","10")
    e.store.save("broker_snapshot",snapshot(b.time))
    with pytest.raises(Unsafe,match="planned risk"):
        e.write_guard(args)
    assert len(b.submissions)==1


def test_known_id_query_failure_recovers_without_duplicate_buy(live):
    e,b=live; approve(e)
    original=b.order
    calls=0
    async def unavailable_once(*args):
        nonlocal calls
        calls+=1
        if calls==1: raise ConnectionError("Query after acknowledgement failed")
        return await original(*args)
    b.order=unavailable_once
    tick(e,b)
    assert len(b.submissions)==1 and e.store.unresolved()[0]["broker_id"]
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    tick(restarted,b)
    assert [x["side"] for x in b.submissions]==["buy","sell"]
    assert not e.store.get("position")


def test_stop_process_gate_disables_even_exits(live):
    e,b=live; approve(e); tick(e,b)
    atomic_text(e.policy.runtime/"STOP","operator stop")
    atomic_text(e.policy.runtime/"LIQUIDATE","request")
    tick(e,b)
    assert len(b.submissions)==1 and e.store.get("position")


def test_real_process_crash_after_fake_acceptance(live):
    import subprocess,sys
    e,b=live
    tests=Path(__file__).resolve().parent
    program="""
import os,sys,json,asyncio
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from test_live import Broker,approve,tick
from trader.live import LiveEngine
from trader.models import Config
from trader.store import Store
from dataclasses import replace
r=Path(sys.argv[1]); s=Store(r/'state.sqlite3'); b=Broker()
e=LiveEngine(s,r,replace(Config(),universe=('SPY',)),lambda:b.time)
e.account='test0000'; s.save('account_number',e.account); approve(e)
submit=b.submit
async def crash(args):
    await submit(args)
    (r/'fake-accepted.json').write_text(json.dumps(b.orders))
    os._exit(27)
b.submit=crash
tick(e,b)
"""
    p=subprocess.run([sys.executable,"-c",program,str(e.root),str(tests)])
    assert p.returncode==27 and (e.root/"fake-accepted.json").exists()
    restarted=LiveEngine(e.store,e.root,e.cfg,lambda:b.time)
    assert e.store.unresolved()[0]["status"]=="AMBIGUOUS"
    tick(restarted,b)
    assert not b.submissions
