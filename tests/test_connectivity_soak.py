from datetime import timedelta
import pytest
from ops.connectivity_soak import quote_check
from trader.calendar import Calendar
from trader.models import Unsafe, dt, iso

SESSION=Calendar().session(dt('2026-09-24T22:00:00Z'))


def quotes(when):
    return {s:dict(bid_price='100',ask_price='100.01',venue_bid_time=iso(when),venue_ask_time=iso(when),has_traded=True,state='active') for s in ('SPY','QQQ')}


def test_closed_market_certificate_does_not_imply_live_quote_freshness():
    now=SESSION.close+timedelta(hours=2);q=quotes(SESSION.close-timedelta(seconds=1))
    assert set(quote_check(q,now,SESSION,True))=={'SPY','QQQ'}
    with pytest.raises(Unsafe,match='Stale'):quote_check(q,now,SESSION,False)


def test_closed_market_cannot_bypass_active_session_freshness():
    now=SESSION.open+timedelta(hours=1)
    with pytest.raises(Unsafe,match='completed session'):quote_check(quotes(now),now,SESSION,True)


@pytest.mark.parametrize('change', ['missing','old','future','nan','crossed'])
def test_invalid_quote_responses_fail_closed(change):
    now=SESSION.close+timedelta(hours=2);q=quotes(SESSION.close-timedelta(seconds=1))
    if change=='missing':q.pop('QQQ')
    if change=='old':q['SPY']['venue_bid_time']=iso(SESSION.open)
    if change=='future':q['SPY']['venue_ask_time']=iso(now+timedelta(seconds=1))
    if change=='nan':q['SPY']['bid_price']='NaN'
    if change=='crossed':q['SPY']['bid_price']='101'
    with pytest.raises(Unsafe):quote_check(q,now,SESSION,True)
