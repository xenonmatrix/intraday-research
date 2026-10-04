"""Date-scoped operator authorization for the existing controlled transition.

No order capabilities or strategy logic. Run under launchd with caffeinate.
Authorization is private runtime state, bound to date, config and operator holds.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader import configuration as C
from trader.calendar import Calendar
from trader.doctor import doctor, transition
from trader.models import UTC, NY, Unsafe, iso
from trader.store import atomic_text, process_lock


def scope(root, authorization, now):
    """Return WAIT/DUE/DONE; changes to authorization scope fail closed."""
    root=Path(root);runtime=root/'runtime'
    if now.astimezone(NY).date().isoformat()!=authorization['day']:
        raise Unsafe('Authorization applies only to its specified session date')
    config=C.load(root)
    if C.fingerprint(config)!=authorization['config_fingerprint']:
        raise Unsafe('Configuration changed after activation authorization')
    for name in ('STOP','TRANSITION','ENTRIES_DISABLED','LIQUIDATE'):
        if (runtime/name).exists():raise Unsafe(name+' prevents scheduled activation')
    db=sqlite3.connect(f'file:{runtime}/persistent.sqlite3?mode=ro',uri=True)
    try:
        settings=json.loads(db.execute("select value from kv where key='settings'").fetchone()[0])
        if settings['live_enabled']:return 'DONE'
        for row in db.execute("select data from events where id>? and kind='CONFIG_CHANGED'",(authorization['event_id'],)):
            if json.loads(row[0]).get('key')=='live_enabled':
                raise Unsafe('Operator changed LIVE permission after authorization')
    finally:db.close()
    for name,expected in authorization['holds'].items():
        actual=(runtime/name).read_text() if (runtime/name).exists() else None
        if actual!=expected:raise Unsafe('Operator hold changed: '+name)
    session=Calendar(config).session(now)
    if not session:raise Unsafe('No exchange session')
    if now>=session.close-timedelta(minutes=session.entry_cutoff_minutes):
        raise Unsafe('Entry cutoff reached; authorization expired')
    return 'DUE' if session.entry_window(now) else 'WAIT'


async def attempt(root, authorization, now):
    decision=scope(root,authorization,now)
    if decision!='DUE':return {'state':decision}
    result=await doctor(root,True)
    if not result['ready_for_transition']:
        return {'state':'WAIT','blockers':result['blockers']}
    # The command itself repeats all checks, validates watchdog KILL recovery,
    # holds entries through enablement, and rolls back on failed verification.
    await transition(root,activate=True,acknowledge=True,recover_kill=True)
    return {'state':'DONE','activated':True}


async def run(root, authorization_path):
    root=Path(root);path=Path(authorization_path)
    authorization=json.loads(path.read_text())
    status=root/'runtime'/('activation-'+authorization['day']+'-status.json')
    if status.exists() and json.loads(status.read_text()).get('state') in ('DONE','STOPPED'):
        return
    with process_lock(root/'runtime/session-activation.lock'):
        while True:
            now=datetime.now(UTC)
            try:
                result=await attempt(root,authorization,now)
            except Unsafe as exc:
                result={'state':'STOPPED','reason':str(exc)}
            except Exception as exc:
                # Operational errors never activate or erase state. Next call
                # rechecks scope; an interrupted TRANSITION stops the watcher.
                result={'state':'WAIT','error_type':type(exc).__name__}
            result.update(at=iso(datetime.now(UTC)),day=authorization['day'],
                          config_fingerprint=authorization['config_fingerprint'])
            atomic_text(status,json.dumps(result,indent=2))
            print(json.dumps(result),flush=True)
            if result['state'] in ('DONE','STOPPED'):return
            await asyncio.sleep(30)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--authorization',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(run(args.root.resolve(),args.authorization.resolve()))
