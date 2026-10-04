"""Installed-package vendor schema validation, independent of local ops scripts."""
import pandas as pd
import numpy as np
from ..models import Unsafe


def validate_frame(frame):
    if list(frame.columns)!=['timestamp','open','high','low','close','volume']:raise Unsafe('Unexpected vendor schema')
    times=pd.to_datetime(frame.timestamp,format='%Y-%m-%d %H:%M:%S',errors='raise').dt.tz_localize('America/New_York',ambiguous='raise',nonexistent='raise')
    if times.duplicated().any() or not times.is_monotonic_increasing or (times.dt.second!=0).any():raise Unsafe('Duplicate, unordered or off-minute timestamps')
    data=frame[['open','high','low','close','volume']].apply(pd.to_numeric,errors='raise')
    if not np.isfinite(data.to_numpy()).all() or (data.volume<=0).any() or (data.low<=0).any() or (data.low>data[['open','close']].min(axis=1)).any() or (data.high<data[['open','close']].max(axis=1)).any():raise Unsafe('Malformed OHLCV')
    data['timestamp']=times
    return data
