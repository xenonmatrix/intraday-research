"""Transport diagnostics without credentials, headers, arguments or response bodies."""
import fcntl
import json
from pathlib import Path
import re
import time
import traceback
from datetime import datetime, timezone
from mcp.shared.exceptions import McpError
import httpx
from .models import Unsafe


def redact(text):
    text=re.sub(r'https?://\S+','[URL]',str(text))
    text=re.sub(r'(?i)(bearer\s+|(?:access_token|refresh_token|client_secret|authorization|api_key|code)[\"\s:=]+)[^\s\",}]+',r'\1[REDACTED]',text)
    text=re.sub(r'\b[A-Za-z0-9_\-.]{32,}\b','[REDACTED]',text)
    return re.sub(r'\b\d{8,}\b','[REDACTED]',text)[:1000]


def exception_record(exc):
    out=dict(type=type(exc).__name__,message=redact(exc),frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)[-10:]])
    if isinstance(exc,McpError):out['mcp_code']=exc.error.code
    if isinstance(exc,httpx.HTTPStatusError):out['http_status']=exc.response.status_code
    if isinstance(exc,BaseExceptionGroup):out['children']=[exception_record(x) for x in exc.exceptions]
    elif exc.__cause__ is not None and exc.__cause__ is not exc:out['cause']=exception_record(exc.__cause__)
    return out


def event(runtime,kind,**fields):
    path=Path(runtime)/'mcp-diagnostics.jsonl'
    with path.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        f.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),kind=kind,**fields),default=str)+'\n')
        f.flush()


def hooks(runtime,connection_id,level="DEBUG",failure_context=None):
    mutations_started=set()
    async def request(req):
        info=dict(method=req.method,category='mcp' if req.url.path=='/mcp/trading' else 'oauth_or_discovery')
        if info['category']=='mcp' and req.method=='POST':
            try:
                payload=json.loads(req.content)
                info.update(rpc=payload.get('method'),request_id=payload.get('id'))
                if payload.get('method')=='tools/call':info['tool']=payload.get('params',{}).get('name')
            except (ValueError,httpx.RequestNotRead):pass
        req.extensions['research_diagnostic']=(time.monotonic(),info)
        async def trace(name,details):
            # Never log trace info wholesale: it can contain HTTP headers/body.
            if name.endswith('.send_request_headers.started') and info.get('tool') in ('place_equity_order','cancel_equity_order'):
                key=info.get('request_id')
                if key is None or key in mutations_started:
                    event(runtime,'MUTATION_REPLAY_BLOCKED',connection_id=connection_id,**info)
                    raise Unsafe('Automatic HTTP mutation replay blocked; reconcile actual order state')
                mutations_started.add(key)
            observed=('connection.connect_tcp.','connection.start_tls.',
                      'http11.send_request_body.','http11.receive_response_headers.',
                      'http2.send_request_body.','http2.receive_response_headers.')
            if name.startswith(observed) and (level=='DEBUG' or 'exception' in details):
                fields=dict(connection_id=connection_id,**info,phase=name)
                if 'exception' in details:fields['exception']=exception_record(details['exception'])
                if 'exception' in details and failure_context is not None:
                    failure_context.update(tool=info.get('tool'),http_phase=name,connection_id=connection_id)
                event(runtime,'HTTP_PHASE',**fields)
        req.extensions['trace']=trace
        event(runtime,'HTTP_REQUEST',connection_id=connection_id,**info)
    async def response(res):
        started,info=res.request.extensions.get('research_diagnostic',(time.monotonic(),{}))
        retry=res.headers.get('retry-after')
        event(runtime,'HTTP_RESPONSE_HEADERS',connection_id=connection_id,**info,status=res.status_code,
              http_version=res.http_version,
              header_seconds=round(time.monotonic()-started,4),content_type=res.headers.get('content-type'),
              retry_after=retry if retry and re.fullmatch(r'[0-9.]+',retry) else None,
              has_mcp_session_header='mcp-session-id' in res.headers)
    return dict(request=[request],response=[response])
