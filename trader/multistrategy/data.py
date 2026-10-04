"""Strict original-vendor import; exclusions are explicit, never imputed."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
from pathlib import Path
import pandas as pd

from ..calendar import Calendar
from ..models import dt, iso, Unsafe


@dataclass(frozen=True)
class MarketDay:
    session: object
    minutes: dict
    five: dict


def aggregate(rows, minutes=5):
    if len(rows) % minutes: raise Unsafe('Incomplete aggregation group')
    out=[]
    for i in range(0,len(rows),minutes):
        chunk=rows[i:i+minutes]
        if any(dt(b['begins_at'])!=dt(chunk[0]['begins_at'])+timedelta(minutes=j) for j,b in enumerate(chunk)):
            raise Unsafe('Noncontiguous aggregation input')
        out.append(dict(begins_at=chunk[0]['begins_at'],open_price=chunk[0]['open_price'],
            high_price=str(max(float(b['high_price']) for b in chunk)),
            low_price=str(min(float(b['low_price']) for b in chunk)),close_price=chunk[-1]['close_price'],
            volume=sum(float(b['volume']) for b in chunk),session='reg'))
    return out


def load_vendor(directory,start='2022-09-30',end='2023-09-30'):
    from .validation_data import validate_frame
    calendar=Calendar();sessions={}
    for day in pd.date_range(start,end,inclusive='left'):
        session=calendar.session(dt(str(day.date())+'T16:00:00Z'))
        if session:sessions[session.day]=session
    symbols={};manifest={'source':'FirstRate original CSV','files':{},'excluded':[],'repairs':[],
                        'historical_quotes':False,'adjustment_basis':'UNSPECIFIED','holdout_untouched':False}
    for sym in ('SPY','QQQ'):
        path=Path(directory)/(sym+'.csv')
        frame=validate_frame(pd.read_csv(path))
        manifest['files'][sym]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':len(frame)}
        grouped={}
        for row in frame.itertuples(index=False):
            now=row.timestamp.to_pydatetime();key=now.date().isoformat();s=sessions.get(key)
            if not s or not s.contains(now):continue
            grouped.setdefault(key,[]).append(dict(begins_at=iso(now),open_price=str(row.open),high_price=str(row.high),
                low_price=str(row.low),close_price=str(row.close),volume=float(row.volume),session='reg'))
        symbols[sym]=grouped
    days=[]
    for key,s in sessions.items():
        expected=int((s.close-s.open).total_seconds()/60);group={};errors={}
        for sym in ('SPY','QQQ'):
            rows=symbols[sym].get(key,[])
            wanted={s.open+timedelta(minutes=i) for i in range(expected)}
            actual={dt(b['begins_at']) for b in rows}
            if len(rows)!=expected or actual!=wanted:errors[sym]={'rows':len(rows),'missing':len(wanted-actual),'extra':len(actual-wanted)}
            else:group[sym]=rows
        if errors:manifest['excluded'].append({'day':key,'reasons':errors});continue
        days.append(MarketDay(s,group,{sym:aggregate(rows) for sym,rows in group.items()}))
    manifest.update(expected_sessions=len(sessions),complete_sessions=len(days),first=days[0].session.day if days else None,
                    last=days[-1].session.day if days else None,coverage=len(days)/len(sessions) if sessions else 0)
    return days,manifest
