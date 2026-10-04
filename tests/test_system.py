import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import subprocess
import sys
import pytest
from trader.broker import Robinhood, READ_TOOLS
from trader.calendar import Calendar
from trader.engine import Engine
from trader.execution import verify_actual_order, submit_live
from trader.models import Config, D, Mode, Unsafe, dt, iso
from trader.risk import reconcile, size
from trader.store import Store, process_lock
from trader.strategy import evaluate

NOW = dt("2026-09-23T13:55:01Z")
CFG = replace(Config(), universe=("SPY",))
ACCOUNT = "test0000"


def snapshot(now=NOW):
    return dict(observed_at=iso(now), account=dict(account_number=ACCOUNT, agentic_allowed=True, nickname="Agentic", state="active", type="limited_margin"),
                portfolio=dict(total_value="100", cash="100", currency="USD", buying_power=dict(buying_power="100", unleveraged_buying_power="100", display_currency="USD"),
                               options_value="0", crypto_value="0", futures_value="0", event_contracts_value="0", fixed_income_value="0", mutual_funds_value="0"),
                positions=[], orders=[])


def quote(now=NOW, bid="101.005", ask="101.01"):
    return dict(symbol="SPY", state="active", has_traded=True, bid_price=bid, ask_price=ask, venue_bid_time=iso(now), venue_ask_time=iso(now))


def bars():
    start = dt("2026-09-23T13:30:00Z")
    prices = [("100", "99", "99.5")]*3 + [("102.5", "99.9", "102"), ("101", "99.9", "100.5")]
    return [dict(begins_at=iso(start+timedelta(minutes=5*i)), high_price=h, low_price=l, close_price=c, open_price=l, volume=1_000_000, session="reg") for i, (h,l,c) in enumerate(prices)]


def tradability():
    return {"SPY": dict(tradeable=True, state="active", country="US", fractional_tradability="tradable", account_type_tradabilities=[dict(account_type="individual", account_type_tradability="tradable")])}


class FakeBroker:
    def __init__(self):
        self.time = NOW
        self.reviews = 0
        self.alert = {}
        self.q = quote()
        self.snap = snapshot()

    def now(self):
        return self.time

    async def review(self, *args):
        self.reviews += 1
        return dict(symbol="SPY", side="buy", type="market", order_checks=self.alert)

    async def snapshot(self, *_):
        return self.snap

    async def quotes(self, *_):
        return {"SPY": self.q}

    async def tradability(self, *_):
        return tradability()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "runtime" / "PAPER"
    store = Store(root / "state.sqlite3")
    engine = Engine(store, root, config=CFG)
    engine.account = ACCOUNT
    store.save("account_number", ACCOUNT)
    yield engine, FakeBroker(), Calendar().session(NOW)
    store.close()


def tick(engine, broker, session, **kwargs):
    args = dict(now=NOW, session=session, snapshot=snapshot(), quotes={"SPY": quote()}, bars={"SPY": bars()}, tradability=tradability())
    args.update(kwargs)
    asyncio.run(engine.tick(broker, **args))


def test_strategy_calculates_complete_setup():
    s = evaluate("SPY", bars(), Calendar().session(NOW), NOW)
    assert s["state"] == "ARMED"
    assert s["opening_high"] == "100" and s["opening_low"] == "99"
    assert D(s["trigger"]) == D("101.01")
    assert D(s["invalidation"]) == D("99.89")
    assert s["pullback_high"] and s["pullback_low"] and s["vwap"] and s["target"] and s["time_exit"]


def test_no_lookahead_missing_and_interpolated_bars():
    session = Calendar().session(NOW)
    assert evaluate("SPY", bars(), session, NOW-timedelta(minutes=3))["state"] == "WAIT_PULLBACK"
    with pytest.raises(Unsafe):
        evaluate("SPY", bars()[1:], session, NOW)
    b = bars()
    b[0]["interpolated"] = True
    with pytest.raises(Unsafe):
        evaluate("SPY", b, session, NOW)


def test_paper_entry_caps_and_persistence(setup):
    e,b,s = setup
    tick(e,b,s)
    p = e.store.get("position")
    assert p is not None
    assert D(p["quantity"])*D(p["entry"]) <= 20
    assert D(e.store.get("cash")) >= 20
    assert D(p["quantity"])*(D(p["entry"])-D(p["stop"])+D(p["entry"])/10000)+D(".02") <= D(".25")
    assert len(e.store.rows()) == 1 and b.reviews == 1
    assert e.store.get("candidate:SPY")["state"] == "ARMED"
    tick(e,b,s)
    assert len(e.store.rows()) == 1


def test_duplicate_intent_is_idempotent(setup):
    e,b,s = setup
    tick(e,b,s)
    row = e.store.db.execute("SELECT * FROM intents").fetchone()
    payload = json.loads(row["payload"])
    cash = e.store.get("cash")
    assert e.paper_fill(row["intent_key"], payload, NOW) is False
    assert e.store.get("cash") == cash and len(e.store.rows()) == 1
    payload["quantity"] = "99"
    with pytest.raises(Unsafe):
        e.paper_fill(row["intent_key"], payload, NOW)


def test_restart_recovers_position_and_exits_once(setup):
    e,b,s = setup
    tick(e,b,s)
    restarted = Engine(e.store, e.root, config=CFG)
    assert restarted.store.get("recovery_exit")
    tick(restarted,b,s)
    assert restarted.store.get("position") is None
    assert len(restarted.store.rows()) == 2
    tick(restarted,b,s)
    assert len(restarted.store.rows()) == 2


def test_prepared_paper_intent_aborted_on_restart(setup):
    e,b,s = setup
    e.store.intent("uncommitted", "PAPER", {"symbol": "SPY"})
    Engine(e.store, e.root, config=CFG)
    assert not e.store.unresolved()
    assert e.store.db.execute("SELECT status FROM intents").fetchone()[0] == "ABORTED"
    assert e.store.get("cash") == "100" and not e.store.rows()


@pytest.mark.parametrize("case", ["stale_quote", "future_quote", "stale_account", "unreachable", "mismatch", "ambiguous_order", "wide_spread", "missing_quote", "not_fractional", "low_power", "borrow_capable", "alert", "stale_after_review", "late_after_review"])
def test_fail_closed(setup, case):
    e,b,s = setup
    snap, q, t = snapshot(), quote(), tradability()
    if case == "stale_quote": q = quote(NOW-timedelta(seconds=6))
    if case == "future_quote": q = quote(NOW+timedelta(seconds=1))
    if case == "stale_account": snap["observed_at"] = iso(NOW-timedelta(seconds=16))
    if case == "unreachable": snap = None
    if case == "mismatch": snap["positions"] = [dict(symbol="SPY", quantity="1")]
    if case == "ambiguous_order": snap["orders"] = [{"state": "unconfirmed"}]
    if case == "wide_spread": q["bid_price"] = "100"
    if case == "not_fractional": t["SPY"]["fractional_tradability"] = "untradable"
    if case == "low_power": snap["portfolio"]["buying_power"]["unleveraged_buying_power"] = "20"
    if case == "borrow_capable": snap["account"]["type"] = "margin"
    if case == "alert": b.alert = {"alert_type": "TEST_RESTRICTION"}
    if case == "stale_after_review": b.q = quote(NOW-timedelta(seconds=6))
    if case == "late_after_review": b.time = s.close-timedelta(minutes=59)
    tick(e,b,s,snapshot=snap,quotes={} if case == "missing_quote" else {"SPY":q}, tradability=t)
    assert not e.store.rows() and e.store.get("position") is None


def test_ambiguous_intent_persists_across_restart(setup):
    e,b,s = setup
    row = e.store.intent("unknown", "LIVE", {"symbol": "SPY", "side": "buy", "quantity": "0.1"})
    with e.store.db:
        e.store.db.execute("UPDATE intents SET status='AMBIGUOUS'")
    Engine(e.store,e.root,config=CFG)
    tick(e,b,s)
    assert e.store.unresolved() and not e.store.rows()


def test_daily_loss_shutdown_survives_restart(setup):
    e,b,s = setup
    daily = e.daily(s.day)
    daily.update(net="-4", gross_losses="5", latched=True)
    e.store.save("daily:"+s.day, daily)
    restarted = Engine(e.store,e.root,config=CFG)
    tick(restarted,b,s)
    assert not e.store.rows()
    assert not restarted.daily("2026-09-24")["latched"]


def test_realized_loss_latches_after_exit(setup):
    e,b,s = setup
    tick(e,b,s)
    day = e.daily(s.day)
    day.update(gross_losses="4.99", net="-4.99")
    e.store.save("daily:"+s.day, day)
    tick(e,b,s,quotes={"SPY":quote(bid="99",ask="99.01")})
    assert e.store.get("position") is None
    assert e.daily(s.day)["latched"]
    assert D(e.daily(s.day)["gross_losses"]) >= 5


def test_kill_blocks_entries_and_allows_paper_exit(setup):
    e,b,s = setup
    e.kill.write_text("test")
    tick(e,b,s)
    assert not e.store.rows()
    e.kill.unlink()
    tick(e,b,s)
    assert e.store.get("position")
    e.kill.write_text("test")
    tick(e,b,s)
    assert not e.store.get("position") and len(e.store.rows()) == 2


def test_stale_exit_keeps_position_no_invented_fill(setup):
    e,b,s = setup
    tick(e,b,s)
    e.kill.write_text("test")
    tick(e,b,s,quotes={"SPY":quote(NOW-timedelta(seconds=8))})
    assert e.store.get("position") and len(e.store.rows()) == 1


def test_exit_at_early_close_and_holidays():
    cal = Calendar()
    assert cal.session(dt("2026-12-25T16:00:00Z")) is None
    assert cal.session(dt("2026-09-26T16:00:00Z")) is None
    s = cal.session(dt("2026-11-27T16:00:00Z"))
    assert s.close == dt("2026-11-27T18:00:00Z")
    assert not s.entry_window(dt("2026-11-27T17:01:00Z"))
    assert cal.session(dt("2026-03-09T15:00:00Z")).open.hour == 13
    assert cal.session(dt("2026-03-06T15:00:00Z")).open.hour == 14


def test_read_only_and_live_lock(setup):
    e,b,s = setup
    e.mode = Mode.READ_ONLY
    tick(e,b,s)
    assert b.reviews == 0 and not e.store.rows()
    with pytest.raises(Unsafe): Engine(e.store,e.root,Mode.LIVE)
    with pytest.raises(Unsafe): asyncio.run(submit_live())
    assert not any(name.startswith(("place", "cancel", "replace")) for name in READ_TOOLS)
    with pytest.raises(Unsafe): asyncio.run(Robinhood(None).call("place_equity_order", {}))


def test_actual_fill_verification_partial_and_duplicate(setup):
    e,_,_ = setup
    row = e.store.intent("live-test", "LIVE", dict(symbol="SPY",side="buy",quantity="1"))
    with e.store.db:
        e.store.db.execute("UPDATE intents SET broker_id='broker1' WHERE intent_key='live-test'")
    execution = dict(id="fill1",quantity="0.5",price="100",fees="0",timestamp=iso(NOW))
    order = dict(id="broker1",symbol="SPY",side="buy",state="partially_filled",quantity="1",cumulative_quantity="0.5",executions=[execution])
    assert verify_actual_order(e.store,"live-test",order) == "PARTIAL"
    verify_actual_order(e.store,"live-test",order)
    assert e.store.get("actual_execution:fill1")["ref_id"] == row["ref_id"]
    assert e.store.unresolved()
    order["state"] = "filled"
    with pytest.raises(Unsafe): verify_actual_order(e.store,"live-test",order)
    assert e.store.unresolved()[0]["status"] == "AMBIGUOUS"


def test_single_writer_lock(tmp_path):
    with process_lock(tmp_path/"lock"):
        with pytest.raises(Unsafe):
            with process_lock(tmp_path/"lock"):
                pass


def test_rollback_of_fill_commit(setup):
    e,b,s = setup
    e.store.db.execute("CREATE TRIGGER break_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
    with pytest.raises(Exception): tick(e,b,s)
    assert e.store.get("cash") == "100" and e.store.get("position") is None
    assert len(e.store.rows()) == 0
    Engine(e.store,e.root,config=CFG)
    assert not e.store.unresolved()


def test_cli_live_rejected_without_auth(tmp_path):
    p = subprocess.run([sys.executable,"-m","trader","--root",str(tmp_path),"enable-live","--date","2026-09-24"],capture_output=True,text=True)
    assert p.returncode != 0
    assert not (tmp_path/"runtime"/"live-approval.json").exists()


def test_csv_regeneration_no_duplicates(setup):
    e,b,s = setup
    tick(e,b,s)
    e.store.export_csv(e.root/"trades.csv")
    e.store.export_csv(e.root/"trades.csv")
    assert len((e.root/"trades.csv").read_text().splitlines()) == 2


def test_corrupt_local_position_blocks_entry_and_exit(setup):
    e,b,s = setup
    e.store.save("cash", "200")
    tick(e,b,s)
    assert not e.store.rows()
    assert "mismatch" in e.store.get("gate")["reason"]


def test_crash_after_commit_keeps_position(tmp_path):
    root = tmp_path/"runtime"/"PAPER"
    program = """
import os, sys
from pathlib import Path
from trader.store import Store
from trader.engine import Engine
from trader.models import dt
p=Path(sys.argv[1]); s=Store(p/'state.sqlite3'); e=Engine(s,p)
e.paper_fill('crash-entry',dict(symbol='SPY',side='buy',quantity='0.1',price='100',stop='99',target='102',setup_id='crash'),dt('2026-09-23T14:00:00Z'))
os._exit(23)
"""
    result = subprocess.run([sys.executable,"-c",program,str(root)])
    assert result.returncode == 23
    store = Store(root/"state.sqlite3")
    e = Engine(store,root)
    assert store.get("recovery_exit") and store.get("position")["quantity"] == "0.1"
    assert D(store.get("cash")) == D("89.99")
    e.reconcile_paper_ledger()
    assert len(store.rows()) == 1
    store.close()


def test_crash_before_commit_rolls_back(tmp_path):
    root = tmp_path/"runtime"/"PAPER"
    program = """
import os, sys
from pathlib import Path
from trader.store import Store
from trader.engine import Engine
p=Path(sys.argv[1]); s=Store(p/'state.sqlite3'); e=Engine(s,p)
s.intent('pending','PAPER',{'symbol':'SPY'})
s.db.execute('BEGIN IMMEDIATE')
s.put('cash','0')
os._exit(24)
"""
    result = subprocess.run([sys.executable,"-c",program,str(root)])
    assert result.returncode == 24
    store = Store(root/"state.sqlite3")
    e = Engine(store,root)
    assert store.get("cash") == "100" and not store.unresolved()
    e.reconcile_paper_ledger()
    store.close()


def test_pagination_complete_and_cycles_rejected():
    class Paged(Robinhood):
        def __init__(self): self.calls=0
        async def call(self,name,args):
            self.calls+=1
            if self.calls==1: return {"positions":[{"symbol":"SPY"}],"next":"https://example.invalid/?cursor=abc"}
            assert args["cursor"] == "abc"
            return {"positions":[{"symbol":"AAPL"}]}
    broker=Paged()
    assert len(asyncio.run(broker.pages("get_equity_positions","positions",ACCOUNT)))==2


def test_position_size_near_loss_limit(setup):
    e,b,s=setup
    c=evaluate("SPY",bars(),s,NOW)
    day=e.daily(s.day)
    day["gross_losses"]="4.90"
    qty,price,_=size(c,quote(),D(100),D(100),day,NOW,CFG)
    assert qty*(price-D(c["invalidation"])+price/10000)+D(".02") <= D(".10")


def test_target_stop_time_and_close_exits(setup):
    e,b,s=setup
    tick(e,b,s)
    p=e.store.get("position")
    target=D(p["target"])
    tick(e,b,s,quotes={"SPY":quote(bid=str(target),ask=str(target+D(".01")))})
    assert e.store.get("position") is None
    assert D(e.store.rows()[-1]["realized_pnl"]) > 0


def test_no_afterhours_fills_even_when_killed(setup):
    e,b,s=setup
    tick(e,b,s)
    e.kill.write_text("stop")
    after=s.close+timedelta(seconds=1)
    tick(e,b,s,now=after,quotes={"SPY":quote(after)})
    assert len(e.store.rows())==1 and e.store.get("position")


def test_time_exit_and_session_liquidation(setup):
    e,b,s=setup
    tick(e,b,s)
    later=NOW+timedelta(minutes=31)
    tick(e,b,s,now=later,quotes={"SPY":quote(later)})
    assert e.store.get("position") is None
    assert e.store.rows()[-1]["stop_condition"] == "TIME"


def test_exit_near_session_close(setup):
    e,b,s=setup
    tick(e,b,s)
    later=s.close-timedelta(minutes=14)
    tick(e,b,s,now=later,quotes={"SPY":quote(later)})
    assert e.store.get("position") is None
    assert e.store.rows()[-1]["stop_condition"] == "SESSION_CLOSE"


def test_live_fill_complete_status_is_verified(setup):
    e,_,_=setup
    e.store.intent("verified", "LIVE", dict(symbol="SPY",side="sell",quantity="0.5"))
    with e.store.db:
        e.store.db.execute("UPDATE intents SET broker_id='b2' WHERE intent_key='verified'")
    order=dict(id="b2",symbol="SPY",side="sell",state="filled",quantity="0.5",cumulative_quantity="0.5",executions=[dict(id="x",quantity="0.5",price="100",fees=".01",timestamp=iso(NOW))])
    assert verify_actual_order(e.store,"verified",order)=="FILLED"
    assert not e.store.unresolved()


@pytest.mark.parametrize('failure',['crash','stall'])
def test_supervisor_restarts_crashed_child_and_latches_kill(tmp_path,failure):
    import os
    import signal
    import time
    command=[sys.executable,"-m","trader","--root",str(tmp_path),"supervise","--mode","PAPER"]
    supervisor=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    heartbeat=tmp_path/"runtime"/"heartbeat.json"
    def wait_for(predicate,seconds=15):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=json.loads(heartbeat.read_text())
                if predicate(value): return value
            except (FileNotFoundError,ValueError): pass
            time.sleep(.1)
        pytest.fail("Supervisor did not reach expected state")
    try:
        first=wait_for(lambda x:x["state"] in ("DISCONNECTED","BACKOFF"))
        os.kill(first["pid"],signal.SIGKILL if failure=='crash' else signal.SIGSTOP)
        second=wait_for(lambda x:x["pid"]!=first["pid"] and x["state"] in ("DISCONNECTED","BACKOFF"),seconds=65)
        assert second["pid"]!=first["pid"]
        assert (tmp_path/"runtime"/"KILL").exists()
    finally:
        supervisor.terminate()
        try: supervisor.wait(timeout=15)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait()
