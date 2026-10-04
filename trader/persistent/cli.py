"""Operator configuration and migration. No command here places an order."""
import json,sqlite3,hashlib
from pathlib import Path
from datetime import datetime
from ..models import UTC,iso,Unsafe
from ..store import atomic_text
from .db import Ledger

COMMANDS={'set-capital':'capital_per_trade','set-max-positions':'max_open_positions','set-max-exposure':'max_total_exposure',
          'set-trade-loss-percent':'max_loss_per_trade_percent','set-daily-loss-percent':'max_daily_loss_percent',
          'set-max-attempts':'max_entry_attempts_per_day'}

def migrate(root):
    root=Path(root);r=root/'runtime';r.mkdir(parents=True,exist_ok=True)
    legacy=r/'LIVE/state.sqlite3'
    if legacy.exists():
        db=sqlite3.connect(f'file:{legacy}?mode=ro',uri=True)
        try:
            row=db.execute("SELECT value FROM state WHERE key='position'").fetchone()
            if row and json.loads(row[0]):raise Unsafe('Legacy position requires manual migration/reconciliation')
            if db.execute("SELECT count(*) FROM intents WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','ABORTED')").fetchone()[0]:raise Unsafe('Legacy unresolved order prevents migration')
            if db.execute('SELECT count(*) FROM fills').fetchone()[0]:raise Unsafe('Legacy actual fills require an audited ledger import; no loss-budget reset permitted')
            out=r/'migration';out.mkdir(exist_ok=True)
            backup=sqlite3.connect(out/'legacy-state.sqlite3');db.backup(backup);backup.close()
        finally:db.close()
    ledger=Ledger(r/'persistent.sqlite3')
    try:
        from ..configuration import path,apply
        from ..config_cli import init
        if not path(root).exists():init(root)
        if ledger.trades() or ledger.orders():raise Unsafe('Existing persistent positions/orders: migration already in use')
        ledger.configure('live_enabled',False)
        apply(root,ledger,datetime.now(UTC))
        atomic_text(r/'READ_ONLY','Migration/testing: every broker mutation disabled\n')
        # Preserve historical approval, KILL, maintenance and old daily decisions.
        atomic_text(r/'PERSISTENT','persistent-v2\n')
        ledger.event(datetime.now(UTC),'MIGRATED_DISABLED',dict(legacy=str(legacy),settings=ledger.settings().display()))
        print('Persistent architecture selected. LIVE disabled. Hard READ_ONLY enabled. Historical state preserved.')
    finally:ledger.close()

def configure(root,command,value=None):
    from ..config_cli import configure as canonical
    return canonical(root,command,value)

def live_switch(root,enabled):
    if enabled:raise Unsafe('Use production activate for the controlled readiness transition')
    r=Path(root)/'runtime'
    if not (r/'PERSISTENT').exists():raise Unsafe('Migrate before using persistent enablement')
    ledger=Ledger(r/'persistent.sqlite3')
    try:
        ledger.configure('live_enabled',enabled)
        print('Persistent LIVE preference '+('enabled' if enabled else 'disabled')+'. Current session/health/risk gates and READ_ONLY remain mandatory.')
    finally:ledger.close()

def refresh_monitor_evidence(state,runtime,config,now):
    """Read current liveness; a cached status must not extend health deadlines."""
    def block(reason):
        state['trading_eligible_now']=False
        if reason not in state['blockers']:state['blockers'].append(reason)
    try:
        h=json.loads((runtime/'heartbeat.json').read_text())
        age=now.timestamp()-h['at'];state['heartbeat']=dict(h,age_seconds=age)
        if not 0<=age<=config['watchdog']['heartbeat_stale_seconds'] or h['state'] in ('STOPPED','STARTING','CONNECTING','DISCONNECTED','BACKOFF'):
            block('Current monitor heartbeat unhealthy')
        sup=json.loads((runtime/'supervisor-health.json').read_text());state['watchdog']=sup
        if not 0<=now.timestamp()-sup['at']<=config['host']['health_max_age_seconds'] or sup.get('state')!='RUNNING' or sup.get('worker_pid')!=h['pid']:
            block('Current watchdog evidence unhealthy')
    except (OSError,ValueError,KeyError,TypeError):block('Current monitor/watchdog evidence unavailable')
    for marker in ('STOP','TRANSITION','ENTRIES_DISABLED','LIQUIDATE'):
        if (runtime/marker).exists():block(marker)
    state['mode']='READ_ONLY' if state['hard_read_only'] else 'LIVE'

def status(root,json_output=False):
    r=Path(root)/'runtime'
    path=r/'persistent-status.json'
    # Status is read-only: never initialize DB, advance daily state or change flags.
    if not path.exists():print('Persistent monitor has not published status yet. Entries blocked.');return
    s=json.loads(path.read_text())
    db=sqlite3.connect(f'file:{r}/persistent.sqlite3?mode=ro',uri=True)
    try:config=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
    finally:db.close()
    from .config import Settings
    from ..models import dt
    settings=Settings(**config).validate()
    if s.get('config')!=settings.display():
        s['trading_eligible_now']=False;s['blockers'].append('Configuration changed; awaiting monitor recheck')
    s['config']=settings.display();s['persistent_live_enabled']=settings.live_enabled
    age=(datetime.now(UTC)-dt(s['at'])).total_seconds()
    if age<0 or age>45:s['trading_eligible_now']=False;s['blockers'].append('Monitor status stale')
    if not settings.live_enabled:s['trading_eligible_now']=False
    for name,key in [('KILL','kill'),('MAINTENANCE','maintenance'),('READ_ONLY','hard_read_only')]:
        s[key]=(r/name).exists()
        if s[key]:s['trading_eligible_now']=False
    s['status_age_seconds']=round(age,2)
    from ..configuration import load,fingerprint,warnings,path as canonical_path
    try:
        canonical=load(root)
        s['config_path']=str(canonical_path(root));s['requested_config_fingerprint']=fingerprint(canonical);s['config_warnings']=warnings(canonical)
        refresh_monitor_evidence(s,r,canonical,datetime.now(UTC))
        if s.get('config_fingerprint')!=fingerprint(canonical):s['blockers'].append('Configuration pending application/restart');s['trading_eligible_now']=False
    except Unsafe as exc:s['blockers'].append(str(exc));s['trading_eligible_now']=False
    for name,key in [('KILL','kill'),('MAINTENANCE','maintenance'),('READ_ONLY','hard_read_only')]:
        if s[key] and name not in s['blockers']:s['blockers'].append(name)
    s['config'].pop('account_suffix',None)
    if json_output:
        print(json.dumps(s,indent=2));return
    from ..models import NY
    def et(value):return dt(value).astimezone(NY).strftime('%Y-%m-%d %I:%M %p ET') if value else 'No session today'
    clock=s.get('clock') or {}
    offset=clock.get('offset_seconds');uncertainty=clock.get('uncertainty_seconds')
    clock_ms=dict(offset_ms=offset*1000 if offset is not None else None,uncertainty_ms=uncertainty*1000 if uncertainty is not None else None,
                  combined_bound_ms=(abs(offset)+uncertainty)*1000 if offset is not None and uncertainty is not None else None,observed_at=clock.get('at'))
    lines=[
        ('MODE',s.get('mode','READ_ONLY' if s['hard_read_only'] else 'LIVE')),('SESSION',s.get('session_date')),('MARKET',s.get('market_state')),('ENTRY START',et(s.get('entry_start'))),
        ('CONFIG PATH',s.get('config_path')),('CONFIG FINGERPRINT',s.get('config_fingerprint')),('CONFIG WARNINGS',s.get('config_warnings',[])),
        ('BUYING POWER',s.get('buying_power')),('BUYING POWER RESERVE',settings.buying_power_reserve),('ENTRY ATTEMPTS REMAINING',max(0,settings.max_entry_attempts_per_day-s['entry_attempts_today'])),('WATCHDOG',s.get('watchdog')),
        ('PERSISTENT LIVE ENABLED',s['persistent_live_enabled']),('TRADING ELIGIBLE NOW',s['trading_eligible_now']),
        ('CAPITAL PER TRADE','$'+settings.capital_per_trade),('MAX POSITIONS',settings.max_open_positions),
        ('MAX TOTAL EXPOSURE','$'+settings.max_total_exposure),('CURRENT + RESERVED EXPOSURE',f"${s['current_exposure']} + ${s['reserved_exposure']}"),
        ('EXPOSURE MARK FRESH',s['exposure_mark_fresh']),
        ('PER-TRADE LOSS LIMIT',f"{settings.max_loss_per_trade_percent}% of capital/trade = ${settings.trade_loss}"),
        ('DAILY LOSS LIMIT',f"{settings.max_daily_loss_percent}% of max exposure = ${settings.daily_loss}; effective session budget ${s['daily_loss_budget']}"),
        ('DAILY P&L','$'+s['daily_pnl']),('DAILY LOSS REMAINING','$'+s['daily_loss_remaining']),
        ('UNRESERVED DAILY RISK REMAINING','$'+s['daily_unreserved_risk_remaining']),
        ('TRADES TODAY',s['trades_today']),('ENTRY ATTEMPTS TODAY',f"{s['entry_attempts_today']} / {settings.max_entry_attempts_per_day}"),
        ('AUTH',s['auth']),('CONNECTIVITY',s['connectivity']),('CLOCK',clock_ms),('HOST',s['host_health']),
        ('DATA HEALTH',s['data_health']),('RECONCILIATION',s['reconciliation']),('HEARTBEAT',s['heartbeat']),
        ('KILL',s['kill']),('MAINTENANCE',s['maintenance']),('HARD READ ONLY',s['hard_read_only']),
        ('OPENING HEALTH',s['opening_health']),('ENTRY CUTOFF',et(s['entry_cutoff'])),('LIQUIDATION TIME',et(s['liquidation_time'])),
        ('BLOCKERS',s['blockers']),('STATUS AGE SECONDS',s['status_age_seconds'])]
    for name,value in lines:print(name+': '+(json.dumps(value) if isinstance(value,(dict,list)) else str(value)))
    print('OPEN POSITIONS: '+str(len(s['open_positions'])))
    for position in s['open_positions']:print(json.dumps(position,indent=2))
    print('PENDING POSITIONS: '+json.dumps(s.get('pending_positions',[])))
    print('PENDING/UNRESOLVED ORDERS: '+json.dumps(s['pending_unresolved_orders']))
    signal=s.get('last_signal_rejection') or {}
    print('LAST SIGNAL / REJECTION: '+json.dumps({k:signal.get(k) for k in ('symbol','analysis_result','risk_engine_result','final_reason')}))
