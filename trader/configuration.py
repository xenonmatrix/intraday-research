"""Canonical, validated JSON configuration. No broker calls or implicit live enablement."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import hashlib,json,math,os,re,sqlite3
from pathlib import Path
from .models import Config,D,Unsafe

# Explicitly supported knobs. Unsupported/unsafe modes are rejected, not ignored.
DEFAULTS={
 'trading':{'default_mode':'READ_ONLY','capital_per_trade':'10','max_open_positions':3,'max_total_exposure':'30','max_entry_attempts_per_session':5,'one_position_per_symbol':True,'buying_power_reserve':'20','symbols':['SPY','QQQ']},
 'risk':{'max_loss_per_trade_percent':'1.5','max_daily_loss_percent':'5','daily_loss_base':'max_total_exposure','profits_replenish_allowance':False},
 'strategy':{'name':'orb','bar_interval_minutes':5,'opening_range_minutes':15,'breakout_offset':'0.01','stop_offset':'0.01','target_r':'2','max_hold_minutes':30,'vwap_exit':True,'retrospective_entries':False},
 'market_data':{'max_quote_age_seconds':5,'max_spread_percent':'0.05','min_session_dollar_volume':'10000000','future_timestamp_tolerance_seconds':0,'bars_refresh_seconds':20,'tradability_refresh_seconds':60},
 'session':{'entry_cutoff_minutes_before_close':60,'liquidation_minutes_before_close':15,'monitor_warmup_minutes':10},
 'execution':{'order_type':'market','allow_fractional':True,'review_required':True,'preflight_max_age_seconds':5,'pending_entry_timeout_seconds':30,'slippage_bps':'1','fee_buffer':'0.01'},
 'connectivity':{'request_timeout_seconds':8,'connect_timeout_seconds':8,'stream_read_timeout_seconds':300,'initialize_timeout_seconds':30,'backoff_initial_seconds':2,'backoff_max_seconds':30,'position_backoff_max_seconds':5,'outage_threshold':3,'halt_after_seconds':60,'recovery_observations':30,'recovery_min_seconds':300,'ordinary_recovery_observations':3,'ordinary_recovery_seconds':30,'poll_seconds':2,'idle_poll_seconds':30,'account_cache_seconds':8,'account_max_age_seconds':15,'keepalive_expiry_seconds':60},
 'clock':{'health_mode':'combined_bound','max_combined_bound_ms':'50','sample_count':1,'sample_interval_seconds':60,'time_sources':['time.apple.com'],'sample_timeout_seconds':2,'sample_max_age_seconds':120},
 'host':{'min_battery_percent':50,'require_ac_power':False,'require_lid_open':True,'require_sleep_prevention':True,'health_max_age_seconds':15,'poll_seconds':5},
 'watchdog':{'heartbeat_interval_seconds':5,'heartbeat_stale_seconds':15,'check_interval_seconds':1,'restart_after_seconds':45,'startup_timeout_seconds':45,'restart_limit':3,'restart_window_seconds':300,'cycle_deadline_seconds':15},
 'logging':{'level':'INFO'},
 'analysis':{'export_directory':'reports/persistent','features_only':True},
 'broker':{'provider':'robinhood','account_suffix':''}
}
DECIMAL_KEYS={f'{s}.{k}' for s,values in DEFAULTS.items() for k,v in values.items() if isinstance(v,str) and re.fullmatch(r'\d+(\.\d+)?',v)}
FIXED={'trading.one_position_per_symbol':True,'risk.daily_loss_base':'max_total_exposure','risk.profits_replenish_allowance':False,'strategy.name':'orb','strategy.bar_interval_minutes':5,'strategy.opening_range_minutes':15,'strategy.retrospective_entries':False,'market_data.future_timestamp_tolerance_seconds':0,'execution.order_type':'market','execution.allow_fractional':True,'execution.review_required':True,'clock.health_mode':'combined_bound','clock.sample_count':1,'clock.time_sources':['time.apple.com'],'analysis.features_only':True,'broker.provider':'robinhood'}
ALIASES={'set-capital':'trading.capital_per_trade','set-max-positions':'trading.max_open_positions','set-max-exposure':'trading.max_total_exposure','set-trade-loss-percent':'risk.max_loss_per_trade_percent','set-daily-loss-percent':'risk.max_daily_loss_percent','set-max-attempts':'trading.max_entry_attempts_per_session','set-min-battery':'host.min_battery_percent','set-heartbeat-interval':'watchdog.heartbeat_interval_seconds'}

def path(root):return Path(root)/'config/trader.json'
def _pairs(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise Unsafe('Duplicate configuration key: '+k)
        out[k]=v
    return out

def validate(raw):
    if type(raw) is not dict:raise Unsafe('Configuration must be an object')
    result=deepcopy(DEFAULTS)
    for section,values in raw.items():
        if section not in DEFAULTS:raise Unsafe('Unknown configuration section: '+section)
        if type(values) is not dict:raise Unsafe(section+' must be an object')
        for key,value in values.items():
            name=section+'.'+key
            if key not in DEFAULTS[section]:raise Unsafe('Unknown configuration key: '+name)
            default=DEFAULTS[section][key]
            if name in DECIMAL_KEYS:
                if type(value) not in (str,int,float):raise Unsafe(name+' must be a finite decimal')
                try:n=D(str(value))
                except InvalidOperation as exc:raise Unsafe(name+' must be a finite decimal') from exc
                if not n.is_finite() or n<=0:raise Unsafe(name+' must be finite and positive')
                if name=='clock.max_combined_bound_ms' and (not math.isfinite(float(n)) or float(n)/1000==0):
                    raise Unsafe(name+' must remain finite and positive in clock arithmetic')
                value=format(n.normalize(),'f')
            elif type(default) is int:
                if type(value) is not int:raise Unsafe(name+' must be an integer')
                if value<0 or (value==0 and name not in ('host.min_battery_percent','market_data.future_timestamp_tolerance_seconds')):raise Unsafe(name+' must be positive')
            elif type(default) is bool:
                if type(value) is not bool:raise Unsafe(name+' must be true or false')
            elif type(default) is str:
                if type(value) is not str:raise Unsafe(name+' must be a string')
            elif type(default) is list:
                if type(value) is not list or not value or any(type(v) is not str for v in value) or len(set(value))!=len(value):raise Unsafe(name+' must be a nonempty unique string list')
            if name in FIXED and value!=FIXED[name]:raise Unsafe(name+' is an invariant or unsupported mode; required: '+str(FIXED[name]))
            result[section][key]=value
    t,r,h,w,c,m,s=result['trading'],result['risk'],result['host'],result['watchdog'],result['connectivity'],result['market_data'],result['session']
    if D(t['capital_per_trade'])<1:raise Unsafe('trading.capital_per_trade must be at least $1')
    if D(t['capital_per_trade'])>D(t['max_total_exposure']):raise Unsafe('capital_per_trade exceeds max_total_exposure')
    if D(t['buying_power_reserve'])<20:raise Unsafe('buying_power_reserve must remain at least $20')
    if t['default_mode'] not in ('READ_ONLY','PAPER'):raise Unsafe('Fresh/default mode must be READ_ONLY or PAPER')
    if not set(t['symbols'])<=set(('SPY','QQQ')):raise Unsafe('Only SPY and QQQ are currently validated symbols')
    if D(r['max_loss_per_trade_percent'])>10 or D(r['max_daily_loss_percent'])>25:raise Unsafe('Supported loss ranges: per trade (0,10]%, daily (0,25]%')
    if h['min_battery_percent']>100:raise Unsafe('host.min_battery_percent must be in 0..100')
    if w['heartbeat_stale_seconds']<2*w['heartbeat_interval_seconds']:raise Unsafe('heartbeat_stale_seconds must be at least twice heartbeat_interval_seconds')
    if w['restart_after_seconds']<=max(w['heartbeat_stale_seconds'],w['cycle_deadline_seconds']):raise Unsafe('restart_after_seconds must exceed heartbeat staleness and cycle deadline')
    if w['startup_timeout_seconds']<=c['initialize_timeout_seconds']:raise Unsafe('startup_timeout_seconds must exceed initialize_timeout_seconds')
    if w['check_interval_seconds']>=w['heartbeat_stale_seconds']:raise Unsafe('watchdog check interval must be shorter than heartbeat stale threshold')
    if c['request_timeout_seconds']>=w['cycle_deadline_seconds']:raise Unsafe('request_timeout_seconds must be shorter than cycle_deadline_seconds')
    if c['connect_timeout_seconds']>c['initialize_timeout_seconds']:raise Unsafe('connect timeout must not exceed initialize timeout')
    if c['poll_seconds']>=w['heartbeat_stale_seconds'] or h['poll_seconds']>=h['health_max_age_seconds']:raise Unsafe('Polling interval must be shorter than its health deadline')
    if c['account_cache_seconds']>=c['account_max_age_seconds']:raise Unsafe('Account cache must be shorter than account max age')
    if not c['backoff_initial_seconds']<=c['position_backoff_max_seconds']<=c['backoff_max_seconds']:raise Unsafe('Invalid reconnect backoff ordering')
    if c['keepalive_expiry_seconds']<=c['idle_poll_seconds']:raise Unsafe('Keepalive expiry must exceed idle polling interval')
    if c['recovery_observations']<c['ordinary_recovery_observations'] or c['recovery_min_seconds']<c['ordinary_recovery_seconds']:raise Unsafe('Halt recovery must be at least as strict as ordinary recovery')
    if c['stream_read_timeout_seconds']<=c['request_timeout_seconds']:raise Unsafe('Stream read timeout must exceed request timeout')
    if result['clock']['sample_max_age_seconds']<result['clock']['sample_interval_seconds']+result['clock']['sample_timeout_seconds']:raise Unsafe('Clock sample max age must exceed interval plus timeout')
    if not 0<s['liquidation_minutes_before_close']<s['entry_cutoff_minutes_before_close']<180:raise Unsafe('Require 0 < liquidation offset < entry cutoff offset < 180 minutes (early closes)')
    if result['strategy']['max_hold_minutes']>120:raise Unsafe('Supported maximum holding time is 120 minutes')
    if D(m['max_spread_percent'])>1:raise Unsafe('Supported maximum spread is 1 percent')
    if result['logging']['level'] not in ('DEBUG','INFO','WARNING','ERROR'):raise Unsafe('Unsupported logging.level')
    export=Path(result['analysis']['export_directory'])
    if export.is_absolute() or '..' in export.parts or not export.parts or export.parts[0]!='reports':raise Unsafe('analysis.export_directory must be a relative path under reports/')
    suffix=result['broker']['account_suffix']
    if suffix and not re.fullmatch(r'\d{4}',suffix):raise Unsafe('broker.account_suffix must be four digits or empty (unconfigured)')
    return result

def load(root,required=True):
    p=path(root)
    if not p.exists():
        if required:raise Unsafe('Configuration missing: run trader config init')
        return validate({})
    if p.is_symlink():raise Unsafe('Configuration symlinks are unsupported')
    try:return validate(json.loads(p.read_text(),object_pairs_hook=_pairs,parse_constant=lambda x:(_ for _ in ()).throw(Unsafe('Non-finite JSON number: '+x))))
    except (ValueError,OSError) as exc:raise Unsafe('Cannot parse configuration: '+str(exc)) from exc

def public(config):
    out=deepcopy(config);out.get('broker',{}).pop('account_suffix',None);return out

def fingerprint(config):return hashlib.sha256(json.dumps(public(config),sort_keys=True,separators=(',',':')).encode()).hexdigest()
def strategy_fingerprint(config):
    return hashlib.sha256(json.dumps({k:config[k] for k in ('strategy','session')},sort_keys=True,separators=(',',':')).encode()).hexdigest()
def warnings(config):
    out=[]
    for section in ('clock','host','watchdog','connectivity','market_data','execution'):
        if config[section]!=DEFAULTS[section]:out.append('ADVANCED / SAFETY CRITICAL: nondefault '+section+' policy requires review and revalidation')
    if config['strategy']!=DEFAULTS['strategy'] or config['session']!=DEFAULTS['session']:out.append('STRATEGY CHANGE: new fingerprint; next-session only')
    if not config['broker']['account_suffix']:out.append('Broker account selector unconfigured: production readiness blocked')
    return out

def write(root,config):
    config=validate(config);p=path(root);p.parent.mkdir(parents=True,exist_ok=True)
    from .store import atomic_text
    atomic_text(p,json.dumps(config,indent=2)+'\n');p.chmod(0o600)

def settings_values(config):
    t=config['trading'];r=config['risk']
    return dict(capital_per_trade=t['capital_per_trade'],max_open_positions=t['max_open_positions'],max_total_exposure=t['max_total_exposure'],max_entry_attempts_per_day=t['max_entry_attempts_per_session'],buying_power_reserve=t['buying_power_reserve'],max_loss_per_trade_percent=r['max_loss_per_trade_percent'],max_daily_loss_percent=r['max_daily_loss_percent'],account_suffix=config['broker']['account_suffix'])

def market_config(config):
    t=config['trading'];m=config['market_data'];e=config['execution'];c=config['connectivity'];s=config['strategy']
    return replace(Config(),universe=tuple(t['symbols']),max_spread=D(m['max_spread_percent'])/100,min_session_dollars=D(m['min_session_dollar_volume']),quote_age=m['max_quote_age_seconds'],account_age=c['account_max_age_seconds'],poll_seconds=c['poll_seconds'],call_timeout=c['request_timeout_seconds'],heartbeat_timeout=config['watchdog']['heartbeat_stale_seconds'],slip_bps=D(e['slippage_bps']),fee_buffer=D(e['fee_buffer']),breakout_offset=D(s['breakout_offset']),stop_offset=D(s['stop_offset']),target_r=D(s['target_r']),max_hold_minutes=s['max_hold_minutes'],vwap_exit=s['vwap_exit'])

def applied(ledger):return ledger.get('production_config') or validate({})
def apply(root,ledger,now):
    config=load(root);old=ledger.get('production_config')
    if old==config:return config
    if ledger.trades() or ledger.orders():raise Unsafe('Configuration locked while positions or unresolved/pending orders exist')
    if ledger.settings().live_enabled:raise Unsafe('Disable persistent LIVE before applying configuration')
    # A new source can migrate current defaults without resetting any day state.
    from .calendar import Calendar
    session=Calendar().session(now)
    previous=old or validate({})
    if session and session.contains(now) and strategy_fingerprint(previous)!=strategy_fingerprint(config):raise Unsafe('Strategy/session changes cannot apply during a market session')
    with ledger.tx():
        values=ledger.get('settings');values.update(settings_values(config));ledger.put('settings',values)
        ledger.put('production_config',config);ledger.put('config_fingerprint',fingerprint(config));ledger.put('strategy_fingerprint',strategy_fingerprint(config))
        ledger.put('config_snapshot:'+fingerprint(config),public(config))
        if old is not None and fingerprint(old)!=fingerprint(config) and session:
            day=ledger.session(session.day)
            if day:
                day.update(opening_healthy=False,opening_reason='Configuration changed; fresh opening-equivalent check required')
                ledger.save_session(session.day,day)
        ledger.event(now,'CONFIG_APPLIED',dict(fingerprint=fingerprint(config),snapshot=public(config),warnings=warnings(config)))
    return config

def assert_current(root,ledger):
    cfg=load(root)
    if cfg!=ledger.get('production_config'):raise Unsafe('Configuration differs from applied snapshot; restart/revalidate before entries')
    return cfg

def set_value(config,key,text):
    if key.count('.')!=1:raise Unsafe('Use section.key')
    section,field=key.split('.')
    if section not in DEFAULTS or field not in DEFAULTS[section]:raise Unsafe('Unknown configuration key: '+key)
    default=DEFAULTS[section][field]
    try:value=text if isinstance(default,str) else json.loads(text)
    except ValueError as exc:raise Unsafe('Value must be valid JSON for '+key) from exc
    out=deepcopy(config);out[section][field]=value;return validate(out)
