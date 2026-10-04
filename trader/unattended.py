"""Persistent unattended session gates; no broker writes in this module."""
from datetime import timedelta
import fcntl
import json
import os
from .models import NY, Unsafe, dt, iso
from .store import atomic_text


def audit(runtime, now, kind, **payload):
    """Append-only, fsynced operational journal shared by worker/supervisor."""
    with open(runtime/'unattended.jsonl', 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(dict(at=iso(now),pid=os.getpid(),kind=kind,**payload),default=str)+'\n')
        f.flush()
        os.fsync(f.fileno())


class Unattended:
    def __init__(self, policy):
        self.policy = policy
        self.root = policy.runtime/'unattended'/policy.day
        self.root.mkdir(parents=True,exist_ok=True)

    def mark(self, name, now, **details):
        path = self.root/(name+'.json')
        if not path.exists():
            atomic_text(path,json.dumps(dict(at=iso(now),**details)))
            audit(self.policy.runtime,now,name,**details)

    def health(self, now, healthy, snapshot_at=None, reason=None):
        content = json.dumps(dict(at=iso(now),pid=os.getpid(),healthy=healthy,snapshot_at=snapshot_at,reason=reason))
        atomic_text(self.policy.runtime/'readiness.json', content)
        if now < self.policy.session.open:
            atomic_text(self.policy.runtime/'preopen-readiness.json', content)

    def failure(self, store, now, reason):
        self.health(now,False,reason=reason)
        audit(self.policy.runtime,now,'CONNECTIVITY_OR_RECONCILIATION_FAILURE',reason=reason)
        # One outage is one episode, not one failure per request/retry. No entry
        # is allowed during recovery. Sustained outages still latch at 60s.
        if now.astimezone(NY).date().isoformat() == self.policy.day and now < self.policy.session.close:
            key = 'unattended_failures:'+self.policy.day
            episode_key = 'connectivity_episode:'+self.policy.day
            with store.db:
                episode = store.get(episode_key,{})
                new = not episode.get('active')
                if new:
                    episode = dict(active=True,started=iso(now),attempts=0)
                episode.update(last_failure=iso(now),attempts=episode['attempts']+1,successes=0,reason=reason)
                count = store.get(key,0)+int(new)
                store.put(key,count)
                store.put(episode_key,episode)
            atomic_text(self.root/'recovering.json',json.dumps(episode))
            duration = max(0,(now-dt(episode['started'])).total_seconds())
            if count >= 3 or duration >= 60:
                self.mark('halt-connectivity',now,failures=count,outage_seconds=duration,reason=reason)

    def success(self, store, now):
        key='connectivity_episode:'+self.policy.day
        episode=store.get(key,{})
        if not episode.get('active'):
            return
        episode['successes']=episode.get('successes',0)+1
        if episode['successes'] >= 3 and (now-dt(episode['last_failure'])).total_seconds() >= 30:
            episode.update(active=False,recovered_at=iso(now))
            audit(self.policy.runtime,now,'CONNECTIVITY_RECOVERED',episode=episode)
        store.save(key,episode)
        atomic_text(self.root/'recovering.json',json.dumps(episode))

    def checkpoint(self, now, worker_pid, heartbeat):
        """Supervisor wall-clock checks run even while a broker call hangs."""
        session = self.policy.session
        recovering=self.root/'recovering.json'
        if recovering.exists():
            episode=json.loads(recovering.read_text())
            try:
                health=json.loads((self.policy.runtime/'readiness.json').read_text())
            except (OSError,ValueError):
                health={}
            if (episode.get('active') and not health.get('healthy')
                    and (now-dt(episode['started'])).total_seconds()>=60):
                self.mark('halt-connectivity',now,reason='Broker outage exceeded 60 seconds',outage_seconds=(now-dt(episode['started'])).total_seconds())
        if now >= session.open and not (self.root/'opening.json').exists():
            try:
                health = json.loads((self.policy.runtime/'preopen-readiness.json').read_text())
                latest = json.loads((self.policy.runtime/'readiness.json').read_text())
                healthy = (0 <= (now-session.open).total_seconds() <= 5
                    and latest['healthy'] is True and latest['pid'] == worker_pid
                    and health['healthy'] is True and health['pid'] == worker_pid
                    and 0 <= (session.open-dt(health['at'])).total_seconds() <= 15
                    and 0 <= (session.open-dt(health['snapshot_at'])).total_seconds() <= 15
                    and heartbeat['pid'] == worker_pid
                    and 0 <= now.timestamp()-heartbeat['at'] <= 15
                    and heartbeat['state'] in ('HEALTHY','ENTRIES_BLOCKED','POLLING'))
                if healthy:
                    from .host_health import require_host
                    require_host(self.policy.runtime,now)
            except (Unsafe,OSError,ValueError,KeyError,TypeError):
                healthy = False
            self.mark('opening',now,healthy=healthy)
            if not healthy:
                self.mark('halt-opening',now,reason='No healthy, recent pre-open reconciliation and worker heartbeat at opening checkpoint')
        if now >= session.close-timedelta(hours=1):
            self.mark('halt-cutoff',now,reason='Final-hour entry cutoff')
        if now >= session.close-timedelta(minutes=15):
            self.mark('liquidation-due',now,reason='Final fifteen minutes')
        if now >= session.close:
            self.mark('expired',now,reason='Authorized regular session closed')

    def entry_check(self):
        recovering=self.root/'recovering.json'
        if recovering.exists() and json.loads(recovering.read_text()).get('active'):
            raise Unsafe('Broker connection recovery has not stabilized')
        try:
            opening = json.loads((self.root/'opening.json').read_text())
        except (OSError,ValueError):
            raise Unsafe('Unattended opening health check not passed')
        if opening.get('healthy') is not True:
            raise Unsafe('Unattended opening health check failed for the session')
        if list(self.root.glob('halt-*.json')) or (self.root/'expired.json').exists():
            raise Unsafe('Unattended session entries permanently disabled')

    def liquidation_due(self, now):
        return now >= self.policy.session.close-timedelta(minutes=15)

    def keep_awake(self, now):
        if (self.policy.runtime/'STOP').exists():
            return False
        # Observation permission does not grant any broker mutation capability.
        try:
            plan=json.loads((self.policy.runtime/'READ_ONLY_SESSION.json').read_text())
            if ((self.policy.runtime/'MAINTENANCE').exists() and plan.get('read_only') is True
                    and plan.get('session_date')==self.policy.day
                    and dt(plan['start']) <= now < min(dt(plan['expires']),self.policy.session.close+timedelta(minutes=5))):
                return True
        except (OSError,ValueError,KeyError,TypeError,Unsafe):
            pass
        try:
            self.policy.approval()
        except (Unsafe,OSError,ValueError):
            return False
        return now < self.policy.session.close and not (self.policy.runtime/'STOP').exists()
