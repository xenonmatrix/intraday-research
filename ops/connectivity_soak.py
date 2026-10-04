"""Read-only persisted-OAuth soak. Never binds an order capability or writes broker state.

Closed-market success means transport/account health, NOT tradable quote freshness.
The existing LaunchAgent is observed alongside this separate persistent MCP client.
"""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader.broker import connect, failure_details
from trader.calendar import Calendar
from trader.models import Config, D, NY, UTC, Unsafe, dt, iso, valid_quote
from trader.risk import reconcile
from trader.store import atomic_text

ROOT = Path(__file__).resolve().parent.parent


def quote_check(quotes, now, session, closed_market):
    if set(quotes) != {'SPY', 'QQQ'}:
        raise Unsafe('Missing SPY/QQQ response')
    evidence = {}
    for symbol, q in quotes.items():
        if not q.get('has_traded') or q.get('state') != 'active':
            raise Unsafe('Inactive quote')
        bid, ask = D(q['bid_price']), D(q['ask_price'])
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise Unsafe('Invalid quote book')
        times = [dt(q[k]) for k in ('venue_bid_time', 'venue_ask_time')]
        if closed_market:
            if not session or now < session.close:
                raise Unsafe('Closed-market check requires a completed session today')
            if any(t > now for t in times):
                raise Unsafe('Future quote timestamp; verify host clock')
            if any(t < session.close-timedelta(minutes=1) for t in times):
                raise Unsafe('Quote response predates the latest regular close')
        else:
            valid_quote(q, now, Config())
        evidence[symbol] = {k:q[k] for k in ('bid_price','ask_price','venue_bid_time','venue_ask_time')}
    return evidence


async def soak(root, duration, closed_market=False):
    if duration < 600:
        raise ValueError('Certification requires at least 600 seconds')
    runtime = root/'runtime'; started = datetime.now(UTC); start = time.monotonic()
    day = started.astimezone(NY).date().isoformat()
    db = sqlite3.connect(f'file:{runtime}/LIVE/state.sqlite3?mode=ro', uri=True)
    def state(key, default=None):
        row = db.execute('select value from state where key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def counts():
        return tuple(db.execute(f'select count(*) from {t}').fetchone()[0] for t in ('intents','fills'))
    baseline = counts(); failures = state('unattended_failures:'+day, 0)
    samples=[]; errors=[]; reconnect=False; clean_close=False; max_gap=0; account=None
    try:
        async with connect(runtime/'oauth.json') as broker:
            if broker.live_guard is not None or broker.cancel_guard is not None:
                raise Unsafe('Soak must not possess broker write capabilities')
            accounts = await broker.call('get_accounts', {})
            matches = [a for a in accounts['accounts'] if a and a.get('nickname')=='Agentic' and a.get('agentic_allowed') and a['account_number'].endswith(__import__('trader.configuration',fromlist=['load']).load(ROOT)['broker']['account_suffix'])]
            if len(matches)!=1: raise Unsafe('Agentic account identity ambiguous')
            account=matches[0]['account_number']; worker=None
            while time.monotonic()-start < duration:
                tick = time.monotonic()
                if not (runtime/'MAINTENANCE').exists(): raise Unsafe('Maintenance gate removed')
                if state('position') or counts()!=baseline: raise Unsafe('Local position/order ledger changed')
                snapshot=await broker.observe(account)
                power=reconcile(snapshot, account, datetime.now(UTC), Config(), {})
                if snapshot['positions'] or snapshot['orders']: raise Unsafe('Expected zero broker positions and orders')
                quotes=await broker.quotes(['SPY','QQQ']); now=datetime.now(UTC)
                evidence=quote_check(quotes, now, Calendar().session(now), closed_market)
                heartbeat=json.loads((runtime/'heartbeat.json').read_text())
                auth=json.loads((runtime/'authentication.json').read_text())
                if not 0<=time.time()-heartbeat['at']<=45: raise Unsafe('LaunchAgent heartbeat stale')
                if worker is None: worker=heartbeat['pid']
                if worker!=heartbeat['pid']: raise Unsafe('LaunchAgent restarted during soak')
                if auth['status']!='AUTHENTICATED_READ_VERIFIED' or (now-dt(auth['at'])).total_seconds()>45:
                    raise Unsafe('LaunchAgent authentication/reconciliation unhealthy')
                if state('unattended_failures:'+day,0)!=failures: raise Unsafe('LaunchAgent recorded a new connectivity failure')
                if samples:
                    gap=(now-dt(samples[-1]['at'])).total_seconds(); max_gap=max(max_gap,gap)
                    if gap>15: raise Unsafe('Read-only sampling gap exceeds 15 seconds')
                samples.append(dict(at=iso(now),connection_id=broker.connection_info['connection_id'],worker_pid=worker,
                    snapshot_at=snapshot['observed_at'],buying_power=str(power),account_value=snapshot['portfolio']['total_value'],
                    quotes=evidence,regular_market_quotes_fresh=not closed_market))
                if len(samples)%15==0:
                    print(json.dumps(dict(elapsed_seconds=round(time.monotonic()-start),cycles=len(samples),failures=failures)),flush=True)
                await asyncio.sleep(max(.1,2-(time.monotonic()-tick)))
        clean_close=True
        # Same independent OAuth file, a new connection, no browser or manual token input.
        async with connect(runtime/'oauth.json') as broker:
            snapshot=await broker.snapshot(account)
            reconcile(snapshot,account,datetime.now(UTC),Config(),{})
            if snapshot['positions'] or snapshot['orders']: raise Unsafe('Reconnect broker state changed')
            reconnect=True
    except Exception as exc:
        errors=failure_details(exc)
    finally:
        after=state('unattended_failures:'+day,0); final_counts=counts();db.close()
    result=dict(started_at=iso(started),ended_at=iso(datetime.now(UTC)),duration_seconds=round(time.monotonic()-start,3),
        passed=not errors and len(samples)>=150 and clean_close and reconnect and final_counts==baseline,
        scope='closed_market_connectivity' if closed_market else 'regular_market_readiness',
        regular_market_freshness_certified=not closed_market and not errors,
        launchagent_observed=True,probe_is_separate_client=True,sample_count=len(samples),max_sample_gap_seconds=max_gap,
        clean_close=clean_close,noninteractive_reconnect=reconnect,failure_count_before=failures,failure_count_after=after,
        intent_fill_counts_before=baseline,intent_fill_counts_after=final_counts,errors=errors,samples=samples)
    output=root/'research/results/connectivity-soak.json';atomic_text(output,json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2),flush=True)
    return result['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--duration',type=int,default=600);parser.add_argument('--closed-market',action='store_true')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(soak(ROOT,args.duration,args.closed_market)) else 1)
