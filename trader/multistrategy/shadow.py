"""Quote-observation shadow books. No broker reference or mutation capability.

Each strategy owns an independent virtual $50 book with the production risk
limits. Quotes establish hypothetical prices; they cannot prove real fills.
"""
import json,sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from ..models import D,Unsafe,dt,iso,valid_quote,down
from ..configuration import DEFAULTS,market_config
from ..persistent.config import Settings
from ..persistent.risk import proposal
from ..persistent.engine import Coordinator
from .features import FeatureEngine
from .regimes import classify


class ShadowObserver:
    mode='SHADOW'
    def __init__(self,path,registry,history=()):
        self.registry=registry;self.features=FeatureEngine(history);self.cfg=market_config(DEFAULTS)
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row
        # Refuse an actual broker ledger even if supplied accidentally.
        names={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names-{'shadow_books','shadow_used','shadow_events','shadow_meta'}:
            self.db.close();raise Unsafe('Shadow storage must be separate from production storage')
        self.db.executescript('CREATE TABLE IF NOT EXISTS shadow_books(id TEXT PRIMARY KEY,data TEXT NOT NULL);'
            'CREATE TABLE IF NOT EXISTS shadow_used(id TEXT PRIMARY KEY);'
            'CREATE TABLE IF NOT EXISTS shadow_events(id TEXT PRIMARY KEY,data TEXT NOT NULL);'
            'CREATE TABLE IF NOT EXISTS shadow_meta(id TEXT PRIMARY KEY,data TEXT NOT NULL);')
        self.db.commit()

    def close(self):self.db.close()

    def event(self,key,data):
        self.db.execute('INSERT OR IGNORE INTO shadow_events VALUES(?,?)',('shadow:'+key,json.dumps(dict(data,mode='SHADOW',registry_fingerprint=self.registry.fingerprint),sort_keys=True)))

    def feed(self,session,now,bars,quotes,*,healthy,code_commit='unknown'):
        if not healthy or not session or not session.contains(now):raise Unsafe('Shadow requires current healthy session observations')
        if set(quotes)!={'SPY','QQQ'}:raise Unsafe('Both observed quotes required')
        for q in quotes.values():valid_quote(q,now,self.cfg)
        frames={s:self.features.snapshot(s,bars.get(s,[]),session,now,bars) for s in ('SPY','QQQ')}
        last=self.db.execute("SELECT data FROM shadow_meta WHERE id='last_at'").fetchone()
        if last and now<=dt(json.loads(last[0])):raise Unsafe('Duplicate or backward shadow observation')
        with self.db:
            for sid,strategy in self.registry.objects.items():
                if not self.registry.permitted(sid,'SHADOW'):continue
                row=self.db.execute('SELECT data FROM shadow_books WHERE id=?',(sid,)).fetchone()
                book=json.loads(row[0]) if row else dict(day=session.day,cash='50',net='0',losses='0',attempts=0,positions={},pending={})
                if book['day']!=session.day:
                    if book['positions'] or book['pending']:raise Unsafe('Shadow position crossed session or unobserved close; reconcile simulation explicitly')
                    book.update(day=session.day,losses='0',attempts=0)
                positions=book['positions'];pending=book['pending']
                for sym,p in positions.items():p['mark']=str(D(p['quantity'])*D(quotes[sym]['ask_price']))
                exposure=sum(max(D(p['cost']),D(p['mark'])) for p in positions.values())+sum(D(p['proposal']['notional']) for p in pending.values())
                for sym,p in list(positions.items()):
                    bid,ask=valid_quote(quotes[sym],now,self.cfg);f=frames[sym].values;reason=None
                    if exposure>D(30) or D(book['losses'])>=D('1.5'):reason='EXPOSURE_OR_DAILY_LOSS'
                    elif now>=session.close-timedelta(minutes=session.liquidation_minutes):reason='SESSION_CLOSE'
                    elif bid<=D(p['stop']):reason='STOP'
                    elif bid>=D(p['target']):reason='TARGET'
                    elif now>=dt(p['deadline']):reason='TIME'
                    elif p['vwap_exit'] and f['price']<f['vwap']:reason='VWAP'
                    if reason:
                        exit_price=bid*(1-self.cfg.slip_bps/10000);pnl=D(p['quantity'])*exit_price-D(p['cost'])
                        book['cash']=str(D(book['cash'])+D(p['quantity'])*exit_price)
                        book['net']=str(D(book['net'])+pnl);book['losses']=str(D(book['losses'])+max(D(0),-pnl))
                        self.event(p['setup_id']+':exit',dict(p,exit=str(exit_price),exit_at=iso(now),exit_reason=reason,pnl=str(pnl),r_multiple=str(pnl/D(p['planned_risk'])),execution_basis='Observed quote plus assumed slippage; zero assumed fees'))
                        del positions[sym]
                for sym,p in list(pending.items()):
                    if now<dt(p['not_before']):continue
                    del pending[sym];signal=p['signal']
                    try:
                        if now>=dt(signal['valid_until']) or not session.entry_window(now):raise Unsafe('Shadow pending signal expired')
                        current=strategy.evaluate(frames[sym],session).signal
                        if not current or current.setup_id!=signal['setup_id']:raise Unsafe('Shadow setup no longer valid')
                        fresh=self.size(book,current,quotes[sym],now)
                        dollars=min(D(fresh['notional']),D(p['proposal']['notional']));q=down(dollars/D(fresh['entry']))
                        risk=q*(D(fresh['entry'])-D(fresh['stop'])+D(fresh['entry'])*self.cfg.slip_bps/10000)+2*self.cfg.fee_buffer
                        if risk>D(p['proposal']['planned_risk']):raise Unsafe('Shadow risk increased before fill')
                        cost=q*D(fresh['entry']);book['cash']=str(D(book['cash'])-cost)
                        positions[sym]=dict(signal,mode='SHADOW',run_id='shadow:'+session.day,code_commit=code_commit,registry_fingerprint=self.registry.fingerprint,
                            quantity=str(q),cost=str(cost),entry=fresh['entry'],entry_at=iso(now),planned_risk=str(risk),
                            target=str(D(fresh['entry'])+D(str(signal['target_r']))*(D(fresh['entry'])-D(fresh['stop']))),
                            deadline=iso(now+timedelta(minutes=signal['max_hold_minutes'])))
                        self.event(signal['setup_id']+':entry',positions[sym])
                    except Unsafe as exc:self.event(signal['setup_id']+':unfilled',dict(strategy=sid,symbol=sym,reason=str(exc),at=iso(now)))
                for sym,f in frames.items():
                    evaluation=strategy.evaluate(f,session);s=evaluation.signal
                    if not s:
                        self.event(sid+sym+f.values['last_completed_at']+':rejection',dict(strategy=sid,symbol=sym,at=iso(now),features=f.audit(),reason=evaluation.reason));continue
                    key='shadow:'+s.setup_id
                    if sym in positions or sym in pending or self.db.execute('SELECT 1 FROM shadow_used WHERE id=?',(key,)).fetchone():continue
                    try:
                        if book['attempts']>=5:raise Unsafe('Shadow session attempt cap')
                        proposed=self.size(book,s,quotes[sym],now)
                        self.db.execute('INSERT INTO shadow_used VALUES(?)',(key,));book['attempts']+=1
                        pending[sym]=dict(signal=s.audit(),proposal=proposed,not_before=iso(now+timedelta(seconds=2)))
                        self.event(s.setup_id+':candidate',dict(signal=s.audit(),regime=classify(f).__dict__,stage='SHADOW_RESERVED'))
                    except Unsafe as exc:self.event(s.setup_id+':risk:'+iso(now),dict(strategy=sid,symbol=sym,at=iso(now),stage='RISK',reason=str(exc)))
                self.db.execute('INSERT OR REPLACE INTO shadow_books VALUES(?,?)',(sid,json.dumps(book)))
            self.db.execute("INSERT OR REPLACE INTO shadow_meta VALUES('last_at',?)",(json.dumps(iso(now)),))

    def size(self,book,signal,quote,now):
        positions=book['positions'];pending=book['pending']
        if len(positions)+len(pending)>=3:raise Unsafe('Shadow position limit')
        exposure=sum(max(D(p['cost']),D(p.get('mark',p['cost']))) for p in positions.values())+sum(D(p['proposal']['notional']) for p in pending.values())
        reserved=sum(D(p['planned_risk']) for p in positions.values())+sum(D(p['proposal']['planned_risk']) for p in pending.values())
        daily=dict(loss_budget='1.5',gross_losses=book['losses'],loss_latched=D(book['losses'])>=D('1.5'))
        cfg=replace(self.cfg,target_r=D(str(signal.target_r)),max_hold_minutes=signal.max_hold_minutes,vwap_exit=signal.vwap_exit)
        return proposal(Settings(),daily,signal.candidate(),quote,D(book['cash'])-sum(D(p['proposal']['notional']) for p in pending.values()),now,
            max(D(0),D(30)+min(D(0),D(book['net']))-exposure),max(D(0),D('1.5')-D(book['losses'])-reserved),cfg)


class ShadowObservedCoordinator(Coordinator):
    """Optional observer of the same data; baseline broker path stays authoritative."""
    def __init__(self,*args,registry,**kwargs):
        super().__init__(*args,**kwargs)
        self.shadow=ShadowObserver(self.runtime/'shadow'/'observations.sqlite3',registry)

    async def observe(self,broker,bars=None,tradability=None,full=False):
        obs=await super().observe(broker,bars,tradability,full)
        try:self.shadow.feed(self.session(),self.now(),bars or {},obs['quotes'],healthy=obs['data_healthy'])
        except (Unsafe,KeyError,ValueError,TypeError) as exc:
            self.ledger.event(self.now(),'SHADOW_OBSERVATION_BLOCKED',dict(reason=str(exc)))
        return obs
