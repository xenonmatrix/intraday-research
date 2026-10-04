"""Read-only database exports for analysis; originals remain the audit source."""
import csv,io,json,sqlite3
from pathlib import Path
from ..store import atomic_text

def export(root):
    root=Path(root);db=sqlite3.connect(f'file:{root}/runtime/persistent.sqlite3?mode=ro',uri=True)
    from ..configuration import load
    cfg=load(root,required=False)
    out=root/cfg['analysis']['export_directory'];out.mkdir(parents=True,exist_ok=True)
    try:
        db.execute('BEGIN')
        events=[dict(id=r[0],timestamp=r[1],kind=r[2],data=json.loads(r[3])) for r in db.execute('SELECT * FROM events ORDER BY id')]
        fills=[json.loads(r[0]) for r in db.execute('SELECT data FROM fills ORDER BY rowid')]
        trades=[json.loads(r[0]) for r in db.execute('SELECT data FROM trades ORDER BY rowid')]
        candidates=[x for x in events if x['kind']=='CANDIDATE_ANALYSIS']
        for name,rows in [('events',events),('candidates',candidates),('fills',fills),('positions',trades)]:
            atomic_text(out/(name+'.jsonl'),''.join(json.dumps(r)+'\n' for r in rows))
        buf=io.StringIO();fields=['timestamp','ticker','side','quantity','entry_price','exit_price','position_value','setup','stop_condition','target_condition','realized_pnl','account_value','trade_id','order_id','broker_id','execution_id','config_fingerprint','strategy_fingerprint']
        writer=csv.DictWriter(buf,fields);writer.writeheader();byid={t['id']:t for t in trades}
        for fill in fills:
            e=fill['execution'];t=byid[fill['trade_id']]
            from ..models import D
            writer.writerow(dict(timestamp=e['timestamp'],ticker=fill['symbol'],side=fill['side'],quantity=e['quantity'],
                entry_price=e['price'] if fill['side']=='buy' else t['entry'],exit_price=e['price'] if fill['side']=='sell' else '',
                position_value=str(D(e['price'])*D(e['quantity'])),setup=t['setup_id'],stop_condition=fill.get('exit_reason') or t['stop'],
                target_condition=t['target'],realized_pnl=fill['realized_pnl'] if fill['side']=='sell' else '',account_value=fill.get('account_value',''),
                trade_id=fill['trade_id'],order_id=fill['order_id'],broker_id=fill['broker_id'],execution_id=e['id'],config_fingerprint=t.get('config_fingerprint','legacy-unversioned'),strategy_fingerprint=t.get('strategy_fingerprint','legacy-unversioned')))
        atomic_text(out/'trades.csv',buf.getvalue())
    finally:db.close()
    return str(out)
