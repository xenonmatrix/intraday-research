from datetime import timedelta
import json
from pathlib import Path
import pandas as pd
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from trader.diagnostics import exception_record,redact
from trader.probe import active
from trader.models import dt,iso
from trader import replay,extended_replay
from ops.import_firstrate import validate_frame
from test_replay import day,signal


def test_extended_baseline_is_identical_to_frozen_replay():
    d=day();s=signal();f=dict(closes=[100],vwap=99)
    tape={d.session.day:{15:{'SPY':({'orb':s},f),'QQQ':({},f)}}}
    assert replay.run([d],tape,'orb')==extended_replay.run([d],tape,'orb')


def frame():
    return pd.DataFrame([['2023-03-10 09:30:00',100,101,99,100,100],['2023-03-13 09:30:00',100,101,99,100,100]],columns=['timestamp','open','high','low','close','volume'])


def test_vendor_localization_tracks_dst():
    f=validate_frame(frame())
    assert f.timestamp.iloc[0].utcoffset()==timedelta(hours=-5)
    assert f.timestamp.iloc[1].utcoffset()==timedelta(hours=-4)


@pytest.mark.parametrize('case',['duplicate','unsorted','zero_volume','bad_ohlc','nan','not_minute','nonexistent_dst'])
def test_vendor_invalid_data_rejected(case):
    f=frame()
    if case=='duplicate':f.loc[1,'timestamp']=f.loc[0,'timestamp']
    if case=='unsorted':f=f.iloc[::-1]
    if case=='zero_volume':f.loc[0,'volume']=0
    if case=='bad_ohlc':f.loc[0,'low']=102
    if case=='nan':f.loc[0,'close']=float('nan')
    if case=='not_minute':f.loc[0,'timestamp']='2023-03-10 09:30:01'
    if case=='nonexistent_dst':f.loc[0,'timestamp']='2023-03-12 02:30:00'
    with pytest.raises(Exception):validate_frame(f)


def test_mcp_diagnostic_retains_code_but_redacts_credentials():
    e=McpError(ErrorData(code=408,message='Timed out. Bearer verysecretcredential access_token=private-token https://host/path?code=secret'))
    r=exception_record(e)
    assert r['mcp_code']==408 and r['type']=='McpError'
    assert 'verysecretcredential' not in r['message'] and 'private-token' not in r['message'] and 'code=secret' not in r['message']


def test_probe_requires_maintenance_and_expires(tmp_path):
    now=dt('2026-09-24T23:00:00Z')
    (tmp_path/'READ_ONLY_SOAK.json').write_text(json.dumps(dict(read_only=True,start=iso(now),expires=iso(now+timedelta(seconds=60)))))
    assert not active(tmp_path,now)
    (tmp_path/'MAINTENANCE').touch()
    assert active(tmp_path,now)
    assert not active(tmp_path,now+timedelta(seconds=60))


def test_probe_never_changes_kill_or_approval(tmp_path):
    (tmp_path/'KILL').write_text('keep')
    (tmp_path/'live-approval.json').write_text('{"approved":false}')
    assert not active(tmp_path,dt('2026-09-24T23:00:00Z'))
    assert (tmp_path/'KILL').read_text()=='keep'
    assert json.loads((tmp_path/'live-approval.json').read_text())['approved'] is False


@pytest.mark.parametrize('tool',['place_equity_order','cancel_equity_order'])
def test_http_transport_cannot_replay_mutations_even_after_auth_or_goaway(tmp_path,tool):
    import asyncio,httpx
    from trader.diagnostics import hooks
    from trader.models import Unsafe
    h=hooks(tmp_path,'test-connection')
    async def check():
        req=httpx.Request('POST','https://agent.robinhood.com/mcp/trading',json={'method':'tools/call','id':4,'params':{'name':tool,'arguments':{'never':'logged'}}})
        await h['request'][0](req)
        await req.extensions['trace']('http2.send_request_headers.started',{})
        # HTTP/2 GOAWAY retry, or OAuth's resend of the same request: both blocked.
        await h['request'][0](req)
        with pytest.raises(Unsafe,match='replay blocked'):
            await req.extensions['trace']('http2.send_request_headers.started',{})
    asyncio.run(check())
    assert 'never' not in (tmp_path/'mcp-diagnostics.jsonl').read_text()
