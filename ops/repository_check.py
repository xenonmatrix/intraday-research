"""Publication-oriented secret/path scan. Prints paths and finding categories, never values.
Uses git's ignore matcher in an isolated temporary repository, without initializing
or modifying this project's Git state. It cannot certify existing Git history.
"""
import argparse,hashlib,json,os,re,subprocess,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
SKIP={'.git','.venv','venv','__pycache__','.pytest_cache','runtime','reports','research','build','dist'}
PATTERNS={
 'personal_absolute_path':r'/Users/[A-Za-z0-9_.-]+/',
 'bearer_literal':r'(?i)bearer\s+[A-Za-z0-9._~-]{20,}',
 'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'token_assignment':r'''(?i)["'](?:access_token|refresh_token|client_secret|api_key)["']\s*[:=]\s*["'][A-Za-z0-9_./+~-]{24,}["']''',
 'provider_token':r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})',
 'jwt_literal':r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}',
 'credential_url':r'https?://[^\s/:@]+:[^\s/@]+@',
 'cookie_literal':r'''(?i)["'](?:cookie|session_cookie|authorization)["']\s*:\s*["'][A-Za-z0-9=; _./+~-]{24,}["']''',
}
# Exact synthetic redaction-test fixture only; changed literals are rescanned.
TEST_FIXTURES={'tests/test_extended_research.py':{'77377cb387da0387ad177cc487c46aca2a2c08076db5bf8c4a4ebf312720e952'}}

def scan_content(name,content):
    findings=[]
    try:text=content.decode('utf-8')
    except UnicodeError:return [{'path':name,'category':'unreviewed_binary'}]
    for label,pattern in PATTERNS.items():
        matches=list(re.finditer(pattern,text))
        if any(hashlib.sha256(m[0].encode()).hexdigest() not in TEST_FIXTURES.get(name,set()) for m in matches):findings.append({'path':name,'category':label})
    return findings

def audit(root=ROOT,staged=False):
    root=Path(root)
    paths=[]
    for folder,dirs,files in os.walk(root):
        dirs[:]=[d for d in dirs if d not in SKIP and not d.endswith('.egg-info')]
        for f in files:paths.append(str((Path(folder)/f).relative_to(root)))
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(['git','init','-q',temp],check=True,capture_output=True)
        run=subprocess.run(['git','--git-dir',str(Path(temp)/'.git'),'--work-tree',str(root),'check-ignore','--no-index','--stdin','-z'],input='\0'.join(paths)+'\0',text=True,capture_output=True)
        if run.returncode not in (0,1):raise RuntimeError('Git ignore matcher failed')
        ignored=set(run.stdout.strip('\0').split('\0'));publish=[p for p in paths if p not in ignored]
    findings=[]
    if staged:
        # Read the actual index blobs, including force-added ignored paths.
        run=subprocess.run(['git','-C',str(root),'ls-files','--stage','-z'],check=True,capture_output=True)
        blobs={}
        for entry in run.stdout.decode().split('\0'):
            if not entry:continue
            metadata,name=entry.split('\t',1);mode,oid,stage=metadata.split()
            if stage!='0' or mode not in ('100644','100755'):
                findings.append({'path':name,'category':'nonregular_or_conflicted_index_entry'})
            blobs[name]=subprocess.run(['git','-C',str(root),'cat-file','blob',oid],check=True,capture_output=True).stdout
        publish=sorted(blobs)
        ignored_run=subprocess.run(['git','-C',str(root),'check-ignore','--no-index','--stdin','-z'],input='\0'.join(publish)+'\0',text=True,capture_output=True)
        if ignored_run.returncode not in (0,1):raise RuntimeError('Index ignore audit failed')
        for name in ignored_run.stdout.strip('\0').split('\0'):
            if name:findings.append({'path':name,'category':'ignored_path_in_index'})
    for name in publish:
        if not staged and (root/name).is_symlink():
            findings.append({'path':name,'category':'unreviewed_symlink'});continue
        try:content=blobs[name] if staged else (root/name).read_bytes()
        except OSError:
            findings.append({'path':name,'category':'unreadable_file'});continue
        findings.extend(scan_content(name,content))
    # Guard commonly forgotten artifacts independently of pattern scans.
    for name in publish:
        if name.endswith(('.sqlite3','.db','.csv','.jsonl','.log')) or name in ('config/trader.json','config/position-recovery.json','config/multistrategy.json','live-session.json') or name.split('/')[0] in ('runtime','reports','research','logs','.venv'):
            findings.append({'path':name,'category':'private_artifact_not_ignored'})
    return {'publishable_file_count':len(publish),'ignored_file_count':len(ignored- {''}),'scope':'index_blobs' if staged else 'working_tree','findings':findings,'passed':not findings,'note':'No secret values printed. Not a Git-history scan; review staged content before publishing.'}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--staged',action='store_true')
    result=audit(staged=parser.parse_args().staged)
    print(json.dumps(result,indent=2));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
