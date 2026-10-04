"""Validate vendor CSV, retain provenance, and build strict regular-session minutes."""
from datetime import datetime,timezone
import hashlib,json,sys
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from trader.calendar import Calendar
from trader.models import NY,iso
from trader.extended_replay import load_data

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'research/extended';NORMAL=OUT/'normalized'


def validate_frame(frame):
    expected=['timestamp','open','high','low','close','volume']
    if list(frame.columns)!=expected:raise ValueError('Unexpected CSV columns')
    times=pd.to_datetime(frame.timestamp,format='%Y-%m-%d %H:%M:%S',errors='raise').dt.tz_localize('America/New_York',ambiguous='raise',nonexistent='raise')
    if times.duplicated().any():raise ValueError('Duplicate minute timestamps')
    if not times.is_monotonic_increasing:raise ValueError('Unsorted timestamps')
    if (times.dt.second!=0).any():raise ValueError('Not minute-start timestamps')
    data=frame[['open','high','low','close','volume']].apply(pd.to_numeric,errors='raise')
    import numpy as np
    if not np.isfinite(data.to_numpy()).all():raise ValueError('Non-finite OHLCV')
    if (data.volume<=0).any():raise ValueError('Zero/negative volume cannot be imputed')
    if ((data.low<=0)|(data.low>data[['open','close']].min(axis=1))|(data.high<data[['open','close']].max(axis=1))).any():raise ValueError('Inconsistent OHLC')
    data['timestamp']=times
    return data


def main():
    NORMAL.mkdir(parents=True,exist_ok=True)
    protocol=json.loads((OUT/'protocol.json').read_text());calendar=Calendar();results=[];audit={}
    for symbol in ('SPY','QQQ'):
        path=OUT/'raw'/f'{symbol}.csv';frame=validate_frame(pd.read_csv(path));rows=[];outside=0;day_stats={}
        # Timestamp localization changes EST/EDT through DST; never fixed-offset ET.
        for x in frame.itertuples(index=False):
            when=x.timestamp.to_pydatetime();session=calendar.session(when)
            if not session or not session.contains(when):outside+=1;continue
            if not protocol['start']<=session.day<protocol['end_exclusive']:raise ValueError('Outside frozen date range')
            rows.append(dict(begins_at=iso(when),open_price=str(x.open),high_price=str(x.high),low_price=str(x.low),close_price=str(x.close),volume=float(x.volume),session='reg',interpolated=False))
            stats=day_stats.setdefault(session.day,dict(bars=0,volume=0,open=float(x.open),close=0))
            stats['bars']+=1;stats['volume']+=float(x.volume);stats['close']=float(x.close)
        results.append(dict(symbol=symbol,interval='minute',bounds='regular',bars=rows))
        audit[symbol]=dict(raw_rows=len(frame),regular_rows=len(rows),outside_regular_rows=outside,duplicates=0,invalid_ohlcv=0,
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),sessions=day_stats,
            timestamp_basis='America/New_York, bar start, DST-aware',adjustments='Vendor free sample has no adjustment flag or corporate-action file. Preserve observed price basis; no inferred rebasing or forward adjustment. Corporate-action audit reported separately.')
    # Preserve unknown vendor adjustment basis explicitly; never label it split-adjusted.
    payload=dict(request=dict(interval='minute',bounds='regular',adjustment_type='as_provided'),source='FirstRate free sample',source_adjustment_basis='UNSPECIFIED_BY_SAMPLE',data=dict(results=results))
    (NORMAL/'firstrate.json').write_text(json.dumps(payload,separators=(',',':')))
    days,manifest=load_data(NORMAL,protocol['start'],protocol['end_exclusive'])
    (OUT/'results/data-quality.json').write_text(json.dumps(dict(created_at=datetime.now(timezone.utc).isoformat(),symbols=audit,manifest=manifest),indent=2))
    clean=[];five=[]
    for day in days:
        for symbol in ('SPY','QQQ'):
            for i,b in enumerate(day.minutes[symbol]):
                clean.append(dict(timestamp=iso(day.session.open+pd.Timedelta(minutes=i)),symbol=symbol,open=b.o,high=b.h,low=b.l,close=b.c,volume=b.v))
            for i,b in enumerate(day.five[symbol]):
                five.append(dict(timestamp=iso(day.session.open+pd.Timedelta(minutes=5*i)),symbol=symbol,open=b.o,high=b.h,low=b.l,close=b.c,volume=b.v))
    pd.DataFrame(clean).to_csv(OUT/'clean-minute.csv.gz',index=False)
    pd.DataFrame(five).to_csv(OUT/'clean-five-minute.csv.gz',index=False)
    print(json.dumps({k:v for k,v in manifest.items() if k not in ('files','excluded')},indent=2))
    print('Excluded sessions:',len(manifest['excluded']))


if __name__=='__main__':main()
