"""File configuration commands; no real-money enablement."""
import json,sqlite3
from pathlib import Path
from datetime import datetime
from . import configuration as C
from .models import UTC,Unsafe
from .store import atomic_text,process_lock
from .persistent.db import Ledger

def init(root):
    if C.path(root).exists():raise Unsafe('Configuration already exists; it was not overwritten')
    values=C.validate({});dbpath=Path(root)/'runtime/persistent.sqlite3'
    if dbpath.exists():
        db=sqlite3.connect(f'file:{dbpath}?mode=ro',uri=True)
        try:
            row=db.execute("SELECT value FROM kv WHERE key='settings'").fetchone();old=json.loads(row[0]) if row else {}
            mapping={'capital_per_trade':'trading.capital_per_trade','max_open_positions':'trading.max_open_positions','max_total_exposure':'trading.max_total_exposure','max_entry_attempts_per_day':'trading.max_entry_attempts_per_session','buying_power_reserve':'trading.buying_power_reserve','max_loss_per_trade_percent':'risk.max_loss_per_trade_percent','max_daily_loss_percent':'risk.max_daily_loss_percent','account_suffix':'broker.account_suffix'}
            for k,key in mapping.items():
                if k in old:section,field=key.split('.');values[section][field]=old[k]
        finally:db.close()
    C.write(root,values)
    return values

def configure(root,command,value=None,key=None,dry_run=False):
    root=Path(root);r=root/'runtime';r.mkdir(parents=True,exist_ok=True)
    if command=='path':print(C.path(root));return
    if command=='init':init(root);print('Private configuration created; LIVE permission unchanged.');return
    if command=='reset':
        if not dry_run:raise Unsafe('Reset supports --dry-run only; no automatic risk reset')
        print(json.dumps(C.public(C.DEFAULTS),indent=2));return
    if command=='show' or command=='validate':
        config=C.load(root)
    else:
        with process_lock(r/'configuration.lock'):
            config=C.load(root,required=False)
            name=key if command=='set' else C.ALIASES.get(command)
            if not name:raise Unsafe('Unknown configuration command')
            new=C.set_value(config,name,value)
            ledger=Ledger(r/'persistent.sqlite3')
            try:
                if ledger.settings().live_enabled:raise Unsafe('Disable persistent LIVE before configuration changes')
                if ledger.trades() or ledger.orders():raise Unsafe('Configuration locked while positions/pending/unresolved orders exist')
                from .calendar import Calendar
                now=datetime.now(UTC);session=Calendar().session(now)
                if session and session.contains(now) and C.strategy_fingerprint(config)!=C.strategy_fingerprint(new):raise Unsafe('Strategy/session changes are next-session only; wait until market close')
                C.write(root,new)
                # On a first run without a daemon, apply immediately. A live
                # daemon keeps its pinned snapshot until controlled restart.
                if not (r/'supervisor.json').exists():C.apply(root,ledger,now)
                else:ledger.event(now,'CONFIG_STAGED',dict(fingerprint=C.fingerprint(new),key=name))
            finally:ledger.close()
            config=new
    output=C.public(config)
    output['derived']={'per_trade_loss_dollars':str(C.D(config['trading']['capital_per_trade'])*C.D(config['risk']['max_loss_per_trade_percent'])/100),'daily_loss_dollars':str(C.D(config['trading']['max_total_exposure'])*C.D(config['risk']['max_daily_loss_percent'])/100),'fingerprint':C.fingerprint(config),'strategy_fingerprint':C.strategy_fingerprint(config),'config_path':str(C.path(root)),'warnings':C.warnings(config),'account_selector_configured':bool(config['broker']['account_suffix']),'reload':'Restart required; no mid-session strategy/session changes; positions/orders lock changes'}
    print(json.dumps(output,indent=2))
