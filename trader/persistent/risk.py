"""Deterministic final veto; reservation checks run inside BEGIN IMMEDIATE."""
from dataclasses import replace
from ..models import D, Config, Unsafe, dt, down, valid_quote
from ..risk import eligible,size

CFG=replace(Config(),universe=('SPY','QQQ'))

def proposal(settings,session,candidate,quote,power,now,capital_available=None,risk_available=None,cfg=None):
    cfg=cfg or CFG
    cap=min(D(settings.capital_per_trade),D(capital_available)) if capital_available is not None else D(settings.capital_per_trade)
    risk_cap=min(settings.trade_loss,D(risk_available)) if risk_available is not None else settings.trade_loss
    if cap<1 or risk_cap<=2*cfg.fee_buffer:raise Unsafe('Insufficient remaining capital or planned-loss allowance')
    cfg=replace(cfg,position_cap=cap,trade_risk=risk_cap,
                daily_loss=D(session['loss_budget']),reserve=D(0))
    qty,entry,notional=size(candidate,quote,max(D(0),power-D(settings.buying_power_reserve)),cap,
                             dict(gross_losses=session['gross_losses'],latched=session['loss_latched']),now,cfg)
    dollars=down(notional,'0.01')
    planned=dollars/entry*(entry-D(candidate['invalidation'])+entry*cfg.slip_bps/10000)+2*cfg.fee_buffer
    result=dict(symbol=candidate['symbol'],setup_id=candidate['setup_id'],notional=str(dollars),quantity_estimate=str(qty),
                entry=str(entry),stop=candidate['invalidation'],target=candidate['target'],planned_risk=str(planned),reward_risk=str(cfg.target_r),target_r=str(cfg.target_r),max_hold_minutes=cfg.max_hold_minutes,vwap_exit=cfg.vwap_exit)
    if candidate.get('signal'):result['strategy_signal']=candidate['signal']
    return result

def check_reservation(ledger,settings,session,trades,orders,proposal,now,health_check,excluding=None,candidate_config=None):
    from ..configuration import applied,market_config
    cfg=market_config(applied(ledger))
    health_check(now)
    obs=ledger.get('observation',{})
    if not obs or not 0<=(now-dt(obs['at'])).total_seconds()<=applied(ledger)['execution']['preflight_max_age_seconds']:raise Unsafe('Fresh reconciliation/quote observation required')
    if any(o['status']=='AMBIGUOUS' for o in orders):raise Unsafe('Ambiguous order blocks new entries')
    others=[o for o in orders if o['id']!=excluding]
    if any(o['status']=='DISPATCHING' for o in others):raise Unsafe('Submission not acknowledged; reconcile before another entry')
    sym=proposal['symbol']
    if sym not in cfg.universe:raise Unsafe('Only SPY/QQQ long equity entries permitted')
    if not eligible(obs.get('tradability',{}).get(sym,{})):raise Unsafe('Symbol/fractional trading unavailable')
    if not excluding and any(t['symbol']==sym for t in trades):raise Unsafe('One open or pending position per symbol')
    if len(trades)+(0 if excluding else 1)>settings.max_open_positions:raise Unsafe('Open plus pending position limit')
    if session['loss_latched']:raise Unsafe('Daily loss latch')
    if not excluding and session['attempts']>=settings.max_entry_attempts_per_day:raise Unsafe('Daily entry attempt limit')
    if excluding and session['attempts']>settings.max_entry_attempts_per_day:raise Unsafe('Entry attempt limit reduced')
    n,risk=D(proposal['notional']),D(proposal['planned_risk'])
    if not 1<=n<=D(settings.capital_per_trade) or not 0<risk<=settings.trade_loss:raise Unsafe('Per-trade capital/planned-risk cap')
    for t in trades:
        if D(t['quantity']):valid_quote(obs['quotes'][t['symbol']],now,cfg)
    current,reserved,allocated_risk=ledger.exposure(obs.get('quotes'))
    import json
    cumulative_net=sum((D(json.loads(r[0])['net']) for r in ledger.db.execute('SELECT data FROM sessions')),D(0))
    allocation=max(D(0),D(settings.max_total_exposure)+min(D(0),cumulative_net))
    if current+reserved+(D(0) if excluding else n)>allocation:raise Unsafe('Experiment allocation depleted by realized losses')
    if current+reserved+(D(0) if excluding else n)>D(settings.max_total_exposure):raise Unsafe('Aggregate current/reserved exposure cap')
    if D(session['gross_losses'])+allocated_risk+(D(0) if excluding else risk)>D(session['loss_budget']):raise Unsafe('Daily loss allowance including reserved planned losses')
    # Subtract all pending entry reservations even if broker buying power already
    # reflects holds: conservative double reservation cannot overspend cash.
    pending_fees=cfg.fee_buffer*sum(o['side']=='buy' for o in orders)
    available=D(obs['power'])-D(settings.buying_power_reserve)-reserved-pending_fees
    if available<(D(0) if excluding else n+cfg.fee_buffer):raise Unsafe('Unleveraged buying power / $20 reserve')
    c=obs['candidates'].get(sym,{})
    if c.get('state')!='ARMED' or c.get('setup_id')!=proposal['setup_id']:raise Unsafe('No current unexpired ORB signal')
    if candidate_config:cfg=candidate_config(c)
    fresh=globals()['proposal'](settings,session,c,obs['quotes'][sym],D(obs['power']),now,cfg=cfg)
    if n>D(fresh['notional']):raise Unsafe('Entry sizing/price changed since reservation')
    entry=D(fresh['entry'])
    current_risk=n/entry*(entry-D(c['invalidation'])+entry*cfg.slip_bps/10000)+2*cfg.fee_buffer
    if current_risk>risk:raise Unsafe('Planned risk increased since reservation')
