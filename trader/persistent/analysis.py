"""Causal diagnostic features. No additional strategy filters or fitted parameters."""
from datetime import timedelta
from ..models import D, dt

def features(symbol,candidate,bars,quotes,now,session):
    completed=[b for b in bars.get(symbol,[]) if session and session.open<=dt(b['begins_at']) and dt(b['begins_at'])+timedelta(minutes=5)<=min(now,session.close)]
    result=dict(feature_version=1,missing=[],vwap_relationship=None,breakout_extension=None,
                volume=None,relative_volume=None,relative_volume_basis='latest completed bar / preceding same-session bars (not time-of-day historical RVOL)',
                volatility=None,momentum=None,recent_bar_structure=None,relative_strength=None,broad_market_confirmation=None,
                minutes_since_open=(now-session.open).total_seconds()/60 if session else None)
    try:
        q=quotes[symbol];bid,ask=D(q['bid_price']),D(q['ask_price']);mid=(bid+ask)/2
        result['spread_bps']=str((ask-bid)/mid*10000)
        if candidate.get('vwap'):result['vwap_relationship']=str(mid/D(candidate['vwap'])-1)
        if candidate.get('trigger'):
            risk=D(candidate['trigger'])-D(candidate['invalidation'])
            result['breakout_extension']=dict(dollars=str(ask-D(candidate['trigger'])),risk_units=str((ask-D(candidate['trigger']))/risk) if risk>0 else None)
    except (KeyError,TypeError,ValueError,ArithmeticError):result['missing'].append('quote_features')
    if completed:
        last=completed[-1];vol=[D(b['volume']) for b in completed];closes=[D(b['close_price']) for b in completed]
        result['volume']=str(vol[-1]);result['cumulative_volume']=str(sum(vol))
        if len(vol)>1 and sum(vol[:-1])>0:result['relative_volume']=str(vol[-1]/(sum(vol[:-1])/len(vol[:-1])))
        ranges=[(D(b['high_price'])-D(b['low_price']))/D(b['close_price']) for b in completed[-6:]]
        result['volatility']=dict(mean_six_bar_range_fraction=str(sum(ranges)/len(ranges)))
        result['momentum']=dict(session_return=str(closes[-1]/D(completed[0]['open_price'])-1),three_bar_return=str(closes[-1]/closes[-4]-1) if len(closes)>3 else None)
        span=D(last['high_price'])-D(last['low_price'])
        result['recent_bar_structure']=dict(body_fraction=str(abs(D(last['close_price'])-D(last['open_price']))/span) if span else None,
                                           close_location=str((D(last['close_price'])-D(last['low_price']))/span) if span else None)
        result['breakout_quality']=dict(last_completed_close_above_range=D(last['close_price'])>D(candidate['opening_high']) if candidate.get('opening_high') else None)
    else:result['missing'].append('completed_bars')
    returns={}
    for s in ('SPY','QQQ'):
        rows=[b for b in bars.get(s,[]) if session and session.open<=dt(b['begins_at']) and dt(b['begins_at'])+timedelta(minutes=5)<=min(now,session.close)]
        if rows:returns[s]=D(rows[-1]['close_price'])/D(rows[0]['open_price'])-1
    if len(returns)==2:
        result['relative_strength']=str(returns['QQQ']-returns['SPY'])
        result['broad_market_confirmation']=dict(proxy='SPY and QQQ completed-bar session returns',both_positive=all(x>0 for x in returns.values()))
    else:result['missing'].append('cross_symbol_confirmation')
    return result
