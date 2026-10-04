"""Read-only macOS clock/power evidence. No broker access or system-setting changes."""
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from .models import UTC, Unsafe, dt, iso
from .store import atomic_text

MAX_CLOCK_ERROR = .050
MAX_CLOCK_AGE = 120
MAX_HOST_AGE = 15
# Permanent entry requirement on either AC or battery power.
MIN_BATTERY_PERCENT = 50


def parse_power(output):
    sources = re.findall(r"^Now drawing from '(AC Power|Battery Power)'$", output, re.M)
    source = sources[0] if len(sources) == 1 else None
    percentages = re.findall(r'^\s*-InternalBattery[^\n]*?\s(\d{1,3})%;[^\n]*\bpresent: true\b', output, re.M)
    percent = int(percentages[0]) if len(percentages) == 1 else None
    if percent is not None and not 0 <= percent <= 100:
        percent = None
    return dict(ac_power=(source == 'AC Power') if source else None,
                battery_percent=percent)


def parse_ntp(output):
    matches = re.findall(r'^([+-][0-9.]+)\s+\+/-\s+([0-9.]+)\s+time\.apple\.com\b', output, re.M)
    if not matches:
        raise Unsafe('No valid Apple time-server measurement')
    offset, uncertainty = map(float, matches[-1])
    if not all(math.isfinite(x) for x in (offset, uncertainty)) or uncertainty < 0:
        raise Unsafe('Invalid time-server measurement')
    return dict(offset_seconds=offset, uncertainty_seconds=uncertainty)


def problems(record, now, config=None):
    host=(config or {}).get('host',{})
    clock=(config or {}).get('clock',{})
    errors = []
    try:
        if not 0 <= (now-dt(record['at'])).total_seconds() <= host.get('health_max_age_seconds',MAX_HOST_AGE):
            errors.append('Host observation stale or future')
        if type(record.get('ac_power')) is not bool:
            errors.append('Verified power source required')
        percent = record.get('battery_percent')
        if type(percent) is not int or not host.get('min_battery_percent',MIN_BATTERY_PERCENT) <= percent <= 100:
            errors.append('Verified battery at least '+str(host.get('min_battery_percent',MIN_BATTERY_PERCENT))+'% required for new entries')
        if host.get('require_ac_power',False) and record.get('ac_power') is not True: errors.append('AC power required')
        if host.get('require_sleep_prevention',False) and record.get('sleep_prevention') is not True: errors.append('Verified sleep prevention required')
        if host.get('require_lid_open',True) and record.get('lid_open') is not True: errors.append('Lid must be open')
        c = record['clock']
        if not 0 <= (now-dt(c['at'])).total_seconds() <= clock.get('sample_max_age_seconds',MAX_CLOCK_AGE):
            errors.append('Clock measurement stale or future')
        offset, uncertainty = float(c['offset_seconds']), float(c['uncertainty_seconds'])
        if not all(math.isfinite(x) for x in (offset, uncertainty)) or uncertainty < 0 or abs(offset)+uncertainty > float(clock.get('max_combined_bound_ms','50'))/1000:
            errors.append('Clock error/uncertainty exceeds '+str(clock.get('max_combined_bound_ms','50'))+' milliseconds')
    except (KeyError, TypeError, ValueError):
        errors.append('Host/clock evidence incomplete')
    return errors


def require_host(runtime, now=None):
    now = now or datetime.now(UTC)
    try:
        record = json.loads((Path(runtime)/'host-health.json').read_text())
        supervisor = json.loads((Path(runtime)/'supervisor.json').read_text())
        config = active_config(Path(runtime))
        errors = problems(record, now, config)
        if config:
            from .configuration import fingerprint
            if record.get('config_fingerprint')!=fingerprint(config):errors.append('Host configuration fingerprint mismatch')
        if record.get('supervisor_pid') != supervisor['pid']:
            errors.append('Host evidence belongs to another supervisor')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Unsafe('Host health unavailable; new entries blocked') from exc
    if errors: raise Unsafe('; '.join(errors))


async def command(*args, timeout=3):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except BaseException:
        if p.returncode is None: p.kill()
        await p.wait()
        raise
    if p.returncode: raise Unsafe('Host command failed: '+Path(args[0]).name)
    return out.decode(errors='replace')


def active_config(runtime):
    import sqlite3
    dbpath=Path(runtime)/'persistent.sqlite3'
    if not dbpath.exists():return None
    db=sqlite3.connect(f'file:{dbpath}?mode=ro',uri=True)
    try:
        row=db.execute("SELECT value FROM kv WHERE key='production_config'").fetchone()
        return json.loads(row[0]) if row else None
    finally:db.close()


async def monitor(runtime, supervisor_pid):
    runtime = Path(runtime); stop = asyncio.Event(); loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(sig, stop.set)
    config=active_config(runtime)
    host=(config or {}).get('host',{});clock=(config or {}).get('clock',{})
    record = dict(supervisor_pid=supervisor_pid, clock={},
                  entry_min_battery_percent=host.get('min_battery_percent',MIN_BATTERY_PERCENT))
    if config:
        from .configuration import fingerprint
        record['config_fingerprint']=fingerprint(config)
    async def clock_loop():
        while not stop.is_set():
            try:
                # Query only: intentionally no -s/-S clock-setting flag.
                output = await command('/usr/bin/sntp', '-t', str(clock.get('sample_timeout_seconds',2)), 'time.apple.com', timeout=max(10,clock.get('sample_timeout_seconds',2)+5))
                record['clock'] = dict(at=iso(datetime.now(UTC)), **parse_ntp(output))
            except Exception as exc:
                record['clock'] = dict(error=str(exc))
            try: await asyncio.wait_for(stop.wait(), clock.get('sample_interval_seconds',60))
            except TimeoutError: pass
    task = asyncio.create_task(clock_loop())
    try:
        while not stop.is_set() and os.getppid() == supervisor_pid:
            try:
                power, lid = await asyncio.gather(command('/usr/bin/pmset','-g','batt'), command('/usr/sbin/ioreg','-r','-k','AppleClamshellState','-d','4'))
                record.update(**parse_power(power),
                              lid_open=bool(re.search(r'"AppleClamshellState"\s*=\s*No\b', lid)))
            except Exception as exc:
                record.update(ac_power=None, battery_percent=None, lid_open=None, observation_error=str(exc))
            if config:
                try:
                    evidence=json.loads((runtime/'supervisor-health.json').read_text())
                    record['sleep_prevention']=bool(evidence['sleep_prevention'] and evidence['pid']==supervisor_pid and 0<=time.time()-evidence['at']<=host.get('health_max_age_seconds',15))
                except (OSError,ValueError,KeyError):record['sleep_prevention']=False
            now = datetime.now(UTC); record['at'] = iso(now)
            record['problems'] = problems(record, now, config); record['healthy'] = not record['problems']
            atomic_text(runtime/'host-health.json', json.dumps(record, indent=2))
            try: await asyncio.wait_for(stop.wait(), host.get('poll_seconds',5))
            except TimeoutError: pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(monitor(Path(sys.argv[1]), int(sys.argv[2])))
