import argparse
import asyncio
import json
from pathlib import Path
from .broker import connect
from .models import Mode, Unsafe
from .service import run, supervise
from .store import atomic_text
from .operations import authentication_status, enable_live, request_stop, status


def main():
    parser = argparse.ArgumentParser(description="Intraday research and persistent guarded execution; disabled by default")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "supervise"):
        p = subs.add_parser(name)
        p.add_argument("--mode", choices=[m.value for m in Mode], default="READ_ONLY")
        if name == "run":
            p.add_argument("--once", action="store_true")
    subs.add_parser("auth")
    status_parser=subs.add_parser("status")
    status_parser.add_argument("--json",action="store_true")
    subs.add_parser("migrate-persistent")
    subs.add_parser("export-analysis")
    p=subs.add_parser("config")
    cs=p.add_subparsers(dest="config_command",required=True)
    cs.add_parser('show');cs.add_parser('validate');cs.add_parser('path');cs.add_parser('init')
    reset=cs.add_parser('reset');reset.add_argument('--dry-run',action='store_true')
    cp=cs.add_parser('set');cp.add_argument('key');cp.add_argument('value')
    from .configuration import ALIASES as COMMANDS
    for name in COMMANDS:
        cp=cs.add_parser(name);cp.add_argument("value")
    p=subs.add_parser('doctor');p.add_argument('--broker',action='store_true');p.add_argument('--json',action='store_true')
    p=subs.add_parser('production');p.add_argument('action',choices=['check','activate']);p.add_argument('--acknowledge-risk',action='store_true');p.add_argument('--recover-kill',action='store_true')
    p=subs.add_parser('launchagent');p.add_argument('action',choices=['generate']);p.add_argument('--output',type=Path)
    p=subs.add_parser('clock-diagnostics');p.add_argument('--samples',type=int,default=10);p.add_argument('--interval',type=int,default=10)
    p=subs.add_parser('stop')
    p=subs.add_parser("live")
    p.add_argument("action",choices=["enable","disable"])
    p=subs.add_parser("maintenance")
    p.add_argument("action",choices=["on","off"])
    p=subs.add_parser("read-only")
    p.add_argument("action",choices=["on","off"])
    p=subs.add_parser("auth-status")
    p.add_argument("--check",action="store_true")
    p=subs.add_parser("enable-live")
    p.add_argument("--date",required=True)
    p.add_argument("--approve",action="store_true")
    for command in ("kill","disable-entries","liquidate"):
        p = subs.add_parser(command)
        p.add_argument("--reason", default="Operator emergency stop")
    subs.add_parser("clear-kill")
    args = parser.parse_args()
    root = args.root.resolve()
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    try:
        if args.command in ("run", "supervise"):
            mode = Mode(args.mode)
            if mode==Mode.LIVE and not (runtime/'PERSISTENT').exists():raise Unsafe('Legacy LIVE runtime retired; initialize and migrate persistent configuration')
            if args.command == "run":
                asyncio.run(run(root, mode, args.once))
            else:
                supervise(root, mode)
        elif args.command == "auth":
            async def auth():
                async with connect(runtime / "oauth.json", interactive=True) as broker:
                    result = await broker.call("get_accounts", {})
                    print("Authenticated read-only client. Accounts visible:", len(result["accounts"] or []))
            asyncio.run(auth())
        elif args.command == "migrate-persistent":
            from .persistent.cli import migrate
            migrate(root)
        elif args.command == "export-analysis":
            from .persistent.export import export
            print(export(root))
        elif args.command == "config":
            from .config_cli import configure
            configure(root,args.config_command,getattr(args,'value',None),getattr(args,'key',None),getattr(args,'dry_run',False))
        elif args.command=='doctor':
            from .doctor import doctor,display
            result=asyncio.run(doctor(root,args.broker));display(result,args.json)
            if not result['ready']:raise SystemExit(1)
        elif args.command=='production':
            from .doctor import transition
            asyncio.run(transition(root,args.action=='activate',args.acknowledge_risk,args.recover_kill))
        elif args.command=='launchagent':
            from .operator_tools import launchagent
            print(launchagent(root,args.output))
        elif args.command=='clock-diagnostics':
            from .operator_tools import clock_diagnostics
            clock_diagnostics(root,args.samples,args.interval)
        elif args.command=='stop':
            atomic_text(runtime/'STOP','Operator stop requested; does not liquidate\n')
            print('Stop requested; no liquidation implied')
        elif args.command == "live":
            from .persistent.cli import live_switch
            live_switch(root,args.action=='enable')
        elif args.command in ('maintenance','read-only'):
            marker=runtime/('MAINTENANCE' if args.command=='maintenance' else 'READ_ONLY')
            if args.action=='on':atomic_text(marker,'Operator '+args.command+' requested\n')
            else:
                if (runtime/'PERSISTENT').exists():raise Unsafe('Use production activate; individual protection removal bypasses transition checks')
                marker.unlink(missing_ok=True)
            print(args.command+' '+args.action+'; other safety gates unchanged')
        elif args.command == "enable-live":
            if (runtime/'PERSISTENT').exists():raise Unsafe('Date-specific enable-live is retired for the persistent runtime')
            asyncio.run(enable_live(root,args.date,args.approve))
        elif args.command == "auth-status":
            print(json.dumps(asyncio.run(authentication_status(root,args.check)),indent=2))
        elif args.command in ("kill","disable-entries","liquidate"):
            request_stop(root,args.command,args.reason)
        elif args.command == "clear-kill":
            # Does not reset daily loss latches, unresolved orders, or data gates.
            if (runtime/'PERSISTENT').exists():raise Unsafe('Use production activate --recover-kill after readiness checks')
            (runtime / "KILL").unlink(missing_ok=True)
            print("Operator kill cleared; all independent safety gates remain active.")
        else:
            if (runtime/'PERSISTENT').exists():
                from .persistent.cli import status as persistent_status
                persistent_status(root,args.json)
            else:status(root)
    except Unsafe as exc:
        parser.exit(2, str(exc)+"\n")


if __name__ == "__main__":
    main()
