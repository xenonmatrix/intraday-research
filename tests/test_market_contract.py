import json
from pathlib import Path
from trader.calendar import Calendar
from trader.models import dt
from trader.strategy import evaluate


def test_synthetic_robinhood_schema_and_full_session_bars():
    sample=json.loads((Path(__file__).parent/'fixtures'/'robinhood_market_sample.json').read_text())
    now=dt('2024-02-06T21:00:00Z')
    session=Calendar().session(now)
    assert len(sample['historicals']['results'])==2
    for row in sample['historicals']['results']:
        assert row['interval']=='5minute' and len(row['bars'])==78
        state=evaluate(row['symbol'],row['bars'],session,now)
        assert state['opening_high'] and state['opening_low'] and state['vwap']
    for row in sample['quotes']['results']:
        q=row['quote']
        assert q['symbol'] in ('SPY','QQQ')
        assert dt(q['venue_bid_time']) and dt(q['venue_ask_time'])
