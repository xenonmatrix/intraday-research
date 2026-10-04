import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
from .broker import connect, TokenFile, failure_types
from .live_policy import LivePolicy
from .models import Config, UTC, Unsafe, iso
from .risk import reconcile
from .store import Store, atomic_text

LABEL = "com.local.intraday-paper"


async def authentication_status(root, check=False):
    runtime = Path(root)/"runtime"
    from .configuration import load
    selector=load(root,required=False)['broker']['account_suffix']
    credentials = TokenFile(runtime/"oauth.json")
    result = {"checked_at":iso(datetime.now(UTC)),"credential_file_present":credentials.path.exists(),"status":"NOT_AUTHENTICATED"}
    if await credentials.get_tokens():
        result["status"] = "CREDENTIALS_PRESENT_NOT_VERIFIED"
        if check:
            try:
                async with connect(runtime/"oauth.json") as broker:
                    data = await broker.call("get_accounts",{})
                    accounts = [a for a in data["accounts"] if a and a.get("agentic_allowed") and a["account_number"].endswith(selector)]
                    if not selector or len(accounts) != 1:
                        raise Unsafe("Expected Agentic account not available")
                    snapshot = await broker.snapshot(accounts[0]["account_number"])
                    result.update(status="AUTHENTICATED_READ_VERIFIED",account_suffix=selector,
                        connection=broker.connection_info,
                        account_tools=[name for name in ("get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders") if name in broker.catalog],
                        portfolio=snapshot["portfolio"], position_count=len(snapshot["positions"]),
                        order_count=len(snapshot["orders"]))
                result["clean_close"] = True
            except Exception as e:
                result.update(status="AUTHENTICATION_OR_CONNECTION_FAILED",errors=failure_types(e),
                              reason=str(e) if isinstance(e,Unsafe) else "Read-only connection check failed")
    if check:
        atomic_text(runtime/"auth-check.json",json.dumps(result,indent=2))
    return result


async def enable_live(root, date, approved=False):
    """Operator-only activation. Never called by service/startup/test on real runtime."""
    raise Unsafe('Date-specific LIVE activation retired; use persistent production readiness')
    root=Path(root)
    policy=LivePolicy(root)
    now=datetime.now(UTC)
    if not approved or date != policy.day or now >= policy.session.close:
        raise Unsafe("Explicit --approve and the unexpired reviewed session date are required")
    for f in ("STOP","KILL","LIQUIDATE","ENTRIES_DISABLED"):
        if (policy.runtime/f).exists():
            raise Unsafe(f"Activation blocked by persistent {f}; do not reset safety stops blindly")
    status_path=policy.runtime/"LIVE"/"status.json"
    heartbeat_path=policy.runtime/"heartbeat.json"
    if not status_path.exists() or not heartbeat_path.exists():
        raise Unsafe("Disarmed LIVE monitor must already be running")
    status=json.loads(status_path.read_text())
    heartbeat=json.loads(heartbeat_path.read_text())
    if time.time()-heartbeat["at"] > 45 or heartbeat["state"] in ("DISCONNECTED","STOPPED","STARTING"):
        raise Unsafe("Monitor is not currently healthy and connected")
    if status.get("position") or status.get("unresolved_orders"):
        raise Unsafe("Activation requires a flat, reconciled experiment")
    async with connect(policy.runtime/"oauth.json") as broker:
        data=await broker.call("get_accounts",{})
        accounts=[a for a in data["accounts"] if a and a.get("agentic_allowed") and a["account_number"].endswith(policy.settings["account_suffix"])]
        if len(accounts)!=1:
            raise Unsafe("Expected account missing")
        snapshot=await broker.snapshot(accounts[0]["account_number"])
        reconcile(snapshot,accounts[0]["account_number"],datetime.now(UTC),Config())
    # No orders are placed by activation, even if invoked during the session.
    atomic_text(policy.runtime/"live-approval.json",json.dumps({"approved":True,"session_date":date,"policy_sha256":policy.digest,"account_suffix":policy.settings["account_suffix"],"approved_at":iso(now)},indent=2))
    print(f"Authorized {date} only. Calendar and all live risk gates remain mandatory.")


def request_stop(root, action, reason):
    runtime=Path(root)/"runtime"
    atomic_text(runtime/"ENTRIES_DISABLED",reason+"\n")
    if action=="liquidate":
        atomic_text(runtime/"LIQUIDATE",reason+"\n")
        print("Liquidation requested. No direct order sent; monitor must reconcile and confirm every exit.")
    elif action=="kill":
        atomic_text(runtime/"STOP",reason+"\n")
        # STOP causes foreground supervisors/workers to exit; bootout removes the
        # installed job immediately and prevents launchd from restarting it.
        if os.uname().sysname=="Darwin":
            result=subprocess.run(["launchctl","bootout",f"gui/{os.getuid()}/{LABEL}"],capture_output=True,text=True)
            if result.returncode:
                print("LaunchAgent not unloaded (may not be loaded); STOP is set for the supervisor.")
        print("Process stop requested. This does NOT liquidate holdings. Verify status is stopped.")
    else:
        print("New entries disabled persistently. Position monitoring and authorized exits continue.")


def status(root):
    root=Path(root)
    runtime=root/"runtime"
    for mode in ("LIVE","PAPER","READ_ONLY"):
        path=runtime/mode/"status.json"
        if path.exists():
            print(path.read_text())
    path=runtime/"heartbeat.json"
    if path.exists():
        h=json.loads(path.read_text())
        print("Monitor heartbeat:",json.dumps(dict(h,age_seconds=round(time.time()-h["at"],2))))
    path=runtime/"authentication.json"
    if path.exists():
        print("Monitor authentication:",path.read_text())
    path=runtime/'host-health.json'
    if path.exists():
        print('Host health:',path.read_text())
    print("Stops:",json.dumps({f:(runtime/f).exists() for f in ("ENTRIES_DISABLED","LIQUIDATE","KILL","STOP")}))
    try:
        p=LivePolicy(root)
        p.approval()
        print("Authorization:",p.day,"ONLY; no authorization outside its regular session")
    except (Unsafe,FileNotFoundError) as exc:
        print("LIVE authorization:", "EXPIRED" if 'EXPIRED' in str(exc) else "DISARMED")
