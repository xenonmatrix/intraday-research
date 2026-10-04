"""Read-only readiness inspection and a separately invoked operator transition.
Doctor never binds a submission/cancellation guard, creates an intent or changes a latch.
"""
import asyncio,json,os,platform,sqlite3,time
from datetime import datetime,timedelta
from pathlib import Path
from . import configuration as C
from .models import UTC,NY,D,Unsafe,dt,iso,valid_quote
from .calendar import Calendar
from .host_health import require_host
from .broker import connect,failure_details,TokenFile
from .risk import reconcile,eligible,TERMINAL as BROKER_TERMINAL
from .orb import evaluate
from .store import atomic_text,process_lock
from .persistent.db import Ledger

OPERATOR_BLOCKS=('Persistent LIVE disabled','READ_ONLY','KILL','MAINTENANCE')

def read_db(root):
    p=Path(root)/'runtime/persistent.sqlite3'
    if not p.exists():raise Unsafe('Persistent database missing; run migrate-persistent')
    db=sqlite3.connect(f'file:{p}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    return db

def kv(db,key,default=None):
    r=db.execute('select value from kv where key=?',(key,)).fetchone();return json.loads(r[0]) if r else default

async def doctor(root,broker_check=False):
    root=Path(root);r=root/'runtime';now=datetime.now(UTC)
    result=dict(at=iso(now),python=platform.python_version(),platform=platform.system(),checks={},blockers=[],operator_blocks=[],ready=False,ready_for_transition=False,read_only=True)
    def check(name,ok,detail=None):
        result['checks'][name]=dict(passed=bool(ok),detail=detail)
        if not ok:result['blockers'].append(str(detail or name+' failed'))
    try:config=C.load(root);check('configuration',True);result.update(config_path=str(C.path(root)),config_fingerprint=C.fingerprint(config),warnings=C.warnings(config))
    except Unsafe as exc:check('configuration',False,str(exc));return result
    cfg=C.market_config(config)
    check('platform',platform.system()=='Darwin','macOS production host adapter required' if platform.system()!='Darwin' else 'macOS')
    check('config_permissions',C.path(root).stat().st_mode&0o077==0,'Local config must have mode 0600')
    check('runtime_permissions',r.exists() and r.stat().st_mode&0o077==0,'Runtime directory must have mode 0700')
    check('account_selector',bool(config['broker']['account_suffix']),'Configure broker.account_suffix locally')
    session=Calendar(config).session(now)
    check('session',session is not None and session.entry_window(now),'Outside calendar entry window' if not session or not session.entry_window(now) else session.day)
    if session:result['session']=dict(day=session.day,open=iso(session.open),close=iso(session.close),entry_start=iso(session.open+timedelta(minutes=session.opening_minutes)),entry_cutoff=iso(session.close-timedelta(minutes=session.entry_cutoff_minutes)),liquidation=iso(session.close-timedelta(minutes=session.liquidation_minutes)))
    for marker in ('READ_ONLY','KILL','MAINTENANCE'):
        if (r/marker).exists():result['operator_blocks'].append(marker)
    for marker in ('STOP','LIQUIDATE','ENTRIES_DISABLED'):
        check(marker,not (r/marker).exists(),marker+' active' if (r/marker).exists() else 'inactive')
    try:
        db=read_db(root)
        try:
            check('database',db.execute('PRAGMA quick_check').fetchone()[0]=='ok','SQLite quick_check')
            applied=kv(db,'production_config');check('applied_configuration',applied==config,'Pending/missing applied configuration; controlled restart required')
            settings=kv(db,'settings',{})
            if not settings.get('live_enabled'):result['operator_blocks'].append('Persistent LIVE disabled')
            expected=C.settings_values(config);check('risk_configuration',all(settings.get(k)==v for k,v in expected.items()),'Applied risk settings must match canonical configuration')
            positions=db.execute("select count(*) from trades where status='OPEN'").fetchone()[0]
            orders=db.execute("select count(*) from orders where status not in ('FILLED','CANCELLED','REJECTED','ABORTED')").fetchone()[0]
            check('local_flat',positions==0 and orders==0,'Production transition requires zero local positions and unresolved/pending orders')
            row=db.execute('select data from sessions where day=?',(now.astimezone(NY).date().isoformat(),)).fetchone();day=json.loads(row[0]) if row else {}
            check('connectivity',bool(day) and not day.get('connectivity_halt',True) and not day.get('recovery_active',True),'Current session connectivity recovery incomplete' if not day or day.get('connectivity_halt') or day.get('recovery_active') else 'recovered')
            check('opening_health',bool(day.get('opening_healthy')),'Current session opening health not passed' if not day.get('opening_healthy') else day.get('opening_checked_at'))
            check('session_risk',bool(day) and not day.get('loss_latched',True) and not day.get('entry_cutoff_latched') and not day.get('closed') and day.get('attempts',0)<config['trading']['max_entry_attempts_per_session'] and D(day.get('gross_losses','0'))<D(day.get('loss_budget','0')),'Daily risk/cutoff/attempt capacity must remain')
            result['daily_loss_remaining']=str(max(D(0),D(day.get('loss_budget','0'))-D(day.get('gross_losses','0'))));result['entry_attempts_remaining']=max(0,config['trading']['max_entry_attempts_per_session']-day.get('attempts',0))
        finally:db.close()
    except (OSError,ValueError,sqlite3.Error,KeyError,Unsafe) as exc:check('database',False,type(exc).__name__+': persistent database unavailable')
    try:
        require_host(r,now);check('host_clock',True)
    except (Unsafe,OSError,ValueError) as exc:check('host_clock',False,str(exc))
    try:
        host=json.loads((r/'host-health.json').read_text());result['host']=host
        h=json.loads((r/'heartbeat.json').read_text());sup=json.loads((r/'supervisor-health.json').read_text())
        check('heartbeat',0<=time.time()-h['at']<=config['watchdog']['heartbeat_stale_seconds'] and h['state'] not in ('DISCONNECTED','STOPPED','STARTING','BACKOFF'),'Monitor heartbeat/state must be healthy')
        check('watchdog',sup.get('state')=='RUNNING' and sup.get('worker_pid')==h['pid'] and sup.get('config_fingerprint')==C.fingerprint(config) and 0<=time.time()-sup['at']<=config['host']['health_max_age_seconds'],'Supervisor/watchdog observation must be fresh and match worker/config')
        os.kill(h['pid'],0);os.kill(sup['pid'],0);result['heartbeat']=h;result['watchdog']=sup
    except (OSError,ValueError,KeyError) as exc:check('monitor',False,'Monitor evidence incomplete or process unavailable')
    if broker_check:
        try:
            if not config['broker']['account_suffix']:raise Unsafe('Broker account selector unconfigured')
            async with connect(r/'oauth.json') as broker:
                if broker.live_guard is not None or broker.cancel_guard is not None:raise Unsafe('Doctor must not possess mutation capability')
                accounts=await broker.call('get_accounts',{})
                matches=[a for a in accounts['accounts'] if a and a.get('agentic_allowed') and a['account_number'].endswith(config['broker']['account_suffix'])]
                if len(matches)!=1:raise Unsafe('Configured Agentic account is not unique')
                account=matches[0]['account_number'];snap=await broker.snapshot(account)
                power=reconcile(snap,account,datetime.now(UTC),cfg,{})
                outstanding=[o for o in snap['orders'] if o.get('state') not in BROKER_TERMINAL]
                if snap['positions'] or outstanding:raise Unsafe('Transition requires zero broker positions/outstanding orders')
                result['broker']=dict(auth='AUTHENTICATED_READ_VERIFIED',reconciled=True,positions=0,orders=0,historical_terminal_orders=len(snap['orders']),unleveraged_buying_power=str(power),observed_at=snap['observed_at'],connection=broker.connection_info)
                check('broker',True)
                now=datetime.now(UTC);session=Calendar(config).session(now)
                quotes=await broker.quotes(cfg.universe);bars=await broker.bars(cfg.universe,session,now) if session and session.contains(now) else {}
                tradability=await broker.tradability(account,cfg.universe);quotes=await broker.quotes(cfg.universe);now=datetime.now(UTC)
                data={};data_ok=bool(session and session.contains(now))
                for sym in cfg.universe:
                    try:
                        bid,ask=valid_quote(quotes[sym],now,cfg)
                        if not eligible(tradability[sym]):raise Unsafe('Symbol/fractional trading unavailable')
                        if (ask-bid)/((ask+bid)/2)>cfg.max_spread:raise Unsafe('Spread exceeds configured limit')
                        if not session or not session.contains(now):raise Unsafe('Regular-session data not yet available')
                        candidate=evaluate(sym,bars[sym],session,now,cfg)
                        if not candidate.get('opening_high'):raise Unsafe('Completed opening range not available')
                        if D(candidate['session_dollars'])<cfg.min_session_dollars:raise Unsafe('Session liquidity below minimum')
                        data[sym]=dict(state=candidate['state'],opening_high=candidate['opening_high'],opening_low=candidate['opening_low'],bid=str(bid),ask=str(ask),quote_ages={k:(now-dt(quotes[sym][k])).total_seconds() for k in ('venue_bid_time','venue_ask_time')},valid_candidate=candidate['state']=='ARMED',last_bar=candidate['last_bar'])
                    except (Unsafe,KeyError,ValueError) as exc:data_ok=False;data[sym]=dict(error=str(exc))
                result['market_data']=data;check('market_data',data_ok,'Fresh quotes, completed opening bars, spread/liquidity and tradability required')
            result['clean_close']=True
        except Exception as exc:check('broker',False,'; '.join(failure_details(exc)))
    else:check('broker',False,'Broker checks not requested; run doctor --broker')
    result['ready_for_transition']=not result['blockers']
    result['ready']=result['ready_for_transition'] and not result['operator_blocks']
    result['ended_at']=iso(datetime.now(UTC));return result

def display(result,json_output=False):
    if json_output:print(json.dumps(result,indent=2));return
    print('SYSTEM HEALTHY' if result['ready'] else 'NOT READY')
    for name,check in result['checks'].items():print(name.upper()+': '+('PASS' if check['passed'] else 'FAIL')+' — '+str(check.get('detail') or ''))
    print('OPERATOR CONTROLS: '+', '.join(result['operator_blocks']))
    print('CONFIG FINGERPRINT: '+str(result.get('config_fingerprint')))
    for warning in result.get('warnings',[]):print('WARNING: '+warning)

def kill_recovery(root,result):
    p=Path(root)/'runtime/KILL'
    if not p.exists():return
    reason=p.read_text()
    if not any(x in reason for x in ('Worker exited','Watchdog stalled worker','Monitoring cycle deadline exceeded','Watchdog restart budget exhausted')):raise Unsafe('KILL cause is not an automatically recoverable watchdog condition; manual audit required')
    db=read_db(root)
    try:
        row=db.execute("select at,data from events where kind='PERSISTENT_WORKER_START' order by id desc limit 1").fetchone()
        if not row or (datetime.now(UTC)-dt(row['at'])).total_seconds()<300 or json.loads(row['data'])['pid']!=result['heartbeat']['pid']:raise Unsafe('KILL recovery requires same healthy worker for at least five minutes')
        session=db.execute('SELECT data FROM sessions WHERE day=?',(datetime.now(NY).date().isoformat(),)).fetchone()
        day=json.loads(session[0]) if session else {}
        since=day.get('last_failure') or day.get('stable_since')
        if not since or (datetime.now(UTC)-dt(since)).total_seconds()<300:raise Unsafe('KILL recovery requires five minutes since last connectivity failure')
    finally:db.close()

async def transition(root,activate=False,acknowledge=False,recover_kill=False):
    root=Path(root);r=root/'runtime'
    if activate and not acknowledge:raise Unsafe('Activation requires explicit --acknowledge-risk')
    with process_lock(r/'configuration.lock'):
        result=await doctor(root,True)
        if not activate:display(result);return result
        if not result['ready_for_transition']:raise Unsafe('Production transition blocked: '+'; '.join(result['blockers']))
        if (r/'KILL').exists():
            if not recover_kill:raise Unsafe('KILL recovery requires explicit --recover-kill and reviewed cause')
            kill_recovery(root,result)
        # Hold new entries through the second broker/host reconciliation. The
        # durable transition marker survives interruption and is fail closed.
        atomic_text(r/'TRANSITION','Production transition in progress; entries blocked\n')
        ledger=Ledger(r/'persistent.sqlite3')
        try:
            current=C.assert_current(root,ledger)
            if C.fingerprint(current)!=result['config_fingerprint']:raise Unsafe('Configuration changed during readiness check')
            if ledger.trades() or ledger.orders():raise Unsafe('Position/order appeared during transition')
            require_host(r)
            # Second fresh read check before removing any submission protection.
            verified=await doctor(root,True)
            if not verified['ready_for_transition']:raise Unsafe('Post-check failed: '+'; '.join(verified['blockers']))
            C.assert_current(root,ledger);require_host(r)
            with ledger.tx():
                if ledger.trades() or ledger.orders():raise Unsafe('Ledger changed during transition')
                ledger.event(datetime.now(UTC),'PRODUCTION_TRANSITION',dict(config_fingerprint=C.fingerprint(current),kill_reason=(r/'KILL').read_text() if (r/'KILL').exists() else None))
                for name in ('KILL','MAINTENANCE','READ_ONLY'):(r/name).unlink(missing_ok=True)
                values=ledger.get('settings');values['live_enabled']=True;ledger.put('settings',values)
            # Keep entries held until an independent broker check after enablement.
            final=await doctor(root,True)
            if not final['ready_for_transition']:raise Unsafe('Post-enable verification failed')
            (r/'TRANSITION').unlink();print('Persistent LIVE enabled; every future entry still requires current signal and all gates. No test order submitted.')
        except BaseException:
            ledger.configure('live_enabled',False)
            atomic_text(r/'READ_ONLY','Production transition failed; all broker mutations blocked\n')
            atomic_text(r/'MAINTENANCE','Production transition failed; review audit\n')
            raise
        finally:ledger.close()
