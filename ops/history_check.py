"""Scan every locally reachable commit/tree/tag without printing secret values.

Verify remote refs separately. Optional exact-value comparison reads only local
private artifacts; none of their values are written to the audit output.
"""
import argparse,hashlib,json,re,sqlite3,subprocess
from pathlib import Path
try:
    from repository_check import scan_content
except ModuleNotFoundError:
    from ops.repository_check import scan_content


def git(*args):return subprocess.check_output(['git',*args])


def private_values(root):
    values=set()
    def walk(value,key=''):
        if isinstance(value,dict):
            for k,v in value.items():walk(v,k)
        elif isinstance(value,list):
            for v in value:walk(v,key)
        elif isinstance(value,str) and len(value)>=8 and re.search(r'token|secret|password|account_number|broker_id|execution_id|^id$|ref_id',key,re.I):values.add(value.encode())
    path=root/'runtime/oauth.json'
    if path.exists():walk(json.loads(path.read_text()))
    path=root/'runtime/persistent.sqlite3'
    if path.exists():
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            row=db.execute("SELECT value FROM kv WHERE key='account_number'").fetchone()
            if row:walk(json.loads(row[0]),'account_number')
            for table in ('orders','fills','trades'):
                for row in db.execute('SELECT * FROM '+table):
                    for value in row:
                        if isinstance(value,str):
                            try:walk(json.loads(value))
                            except (ValueError,TypeError):
                                if re.fullmatch(r'[a-f0-9-]{36}',value):values.add(value.encode())
        finally:db.close()
    return values


def forbidden(path):
    p=Path(path);parts=set(p.parts)
    return bool(parts&{'runtime','reports','research','logs','.venv','.git','.codex','.agents'} or
        p.name=='.env' or p.name.startswith('.env.') or p.suffix in ('.db','.sqlite','.sqlite3','.log','.pid','.pem','.key','.p12','.pfx','.har') or
        path in ('config/trader.json','config/position-recovery.json','config/multistrategy.json','live-session.json') or
        re.search(r'(oauth|credentials|session-cookie|access-token).*\.json$',p.name,re.I))


def audit(exact_private=False):
    root=Path(git('rev-parse','--show-toplevel').decode().strip());secrets=private_values(root) if exact_private else set()
    commits=git('rev-list','--all').decode().splitlines();blobs={};findings=[];paths=set()
    for commit in commits:
        for line in git('ls-tree','-r','-z',commit).split(b'\0'):
            if not line:continue
            header,name=line.split(b'\t',1);mode,kind,oid=header.decode().split();name=name.decode();paths.add(name)
            if forbidden(name):findings.append(dict(path=name,category='forbidden_private_artifact'))
            if kind!='blob' or mode not in ('100644','100755'):findings.append(dict(path=name,category='unreviewed_nonregular_file'));continue
            blobs.setdefault(oid,set()).add(name)
        content=git('cat-file','commit',commit)
        if any(value in content for value in secrets):findings.append(dict(object=commit,category='exact_private_material_in_commit_metadata'))
    for oid,names in blobs.items():
        content=git('cat-file','blob',oid)
        for name in names:
            findings.extend(scan_content(name,content))
            if content.startswith(b'SQLite format 3'):findings.append(dict(path=name,category='sqlite_content'))
            if any(value in content for value in secrets):findings.append(dict(path=name,category='exact_private_material'))
    for ref in git('for-each-ref','--format=%(refname)','refs/tags').decode().splitlines():
        if git('cat-file','-t',ref).strip()==b'tag':
            content=git('cat-file','tag',ref)
            if any(value in content for value in secrets):findings.append(dict(ref=ref,category='exact_private_material_in_tag_metadata'))
    return dict(commits=commits,commit_count=len(commits),unique_blobs=len(blobs),paths=len(paths),
        exact_local_private_comparison=exact_private,findings=findings,secret_audit='FAIL' if findings else 'PASS',
        sensitive_database_runtime_files_tracked=any(forbidden(p) for p in paths),
        scope='All locally reachable history; remote ref equality and visibility must be verified separately',
        limitations='Pattern and known-value audit cannot prove absence of every possible unknown secret; no values printed')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--exact-local-private',action='store_true');args=p.parse_args()
    result=audit(args.exact_local_private);print(json.dumps(result,indent=2));return int(bool(result['findings']))


if __name__=='__main__':raise SystemExit(main())
