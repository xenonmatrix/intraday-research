"""Portable setup helpers and macOS query-only clock diagnostics."""
import json,os,plistlib,re,statistics,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from .models import Unsafe
from . import configuration as C

def launchagent(root,output=None):
    root=Path(root).resolve();C.load(root)
    if sys.platform!='darwin':raise Unsafe('LaunchAgents are supported only on macOS')
    out=Path(output) if output else root/'runtime/com.local.intraday-paper.plist'
    data={'Label':'com.local.intraday-paper','ProgramArguments':[str(root/'.venv/bin/python'),'-m','trader','--root',str(root),'supervise','--mode','LIVE'],'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':{'PathState':{str(root/'runtime/STOP'):False}},'ThrottleInterval':10,'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'},'StandardOutPath':str(root/'runtime/launchd.out.log'),'StandardErrorPath':str(root/'runtime/launchd.err.log')}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(plistlib.dumps(data));out.chmod(0o600)
    return str(out)

def clock_diagnostics(root,samples=10,interval=10):
    if not 1<=samples<=60 or interval<5:raise Unsafe('Use 1..60 samples, interval at least five seconds')
    cfg=C.load(root);out=Path(root)/'reports/clock';out.mkdir(parents=True,exist_ok=True);rows=[]
    for i in range(samples):
        start=time.monotonic();row={'at':datetime.now(timezone.utc).isoformat(),'source':'time.apple.com'}
        try:
            p=subprocess.run(['/usr/bin/sntp','-d','-t',str(cfg['clock']['sample_timeout_seconds']),'time.apple.com'],capture_output=True,text=True,timeout=10)
            raw=p.stdout+p.stderr
            m=re.search(r'^([+-][0-9.]+)\s+\+/-\s+([0-9.]+)\s+time\.apple\.com\s+(\S+)',raw,re.M)
            if p.returncode or not m:raise Unsafe('No valid NTP response')
            selected=raw.split('selected:')[-1];delays=re.findall(r'^\s*delay:.*\(([-+0-9.e]+)\)',selected,re.M)
            offset,uncertainty=float(m[1])*1000,float(m[2])*1000
            row.update(offset_ms=offset,uncertainty_ms=uncertainty,combined_ms=abs(offset)+uncertainty,network_rtt_ms=float(delays[-1])*1000,source_address=m[3])
        except (OSError,ValueError,IndexError,subprocess.TimeoutExpired,Unsafe) as exc:row['error']=type(exc).__name__+': '+str(exc)
        rows.append(row);print(json.dumps(row),flush=True)
        if i<samples-1:time.sleep(max(0,interval-(time.monotonic()-start)))
    good=[r for r in rows if 'error' not in r];summary={'samples':len(good),'failed':len(rows)-len(good),'policy':'abs(offset)+uncertainty <= '+cfg['clock']['max_combined_bound_ms']+' ms','policy_changed':False}
    for k in ('offset_ms','uncertainty_ms','combined_ms','network_rtt_ms'):
        values=[r[k] for r in good]
        summary[k]={'median':statistics.median(values),'maximum':max(values),'standard_deviation':statistics.pstdev(values)} if values else None
    summary['max_absolute_offset_ms']=max((abs(r['offset_ms']) for r in good),default=None)
    summary['passing_samples']=sum(r['combined_ms']<=float(cfg['clock']['max_combined_bound_ms']) for r in good)
    summary['note']='RTT standard deviation is delay variation, not measured clock error or the NTP peer-jitter estimator.'
    filename=out/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json');filename.write_text(json.dumps({'samples':rows,'summary':summary},indent=2))
    print(json.dumps(summary,indent=2));print('Saved '+str(filename));return summary
