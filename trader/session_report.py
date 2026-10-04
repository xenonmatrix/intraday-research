"""Persistent observation report; contains no broker or authorization operations."""
from datetime import timedelta
import json
from .models import D, Unsafe, dt, iso, valid_quote
from .host_health import require_host
from .risk import size, TERMINAL
from .store import atomic_text


def read_json(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def update(engine, status, now):
    policy=engine.policy;store=engine.store;day=policy.day
    folder=policy.project/'reports'/day;folder.mkdir(parents=True,exist_ok=True)
    candidates={s:store.get('candidate:'+s,{}) for s in engine.cfg.universe}
    snap=store.get('broker_snapshot',{})
    host=read_json(policy.runtime/'host-health.json');host_ok=False
    try:require_host(policy.runtime,now);host_ok=True
    except Unsafe:pass
    quotes=engine.last_quotes;fresh={}
    for symbol in engine.cfg.universe:
        try:valid_quote(quotes[symbol],now,engine.cfg);fresh[symbol]=True
        except (Unsafe,KeyError,ValueError):fresh[symbol]=False
    observed=[]
    if policy.session.entry_window(now) and not store.get('position'):
        for symbol,c in candidates.items():
            if c.get('state')!='ARMED':continue
            try:
                bp=snap['portfolio']['buying_power']
                power=min(D(bp['buying_power']),D(bp['unleveraged_buying_power']))-D('20')
                q,price,notional=size(c,quotes[symbol],max(D(0),power),min(D('10'),D(store.get('cash','0'))),engine.daily(day),now,engine.cfg)
                key='observed_signal:'+c['setup_id']
                if not store.get(key):
                    event=dict(at=iso(now),symbol=symbol,setup_id=c['setup_id'],candidate=c,
                        estimated_quantity=str(q),estimated_notional=str(notional),execution_authorized=False,
                        scope='Read-only strategy/quote/risk observation; tradability and final order review not performed',gate=store.get('gate'))
                    store.save(key,event);store.event(iso(now),'READ_ONLY_SIGNAL_OBSERVED',event)
            except (Unsafe,KeyError,ValueError):pass
    for row in store.db.execute("select value from state where key like 'observed_signal:%'"):
        value=json.loads(row[0])
        if value['setup_id'].startswith(day+':'):observed.append(value)
    ranges_complete=all(c.get('opening_high') and c.get('opening_low') and c.get('state')!='DATA_INVALID' for c in candidates.values())
    opening=dict(at=iso(now),phase='PENDING_REGULAR_OPEN' if now<policy.session.open else 'BUILDING_RANGE' if now<policy.session.open+timedelta(minutes=15) else 'RANGE_CHECK',
        range_complete=bool(ranges_complete),host_healthy=host_ok,quotes_fresh=fresh,
        supervisor_checkpoint=status.get('unattended',{}).get('opening'),execution_authorized=False)
    if now>=policy.session.open+timedelta(minutes=15) and not store.get('observation_opening:'+day):
        if ranges_complete or now>=policy.session.open+timedelta(minutes=16):
            store.save('observation_opening:'+day,opening);store.event(iso(now),'READ_ONLY_OPENING_CHECK',opening)
    snapshot_at=snap.get('observed_at');age=(now-dt(snapshot_at)).total_seconds() if snapshot_at else None
    closed=now>=policy.session.close
    orders=[dict(r) for r in store.db.execute('select * from intents')]
    for r in orders:r['payload']=json.loads(r['payload'])
    result=dict(updated_at=iso(now),session_date=day,observation_only=(policy.runtime/'MAINTENANCE').exists(),status=status,host_health=host,
        authentication=read_json(policy.runtime/'authentication.json'),heartbeat=read_json(policy.runtime/'heartbeat.json'),
        opening_health=store.get('observation_opening:'+day,opening),current_data_health=opening,
        candidates=candidates,observed_signals=observed,orders=orders,fills=store.rows(),
        broker_snapshot_at=snapshot_at,broker_snapshot_age_seconds=age,
        broker_positions=snap.get('positions'),broker_orders=snap.get('orders'),
        gates={k:(policy.runtime/k).exists() for k in ('KILL','MAINTENANCE','ENTRIES_DISABLED','LIQUIDATE','STOP')},
        market_closed=closed,end_of_day_state=('PENDING' if not closed else
            'FLAT_READ_VERIFIED' if age is not None and 0<=age<=engine.cfg.account_age and dt(snapshot_at)>=policy.session.close and snap.get('positions')==[] and all(o.get('state') in TERMINAL for o in snap.get('orders',[])) and not store.get('position') and not store.unresolved() else 'UNVERIFIED_OR_NOT_FLAT'),
        logs=dict(database=str(engine.root/'state.sqlite3'),operational=str(policy.runtime/'unattended.jsonl'),transport=str(policy.runtime/'mcp-diagnostics.jsonl'),trades=str(engine.root/'trades.csv')))
    atomic_text(folder/'latest.json',json.dumps(result,indent=2))
    atomic_text(folder/'session.md','# '+day+' session observation\n\nThis reporter never grants activation. It updates independently of Terminal/Codex.\n\n```json\n'+json.dumps(result,indent=2)+'\n```\n')
    if closed:atomic_text(folder/'end-of-day.json',json.dumps(result,indent=2))
