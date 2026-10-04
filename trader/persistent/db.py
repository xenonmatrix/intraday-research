"""SQLite ownership, reservations and immutable execution ledger for multiple positions."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json, sqlite3, uuid
from pathlib import Path
from ..models import D, UTC, NY, Unsafe, iso, dt
from .config import Settings

TERMINAL={'FILLED','CANCELLED','REJECTED','ABORTED'}
ACTIVE=('RESERVED','DISPATCHING','SUBMITTED','PARTIAL','AMBIGUOUS')

class Ledger:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(day TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_symbol ON trades(symbol) WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,intent_key TEXT UNIQUE NOT NULL,trade_id TEXT NOT NULL,
          side TEXT NOT NULL,status TEXT NOT NULL,broker_id TEXT UNIQUE,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
        ''')
        with self.tx():
            if self.get('settings') is None:self.put('settings',asdict(Settings()))
    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield;self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def put(self,key,value):
        self.db.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,default=str)))
    def settings(self):return Settings(**self.get('settings')).validate()
    def configure(self,key,value):
        if key not in asdict(Settings()) or key=='account_suffix':raise Unsafe('Unknown or protected setting')
        with self.tx():
            values=asdict(self.settings());values[key]=value;s=Settings(**values).validate()
            self.put('settings',asdict(s));self.event(datetime.now(UTC),'CONFIG_CHANGED',dict(key=key,value=value))
        return s
    def event(self,now,kind,data):
        data=dict(data,config_fingerprint=self.get('config_fingerprint','legacy-unversioned'),strategy_fingerprint=self.get('strategy_fingerprint','legacy-unversioned'))
        self.db.execute('INSERT INTO events(at,kind,data) VALUES(?,?,?)',(iso(now),kind,json.dumps(data,default=str)))
    def session(self,day):
        row=self.db.execute('SELECT data FROM sessions WHERE day=?',(day,)).fetchone()
        return json.loads(row[0]) if row else None
    def save_session(self,day,data):
        self.db.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(day) DO UPDATE SET data=excluded.data',(day,json.dumps(data,default=str)))
    def ensure_session(self,day):
        s=self.settings();v=self.session(day)
        if v is None:
            v=dict(day=day,loss_budget=str(s.daily_loss),budget_base=s.max_total_exposure,budget_percent=s.max_daily_loss_percent,
                   gross_losses='0',net='0',attempts=0,completed=0,loss_latched=False,
                   opening_healthy=False,connectivity_halt=False,outages=0,recovery_active=True,successes=0,entry_cutoff_latched=False,liquidation_due=False,closed=False,config_fingerprint=self.get('config_fingerprint','legacy-unversioned'),strategy_fingerprint=self.get('strategy_fingerprint','legacy-unversioned'))
        # Raising configured exposure/percentage cannot refill this session's budget.
        v['loss_budget']=str(min(D(v['loss_budget']),s.daily_loss))
        if D(v['gross_losses'])>=D(v['loss_budget']):v['loss_latched']=True
        self.save_session(day,v);return v
    def checkpoint(self,session,now):
        if not session:return
        from datetime import timedelta
        with self.tx():
            data=self.ensure_session(session.day)
            for key,boundary in [('entry_cutoff_latched',session.close-timedelta(minutes=session.entry_cutoff_minutes)),
                                 ('liquidation_due',session.close-timedelta(minutes=session.liquidation_minutes)),('closed',session.close)]:
                if now>=boundary and not data.get(key):
                    data[key]=True;self.event(now,'SESSION_'+key.upper(),dict(day=session.day))
            self.save_session(session.day,data)

    def trades(self,open_only=True):
        sql='SELECT * FROM trades'+(" WHERE status='OPEN'" if open_only else '')+' ORDER BY rowid'
        return [dict(json.loads(r['data']),id=r['id'],symbol=r['symbol'],day=r['day'],status=r['status']) for r in self.db.execute(sql)]
    def trade(self,tid):
        r=self.db.execute('SELECT * FROM trades WHERE id=?',(tid,)).fetchone()
        if not r:raise Unsafe('Unknown trade identity')
        return dict(json.loads(r['data']),id=r['id'],symbol=r['symbol'],day=r['day'],status=r['status'])
    def save_trade(self,t):
        self.db.execute('UPDATE trades SET status=?,data=? WHERE id=?',(t['status'],json.dumps(t,default=str),t['id']))
    def orders(self,active_only=True):
        rows=self.db.execute('SELECT * FROM orders ORDER BY rowid')
        return [dict(r,data=json.loads(r['data'])) for r in rows if not active_only or r['status'] not in TERMINAL]
    def order(self,oid):
        r=self.db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not r:raise Unsafe('Unknown order intent')
        return dict(r,data=json.loads(r['data']))
    def update_order(self,oid,status,broker_id=None,data=None):
        row=self.order(oid)
        self.db.execute('UPDATE orders SET status=?,broker_id=?,data=? WHERE id=?',
                        (status,broker_id or row['broker_id'],json.dumps(data if data is not None else row['data']),oid))
    def recover(self,now):
        with self.tx():
            for o in self.orders():
                if o['status']=='RESERVED':
                    self.update_order(o['id'],'ABORTED')
                    if o['side']=='buy':
                        t=self.trade(o['trade_id'])
                        if D(t['quantity'])==0:t['status']='CLOSED';self.save_trade(t)
                elif o['status']=='DISPATCHING':self.update_order(o['id'],'AMBIGUOUS')
            for t in self.trades():
                t['recovery_exit']=True
                if t.get('position_recovery'):
                    t['position_recovery'].update(state='VERIFIED_RISK_REDUCTION',exit_reason='WORKER_PROCESS_FAILURE')
                self.save_trade(t)
            self.put('health',{});self.put('observation',{})
            day=now.astimezone(NY).date().isoformat();daily=self.ensure_session(day)
            daily.update(recovery_active=True,successes=0,stable_since=iso(now),opening_healthy=False,opening_reason='Worker restart: fresh reconciliation and opening-equivalent health required')
            self.save_session(day,daily)
            self.event(now,'RESTART_RECOVERY',dict(open_positions=len(self.trades()),pending=len(self.orders()),failure_class='WORKER_PROCESS_FAILURE'))
    def exposure(self,quotes=None):
        current=reserved=risk=D(0)
        for t in self.trades():
            qty=D(t['quantity']);cost=D(t['cost']);mark=cost
            if qty and quotes and t['symbol'] in quotes:mark=max(cost,qty*D(quotes[t['symbol']]['ask_price']))
            current+=mark
            risk+=D(t['planned_risk'])
        for o in self.orders():
            if o['side']=='buy':reserved+=max(D(0),D(o['data']['notional'])-D(o['data'].get('filled_cost','0')))
        return current,reserved,risk
    def reserve(self,day,symbol,key,proposal,now,check):
        """Risk check + slot, dollars, loss allowance and attempt claim are ONE transaction."""
        with self.tx():
            if self.db.execute('SELECT 1 FROM orders WHERE intent_key=?',(key,)).fetchone():raise Unsafe('Duplicate candidate intent')
            session=self.ensure_session(day);settings=self.settings()
            check(settings,session,self.trades(),self.orders())
            tid=str(uuid.uuid4());oid=str(uuid.uuid4())
            trade=dict(id=tid,symbol=symbol,day=day,status='OPEN',quantity='0',cost='0',entry_fees='0',entry=None,
                       entered=None,stop=proposal['stop'],target=proposal['target'],time_exit=None,realized_pnl='0',
                       planned_risk=proposal['planned_risk'],notional=proposal['notional'],setup_id=proposal['setup_id'],
                       recovery_exit=False,exit_reason=None,entry_order_id=oid,
                       config_fingerprint=self.get('config_fingerprint','legacy-unversioned'),strategy_fingerprint=self.get('strategy_fingerprint','legacy-unversioned'),
                       config_snapshot=__import__('trader.configuration',fromlist=['public']).public(self.get('production_config') or {}),
                       target_r=proposal.get('target_r','2'),max_hold_minutes=proposal.get('max_hold_minutes',30),vwap_exit=proposal.get('vwap_exit',True))
            if proposal.get('strategy_signal'):
                trade['strategy_signal']=proposal['strategy_signal']
                trade['strategy_fingerprint']=proposal['strategy_signal']['fingerprint']
            data=dict(proposal,symbol=symbol,checked_at=iso(now),filled_cost='0',filled_quantity='0')
            self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?)',(tid,symbol,day,'OPEN',json.dumps(trade)))
            self.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?)',(oid,key,tid,'buy','RESERVED',None,json.dumps(data)))
            session['attempts']+=1;self.save_session(day,session)
            self.event(now,'ENTRY_RESERVED',dict(order_id=oid,trade_id=tid,proposal=proposal))
            return oid
    def reserve_exit(self,tid,reason,now):
        with self.tx():
            t=self.trade(tid)
            if t['status']!='OPEN' or D(t['quantity'])<=0:raise Unsafe('No owned position to exit')
            if any(o['trade_id']==tid for o in self.orders()):raise Unsafe('Position has pending order; reconcile before exit')
            oid=str(uuid.uuid4());key='exit:'+tid+':'+str(self.db.execute('SELECT count(*) FROM orders WHERE trade_id=?',(tid,)).fetchone()[0])
            data=dict(symbol=t['symbol'],quantity=t['quantity'],reason=reason,checked_at=iso(now),filled_quantity='0')
            self.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?)',(oid,key,tid,'sell','RESERVED',None,json.dumps(data)))
            self.event(now,'EXIT_RESERVED',dict(order_id=oid,trade_id=tid,reason=reason));return oid
    def abort(self,oid,reason,now):
        with self.tx():
            o=self.order(oid)
            if o['status']!='RESERVED':raise Unsafe('Cannot abort possibly dispatched order')
            self.update_order(oid,'ABORTED')
            t=self.trade(o['trade_id'])
            if o['side']=='buy' and D(t['quantity'])==0:t['status']='CLOSED';self.save_trade(t)
            self.event(now,'ORDER_ABORTED',dict(order_id=oid,reason=reason))
    def close(self):self.db.close()
