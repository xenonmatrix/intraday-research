"""Canonical configuration and hardening regressions. Temporary roots/fake broker only."""
import asyncio,json,sqlite3
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import pytest
from trader import configuration as C
from trader.models import D,Unsafe,dt
from trader.config_cli import configure,init
from trader.persistent.db import Ledger
from trader.calendar import Calendar
from trader.doctor import doctor,transition
from trader.host_health import problems
from test_persistent import env,tick,raw,eligible_symbols,Broker,host,NOW
from test_system import ACCOUNT

@pytest.mark.parametrize('section',list(C.DEFAULTS))
def test_all_default_sections_round_trip(section,tmp_path):
    c=C.validate({});C.write(tmp_path,c);assert C.load(tmp_path)[section]==c[section]

def test_example_is_valid_and_private_identity_absent():
    c=C.validate(json.loads(Path('config/trader.example.json').read_text()))
    assert c==C.validate({}) and c['trading']['default_mode']=='READ_ONLY' and c['broker']['account_suffix']==''

@pytest.mark.parametrize('key,value',[
 ('trading.capital_per_trade','-1'),('trading.capital_per_trade','NaN'),('trading.capital_per_trade','Infinity'),('trading.capital_per_trade',True),('trading.capital_per_trade','31'),('trading.max_total_exposure','0'),('trading.max_open_positions',0),('trading.max_open_positions',True),('trading.max_entry_attempts_per_session',-1),('trading.buying_power_reserve','-2'),('trading.buying_power_reserve','19'),('trading.symbols',[]),('trading.symbols',['SPY','SPY']),('trading.symbols',['TSLA']),('trading.default_mode','LIVE'),('trading.one_position_per_symbol',False),
 ('risk.max_loss_per_trade_percent','0'),('risk.max_loss_per_trade_percent','11'),('risk.max_daily_loss_percent','26'),('risk.max_daily_loss_percent','-1'),('risk.profits_replenish_allowance',True),('risk.daily_loss_base','account_balance'),
 ('host.min_battery_percent',101),('host.min_battery_percent',-1),('host.min_battery_percent','50'),('host.require_ac_power','false'),('host.poll_seconds',15),
 ('watchdog.heartbeat_interval_seconds',10),('watchdog.heartbeat_stale_seconds',5),('watchdog.restart_after_seconds',15),('watchdog.startup_timeout_seconds',20),('watchdog.restart_limit',0),('watchdog.check_interval_seconds',15),
 ('connectivity.poll_seconds',-1),('connectivity.poll_seconds',20),('connectivity.request_timeout_seconds',15),('connectivity.connect_timeout_seconds',40),('connectivity.account_cache_seconds',15),('connectivity.keepalive_expiry_seconds',30),('connectivity.backoff_initial_seconds',40),('connectivity.recovery_observations',1),('connectivity.recovery_min_seconds',1),('connectivity.stream_read_timeout_seconds',5),
 ('clock.max_combined_bound_ms','NaN'),('clock.max_combined_bound_ms','0'),('clock.max_combined_bound_ms','1e999'),('clock.max_combined_bound_ms','1e-999'),('clock.sample_count',2),('clock.time_sources',['unknown.example']),('clock.health_mode','ignore'),('clock.sample_max_age_seconds',60),
 ('market_data.max_quote_age_seconds',-1),('market_data.max_spread_percent','2'),('market_data.future_timestamp_tolerance_seconds',1),
 ('strategy.retrospective_entries',True),('strategy.bar_interval_minutes',1),('strategy.opening_range_minutes',20),('strategy.max_hold_minutes',180),
 ('session.entry_cutoff_minutes_before_close',15),('session.entry_cutoff_minutes_before_close',180),('session.liquidation_minutes_before_close',0),
 ('execution.review_required',False),('execution.order_type','option'),('execution.allow_fractional',False),
 ('analysis.export_directory','../private'),('analysis.export_directory','/tmp'),('analysis.features_only',False),('broker.provider','crypto'),('broker.account_suffix','abcdef'),('logging.level','noisy')])
def test_invalid_values_fail_specific(key,value):
    raw=deepcopy(C.DEFAULTS);section,field=key.split('.');raw[section][field]=value
    with pytest.raises(Unsafe) as e:C.validate(raw)
    assert str(e.value)

@pytest.mark.parametrize('raw',[[],{'unknown':{}},{'risk':{'unknown':1}},{'host':[]}])
def test_unknown_sections_keys_shapes(raw):
    with pytest.raises(Unsafe):C.validate(raw)

@pytest.mark.parametrize('text',['{"risk":{},"risk":{}}','{"risk":{"max_daily_loss_percent":NaN}}','{"host":'])
def test_malformed_json(tmp_path,text):
    C.path(tmp_path).parent.mkdir();C.path(tmp_path).write_text(text)
    with pytest.raises(Unsafe):C.load(tmp_path)

@pytest.mark.parametrize('capital,expected',[('10','.15'),('25','.375'),('50','.75'),('100','1.5')])
def test_percentage_derivation(capital,expected,tmp_path):
    c=C.validate({'trading':{'capital_per_trade':capital,'max_total_exposure':'100'}})
    C.write(tmp_path,c);db=Ledger(tmp_path/'runtime/persistent.sqlite3');C.apply(tmp_path,db,dt('2026-10-03T12:00:00Z'))
    assert db.settings().trade_loss==D(expected) and db.settings().daily_loss==5;db.close()

def test_fingerprint_canonical_and_excludes_identity():
    a=C.validate({});b=C.validate({'trading':{'capital_per_trade':'10.00'},'broker':{'account_suffix':'1234'}})
    assert C.fingerprint(a)==C.fingerprint(b)
    b['risk']['max_daily_loss_percent']='4';assert C.fingerprint(a)!=C.fingerprint(b)
    assert C.strategy_fingerprint(a)==C.strategy_fingerprint(b)
    b['strategy']['target_r']='3';assert C.strategy_fingerprint(a)!=C.strategy_fingerprint(b)

def test_clock_changes_warn_not_silent():
    c=C.validate({'clock':{'max_combined_bound_ms':'100'}})
    assert any('SAFETY CRITICAL' in w and 'clock' in w for w in C.warnings(c))
    assert C.DEFAULTS['clock']['max_combined_bound_ms']=='50'

@pytest.mark.parametrize('breakout,stop',[('0.01','0.01'),('0.03','0.02')])
def test_orb_uses_canonical_offsets_for_range_and_missed_signal(breakout,stop):
    from trader.orb import evaluate
    from trader.models import iso
    config=C.validate({'strategy':{'breakout_offset':breakout,'stop_offset':stop}})
    cfg=C.market_config(config);session=Calendar(config).session(NOW)
    bars=raw(NOW)['SPY']
    candidate=evaluate('SPY',bars,session,NOW,cfg)
    trigger=D(candidate['opening_high'])+D(breakout)
    invalidation=D(candidate['opening_low'])-D(stop)
    assert D(candidate['trigger'])==trigger
    assert D(candidate['invalidation'])==invalidation
    assert D(candidate['target'])==trigger+cfg.target_r*(trigger-invalidation)
    # An actual completed-bar crossing is still missed, never replayed.
    crossed=dict(bars[-1],begins_at=iso(session.open+timedelta(minutes=15)),
                 high_price=str(trigger),close_price=str(trigger))
    assert evaluate('SPY',bars+[crossed],session,session.open+timedelta(minutes=20),cfg)['state']=='MISSED'

def test_cli_same_file_derived_and_no_account_output(tmp_path,capsys):
    configure(tmp_path,'set-capital','25');configure(tmp_path,'set','1234','broker.account_suffix');configure(tmp_path,'show')
    assert C.load(tmp_path)['trading']['capital_per_trade']=='25'
    assert '1234' not in capsys.readouterr().out
    assert C.path(tmp_path).stat().st_mode&0o077==0
    original=C.path(tmp_path).read_bytes();configure(tmp_path,'reset',dry_run=True);assert C.path(tmp_path).read_bytes()==original
    with pytest.raises(Unsafe):configure(tmp_path,'reset')

def test_missing_configuration_fails_safe(tmp_path):
    with pytest.raises(Unsafe):C.load(tmp_path)
    result=asyncio.run(doctor(tmp_path));assert not result['ready'] and 'configuration' in result['checks']

def test_apply_does_not_reset_daily_latch_or_attempts(tmp_path):
    C.write(tmp_path,C.validate({}));db=Ledger(tmp_path/'runtime/persistent.sqlite3');now=dt('2026-10-03T12:00:00Z')
    with db.tx():
        s=db.ensure_session('2026-10-03');s.update(loss_latched=True,attempts=5,gross_losses='1.5');db.save_session('2026-10-03',s)
    C.apply(tmp_path,db,now);s=db.session('2026-10-03');assert s['loss_latched'] and s['attempts']==5 and s['gross_losses']=='1.5';db.close()

def test_strategy_apply_blocked_during_session(tmp_path):
    c=C.validate({'strategy':{'target_r':'3'}});C.write(tmp_path,c);db=Ledger(tmp_path/'runtime/persistent.sqlite3')
    with pytest.raises(Unsafe,match='during a market session'):C.apply(tmp_path,db,NOW)
    assert db.get('production_config') is None;db.close()

def test_pending_configuration_blocks_entries_preserves_exits(env):
    e,b,db=env;tick(e,b);assert len(db.trades())==2
    # Pinned active snapshot must continue to manage existing holdings even if
    # the desired on-disk configuration becomes malformed.
    c=C.validate({});db.put('production_config',c);C.path(e.root).parent.mkdir();C.path(e.root).write_text('{bad')
    b.prices['SPY']=(D('98'),D('98.01'));tick(e,b)
    assert any(a['side']=='sell' and a['symbol']=='SPY' for a in b.calls)
    assert len([a for a in b.calls if a['side']=='buy'])==2

def test_config_locked_with_positions(env):
    e,b,db=env;tick(e,b);C.write(e.root,C.validate({}))
    with pytest.raises(Unsafe):configure(e.root,'set-capital','5')
    assert C.load(e.root)['trading']['capital_per_trade']=='10'

def test_trade_records_config_and_independent_exit_parameters(env):
    e,b,db=env
    c=C.validate({'strategy':{'target_r':'3','max_hold_minutes':20}});db.put('production_config',c);db.put('config_fingerprint',C.fingerprint(c));db.put('strategy_fingerprint',C.strategy_fingerprint(c));C.write(e.root,c)
    # Existing test host is deliberately a legacy mock. Pin policy in coordinator
    # without claiming a production host certificate.
    from trader.persistent.engine import Coordinator
    e=Coordinator(e.root,db,lambda:b.time);e.account=ACCOUNT;b.guard=e.guard
    # Test record mechanics via a risk-checked reservation fixture without writes.
    from trader.persistent.risk import proposal
    candidate={'symbol':'SPY','setup_id':'fixture','trigger':'100.01','invalidation':'99','target':'103.04','session_dollars':'100000000','state':'ARMED'}
    from test_system import quote
    p=proposal(db.settings(),db.session('2026-09-28'),candidate,quote(b.time,bid='100.005',ask='100.01'),D(100),b.time,cfg=C.market_config(c))
    oid=db.reserve('2026-09-28','SPY','test',p,b.time,lambda *_:None)
    t=db.trade(db.order(oid)['trade_id']);assert t['target_r']=='3' and t['max_hold_minutes']==20 and t['config_fingerprint']==C.fingerprint(c)
    assert 'account_suffix' not in t['config_snapshot']['broker']

@pytest.mark.parametrize('day,closed',[('2026-10-03',True),('2026-12-25',True),('2026-11-27',False)])
def test_calendar_with_config_early_closes(day,closed):
    c=C.validate({'session':{'entry_cutoff_minutes_before_close':45,'liquidation_minutes_before_close':10}})
    s=Calendar(c).session(dt(day+'T15:00:00Z'))
    if closed:assert s is None
    else:
        assert s.close.hour==18 and s.entry_cutoff_minutes==45 and s.liquidation_minutes==10
        assert not s.entry_window(s.close-timedelta(minutes=45))

def test_doctor_never_calls_broker_without_flag(tmp_path,monkeypatch):
    from trader import doctor as module
    C.write(tmp_path,C.validate({}));(tmp_path/'runtime').mkdir(mode=0o700)
    def forbidden(*a,**k):raise AssertionError('Unexpected network')
    monkeypatch.setattr(module,'connect',forbidden)
    result=asyncio.run(module.doctor(tmp_path,False));assert not result['ready']

def test_failed_transition_changes_no_controls(tmp_path,monkeypatch):
    from trader import doctor as module
    r=tmp_path/'runtime';r.mkdir();[(r/k).write_text('test protection') for k in ('KILL','MAINTENANCE','READ_ONLY')]
    async def failed(*a):return {'ready_for_transition':False,'blockers':['clock failed']}
    monkeypatch.setattr(module,'doctor',failed)
    with pytest.raises(Unsafe,match='clock failed'):asyncio.run(module.transition(tmp_path,True,True,True))
    assert all((r/k).read_text()=='test protection' for k in ('KILL','MAINTENANCE','READ_ONLY'))

def test_first_run_migration_safe(tmp_path,capsys):
    from trader.persistent.cli import migrate
    migrate(tmp_path);c=C.load(tmp_path);db=Ledger(tmp_path/'runtime/persistent.sqlite3')
    assert not db.settings().live_enabled and (tmp_path/'runtime/READ_ONLY').exists() and c['trading']['default_mode']=='READ_ONLY';db.close()

def test_protection_removal_cannot_use_legacy_enable(tmp_path):
    from trader.persistent.cli import live_switch
    with pytest.raises(Unsafe,match='production activate'):live_switch(tmp_path,True)

def test_restart_requires_fresh_health_preserves_budget(env):
    e,b,db=env;daily=db.session('2026-09-28');daily.update(attempts=2,gross_losses='.2',net='-.2',outages=2);db.save_session('2026-09-28',daily)
    db.recover(b.time);after=db.session('2026-09-28')
    assert after['recovery_active'] and after['successes']==0 and not after['opening_healthy']
    assert after['attempts']==2 and after['gross_losses']=='.2' and after['outages']==2
    tick(e,b);assert not b.calls

def test_changed_policy_invalidates_opening_without_resetting_losses(tmp_path):
    now=dt('2026-10-01T14:00:00Z');C.write(tmp_path,C.validate({}));db=Ledger(tmp_path/'runtime/persistent.sqlite3');C.apply(tmp_path,db,now)
    with db.tx():
        daily=db.ensure_session('2026-10-01');daily.update(opening_healthy=True,gross_losses='.1');db.save_session('2026-10-01',daily)
    C.write(tmp_path,C.validate({'host':{'require_ac_power':True}}));C.apply(tmp_path,db,now)
    assert not db.session('2026-10-01')['opening_healthy'] and db.session('2026-10-01')['gross_losses']=='.1';db.close()

def test_custom_slippage_used_in_planned_risk():
    from trader.persistent.risk import proposal
    from trader.persistent.config import Settings
    from test_system import quote
    cfg=C.market_config(C.validate({'execution':{'slippage_bps':'2'}}))
    c=dict(symbol='SPY',setup_id='test',trigger='100.01',invalidation='99',target='102.03',session_dollars='100000000')
    p=proposal(Settings(),{'gross_losses':'0','loss_latched':False,'loss_budget':'1.5'},c,quote(NOW,bid='100',ask='100.01'),D(100),NOW,cfg=cfg)
    e=D(p['entry']);expected=D(p['notional'])/e*(e-D('99')+e*D(2)/10000)+2*cfg.fee_buffer
    assert D(p['planned_risk'])==expected and expected<=D('.15')

def test_restart_window_does_not_forget_future_timestamps():
    from trader.persistent.runtime import restart_history
    assert restart_history([600,900,1000,1001],1000,300)==[900,1000,1001]
    with pytest.raises(Unsafe):restart_history([float('nan')],1000,300)

def test_fixed_sources_and_mode_cannot_be_disabled():
    with pytest.raises(Unsafe):C.set_value(C.validate({}),'clock.health_mode','off')
    with pytest.raises(Unsafe):C.set_value(C.validate({}),'execution.review_required','false')

@pytest.mark.parametrize('heartbeat_age,heartbeat_state,supervisor_age,eligible',[(0,'HEALTHY',0,True),(16,'HEALTHY',0,False),(0,'BACKOFF',0,False),(0,'HEALTHY',16,False)])
def test_status_rechecks_current_monitor_deadlines(tmp_path,heartbeat_age,heartbeat_state,supervisor_age,eligible):
    from trader.persistent.cli import refresh_monitor_evidence
    h={'at':NOW.timestamp()-heartbeat_age,'pid':123,'state':heartbeat_state}
    sup={'at':NOW.timestamp()-supervisor_age,'worker_pid':123,'state':'RUNNING'}
    (tmp_path/'heartbeat.json').write_text(json.dumps(h));(tmp_path/'supervisor-health.json').write_text(json.dumps(sup))
    s={'trading_eligible_now':True,'blockers':[],'hard_read_only':False}
    refresh_monitor_evidence(s,tmp_path,C.validate({}),NOW)
    assert s['trading_eligible_now']==eligible
    assert json.loads((tmp_path/'heartbeat.json').read_text())==h

@pytest.mark.parametrize('post_enable_pass',[True,False])
def test_transition_holds_entries_until_post_enable_verification(tmp_path,monkeypatch,post_enable_pass):
    from trader import doctor as module
    from trader.models import UTC
    from datetime import datetime
    r=tmp_path/'runtime';r.mkdir();C.write(tmp_path,C.validate({}))
    db=Ledger(r/'persistent.sqlite3');C.apply(tmp_path,db,datetime.now(UTC));db.close()
    for marker in ('READ_ONLY','MAINTENANCE'):(r/marker).write_text('test operator hold')
    calls=[]
    async def fake_doctor(*args):
        calls.append(1)
        if len(calls)>1:assert (r/'TRANSITION').exists()
        if len(calls)==3:
            ledger=Ledger(r/'persistent.sqlite3')
            assert ledger.settings().live_enabled and not ledger.orders() and not ledger.trades();ledger.close()
        return {'ready_for_transition':len(calls)<3 or post_enable_pass,'blockers':[], 'config_fingerprint':C.fingerprint(C.load(tmp_path))}
    monkeypatch.setattr(module,'doctor',fake_doctor);monkeypatch.setattr(module,'require_host',lambda *a:None)
    if post_enable_pass:asyncio.run(module.transition(tmp_path,True,True))
    else:
        with pytest.raises(Unsafe,match='Post-enable verification failed'):asyncio.run(module.transition(tmp_path,True,True))
    db=Ledger(r/'persistent.sqlite3')
    assert db.settings().live_enabled==post_enable_pass and not db.orders() and not db.trades();db.close()
    assert (r/'TRANSITION').exists()!=post_enable_pass
    assert (r/'READ_ONLY').exists()!=post_enable_pass and (r/'MAINTENANCE').exists()!=post_enable_pass
