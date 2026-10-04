"""Market-open regression tests; no real broker requests or order submission."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from trader.broker import Robinhood
from trader.calendar import Calendar
from trader.models import dt, iso
from trader.orb import evaluate
from test_pre_run_audit import opening


class HistoricalClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args, **kwargs):
        assert name == 'get_equity_historicals'
        self.calls.append(args)
        start, end = dt(args['start_time']), dt(args['end_time'])
        assert end > start
        rows = [b for b in opening()
                if dt(b['begins_at']) + timedelta(minutes=5) <= end]
        return SimpleNamespace(isError=False, structuredContent={'data': {'results': [
            dict(symbol=s, interval='5minute', bars=rows) for s in args['symbols']]}})


@pytest.mark.parametrize('time', [
    '13:29:59', '13:30:00', '13:30:00.706825', '13:34:59.999999',
])
def test_no_request_before_first_completed_bar(time):
    now = dt('2026-09-25T' + time + 'Z')
    session = Calendar().session(now)
    client = HistoricalClient()
    broker = Robinhood(client)
    result = asyncio.run(broker.bars(iter(['SPY', 'QQQ']), session, now))
    assert result == {'SPY': [], 'QQQ': []}
    assert client.calls == []
    assert broker.cycle_error is None
    assert evaluate('SPY', result['SPY'], session, now)['state'] == 'WAIT_OR'


@pytest.mark.parametrize('time,end,count', [
    ('13:35:00', '13:35:00', 1),
    ('13:39:59.999999', '13:35:00', 1),
    ('13:40:00', '13:40:00', 2),
    ('13:44:59.999999', '13:40:00', 2),
    ('13:45:00', '13:45:00', 3),
    ('13:45:01', '13:45:00', 3),
])
def test_september_25_completed_bar_boundaries(time, end, count):
    now = dt('2026-09-25T' + time + 'Z')
    session = Calendar().session(now)
    client = HistoricalClient()
    result = asyncio.run(Robinhood(client).bars(['SPY', 'QQQ'], session, now))
    request = client.calls[0]
    assert request['start_time'] == iso(session.open)
    assert request['end_time'] == iso(dt('2026-09-25T' + end + 'Z'))
    assert request['bounds'] == 'regular' and request['adjustment_type'] == 'none'
    assert len(result['SPY']) == count
    state = evaluate('SPY', result['SPY'], session, now)
    assert state['state'] == ('ARMED' if count == 3 else 'WAIT_OR')
    assert session.entry_window(now) is (count == 3)


@pytest.mark.parametrize('day,close', [
    ('2026-09-25', '20:00:00'), ('2026-11-27', '18:00:00'),
])
def test_end_is_capped_at_calendar_close_including_early_close(day, close):
    now = dt(day + 'T23:00:00Z')
    session = Calendar().session(now)
    client = HistoricalClient()
    asyncio.run(Robinhood(client).bars(['SPY'], session, now))
    assert client.calls[0]['end_time'] == iso(dt(day + 'T' + close + 'Z'))
