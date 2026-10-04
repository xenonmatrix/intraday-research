"""Reproducible chronological replication. Never grants live permission."""
import argparse
from collections import Counter
from dataclasses import asdict,replace
from datetime import datetime
import hashlib,json
from pathlib import Path
import subprocess
import numpy as np

from .data import load_vendor
from .backtest import Costs,build_tape,run,metrics
from .strategies import STRATEGIES,Parameters,fingerprint,FilteredORB
from .regimes import classify,VERSION as REGIME_VERSION


def interval(trades,daily,gates):
    counts=Counter(t['day'] for t in trades)
    active=sum(bool(counts[d['day']]) for d in daily)
    if active<gates['minimum_trade_days_for_interval']:
        return dict(lower=None,upper=None,active_days=active,reason='Too few active day clusters for declared interval reporting floor')
    rng=np.random.default_rng(gates['bootstrap_seed'])
    pnl=np.array([d['pnl'] for d in daily]);n=np.array([counts[d['day']] for d in daily])
    samples=rng.integers(len(daily),size=(gates['bootstrap_replicates'],len(daily)))
    totals=n[samples].sum(axis=1);valid=totals>0
    expectancy=pnl[samples].sum(axis=1)[valid]/totals[valid]
    alpha=gates['family_alpha']/gates['hypotheses']
    lo,hi=np.quantile(expectancy,[alpha/2,1-alpha/2])
    return dict(lower=float(lo),upper=float(hi),active_days=active,level=1-alpha,
                method='IID day-cluster bootstrap; preserves within-day symbol/trade dependence, not serial day dependence',
                limitations='Indicative uncertainty on reused data; family correction covers seven primary hypotheses, not every diagnostic')


def subset(result,start,end):
    trades=[t for t in result['trades'] if start<=t['day']<end]
    daily=[d for d in result['daily'] if start<=d['day']<end]
    return dict(metrics=metrics(trades,daily),trades=trades,daily=daily)


def breakdown(result,key):
    out={}
    for value in sorted({key(t) for t in result['trades']}):
        ts=[t for t in result['trades'] if key(t)==value]
        ds=[dict(day=d['day'],pnl=sum(t['pnl'] for t in ts if t['day']==d['day'])) for d in result['daily']]
        out[value]=metrics(ts,ds)
    return out


def variants(strategy_id):
    p=Parameters()
    for field,values in [('target_r',(1.5,2.5)),('hold_minutes',(20,40))]:
        for value in values:yield field+'='+str(value),replace(p,**{field:value})
    field={'orb':'offset','filtered_orb':'rvol','orb_retest':'retest_width','vwap_trend':'rvol','vwap_reversion':'reversion_z','gap_continuation':'gap_atr','gap_fade':'gap_atr'}[strategy_id]
    for multiplier in (.8,1.2):yield field+'*'+str(multiplier),replace(p,**{field:getattr(p,field)*multiplier})


def decision(parts,stress,walk,sensitivity,ci,protocol):
    g=protocol['gates'];v=parts['validation']['expectancy'];t=parts['test']['expectancy']
    positive=lambda x:x is not None and x>0
    checks=dict(positive_validation=positive(v),positive_test=positive(t),positive_stress=positive(stress['expectancy']),
        uncertainty_lower_positive=positive(ci['lower']),outlier_independence=parts['test']['without_best_day']>0,
        drawdown=parts['test']['max_drawdown']<=g['max_drawdown_dollars'],
        period_stability=bool(walk) and sum(x['pnl']>0 for x in walk.values())/len(walk)>=g['minimum_positive_walk_forward_fraction'],
        parameter_stability=bool(sensitivity) and sum(x['validation']['pnl']>=0 and x['validation']['trades']>0 for x in sensitivity.values())/len(sensitivity)>=g['minimum_nonnegative_sensitivity_fraction'],
        degradation=positive(v) and t is not None and t>=g['test_expectancy_at_least_fraction_of_validation']*v,
        untouched_test=False,live_feature_parity=False,shadow_execution_evidence=False)
    quantitative=all(v for k,v in checks.items() if k not in ('untouched_test','live_feature_parity','shadow_execution_evidence'))
    status='SHADOW_ELIGIBLE' if quantitative else 'RESEARCH_ONLY'
    if parts['test']['trades'] and (not checks['positive_test'] or not checks['positive_stress']):status='REJECTED'
    return dict(status=status,implemented=True,live_eligible=False,live_enabled=False,checks=checks,
                failures=[k for k,v in checks.items() if not v],
                uncertainty='Historical replication only; no claim of untouched, shadow or live edge')


def study(raw,output,protocol_path):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    protocol_bytes=Path(protocol_path).read_bytes();protocol=json.loads(protocol_bytes)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    if subprocess.check_output(['git','status','--porcelain'],text=True).strip():commit+='\u002bworking-tree'
    # Evidence of ordering: write exact preregistration before loading or computing returns.
    protocol_hash=hashlib.sha256(protocol_bytes).hexdigest()
    (output/'frozen-protocol.json').write_bytes(protocol_bytes)
    source_hash=fingerprint({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path('trader/multistrategy').glob('*.py'))})
    run_id='replication-'+protocol_hash[:12]+'-'+source_hash[:12]
    print('Loading original data',flush=True)
    days,manifest=load_vendor(raw)
    (output/'data-audit.json').write_text(json.dumps(manifest,indent=2))
    print(f'Building point-in-time tape: {len(days)} complete paired sessions',flush=True)
    tape=build_tape(days)
    regimes=Counter(label for records in tape.values() for fs in records.values() for f in fs.values() for label in classify(f).labels)
    results={}
    for strategy_id in protocol['strategies']:
        print('Evaluating '+strategy_id,flush=True)
        strategy=STRATEGIES[strategy_id]();scenarios={}
        for name,cost in protocol['cost_scenarios'].items():
            scenarios[name]=run(days,tape,strategy,Costs(**cost),run_id=run_id,code_commit=commit,regime_classifier=classify)
            (output/(strategy_id+'-'+name+'-trades.json')).write_text(json.dumps(scenarios[name],indent=2))
        base=scenarios['base'];parts={name:subset(base,*protocol[name]) for name in ('train','validation','test')}
        walks={month:subset(base,month+'-01',str(np.datetime64(month)+np.timedelta64(1,'M'))+'-01')['metrics']
               for month in sorted({d['day'][:7] for d in base['daily'] if protocol['validation'][0]<=d['day']<protocol['test'][1]})}
        sensitivity={};dev=[d for d in days if d.session.day<protocol['test'][0]]
        for label,params in variants(strategy_id):
            altered=run(dev,tape,STRATEGIES[strategy_id](params),run_id=run_id,code_commit=commit)
            sensitivity[label]={name:subset(altered,*protocol[name])['metrics'] for name in ('train','validation')}
        exploratory={}
        if strategy_id=='filtered_orb':
            for omit in ('vwap','slope','volume','momentum','gap','volatility','cross_asset'):
                ablation=run(dev,tape,FilteredORB(omit_filter=omit),run_id=run_id,code_commit=commit)
                exploratory['omit_'+omit]={name:subset(ablation,*protocol[name])['metrics'] for name in ('train','validation')}
        if strategy_id=='orb_retest':
            for timeout in (4,8):
                ablation=run(dev,tape,STRATEGIES[strategy_id](Parameters(timeout_bars=timeout)),run_id=run_id,code_commit=commit)
                exploratory['timeout_bars='+str(timeout)]={name:subset(ablation,*protocol[name])['metrics'] for name in ('train','validation')}
        ci=interval(parts['test']['trades'],parts['test']['daily'],protocol['gates'])
        partmetrics={k:v['metrics'] for k,v in parts.items()}
        status=decision(partmetrics,subset(scenarios['stress'],*protocol['test'])['metrics'],walks,sensitivity,ci,protocol)
        results[strategy_id]=dict(parameters=asdict(strategy.parameters),fingerprint=strategy.fingerprint,
            version=strategy.version,metrics=base['metrics'],periods=partmetrics,walk_forward=walks,
            scenarios={k:dict(all=v['metrics'],test=subset(v,*protocol['test'])['metrics']) for k,v in scenarios.items()},
            diagnostics={k:v['diagnostics'] for k,v in scenarios.items()},
            sensitivity=sensitivity,confidence_interval=ci,decision=status,
            exploratory_development_only=exploratory,exploratory_note='Additional diagnostics after initial replication, not predeclared selection or live-gate evidence; final-test period excluded',
            by_symbol=breakdown(base,lambda t:t['symbol']),by_regime=breakdown(base,lambda t:'+'.join(t['regime'])),
            by_time=breakdown(base,lambda t:datetime.fromisoformat(t['entry_at']).astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%H:00')),
            degradation_ratio=partmetrics['test']['expectancy']/partmetrics['validation']['expectancy'] if partmetrics['validation']['expectancy'] else None)
        (output/'results.json').write_text(json.dumps(dict(run_id=run_id,code_commit=commit,source_digest=source_hash,protocol_hash=protocol_hash,data=manifest,
            regime_version=REGIME_VERSION,regime_counts=dict(regimes),strategies=results),indent=2,allow_nan=False))
        print(strategy_id+': '+status['status']+', test trades='+str(partmetrics['test']['trades']),flush=True)
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',default='research/extended/raw');parser.add_argument('--output',default='reports/multistrategy-study')
    parser.add_argument('--protocol',default='docs/RESEARCH_PROTOCOL.json');args=parser.parse_args()
    study(args.raw,args.output,args.protocol)


if __name__=='__main__':main()
