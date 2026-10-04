"""Opt-in coordinator using the existing reconciliation/reservation/dispatch path.

No researched strategy is enabled by installation. Baseline runtime is unchanged
when config/multistrategy.json is absent. Configuration requires controlled restart.
"""
from dataclasses import replace
from pathlib import Path
from ..models import D,Unsafe,dt
from ..persistent.engine import Coordinator
from .registry import Registry
from .features import FeatureEngine
from .regimes import classify,eligible
from .arbitration import resolve


class MultiCoordinator(Coordinator):
    def __init__(self,*args,registry,history=(),registry_path=None,**kwargs):
        super().__init__(*args,**kwargs)
        if registry.document['mode']!='MULTI_LIVE':raise Unsafe('MultiCoordinator requires explicit MULTI_LIVE mode')
        self.registry=registry;self.feature_engine=FeatureEngine(history);self.registry_path=registry_path

    def health_check(self,now,entry=True):
        super().health_check(now,entry)
        if entry and self.registry_path and Registry.load(self.registry_path).fingerprint!=self.registry.fingerprint:raise Unsafe('Strategy configuration changed; controlled restart required')

    def candidate_config(self,candidate):
        signal=candidate.get('signal')
        if not signal:raise Unsafe('Versioned selected strategy signal required')
        strategy=self.registry.require_live(signal['strategy_id'])
        if signal['fingerprint']!=strategy.fingerprint or signal['direction']!='LONG':raise Unsafe('Strategy fingerprint/direction mismatch')
        if dt(signal['signal_at'])>self.now() or self.now()>=dt(signal['valid_until']):raise Unsafe('Expired/future strategy signal')
        # Every protective parameter is derived from the fingerprinted object.
        p=strategy.parameters
        if signal['target_r']!=p.target_r or signal['max_hold_minutes']!=p.hold_minutes or signal['vwap_exit']!=(strategy.id not in ('vwap_reversion','gap_fade')):raise Unsafe('Strategy protection parameters changed')
        return replace(self.cfg,target_r=D(str(p.target_r)),max_hold_minutes=p.hold_minutes,vwap_exit=signal['vwap_exit'])

    async def observe(self,broker,bars=None,tradability=None,full=False):
        obs=await super().observe(broker,bars,tradability,full)
        now=self.now();session=self.session();signals=[];candidates={};audit=[]
        if session and session.entry_window(now) and obs['data_healthy']:
            for sym in self.cfg.universe:
                try:
                    frame=self.feature_engine.snapshot(sym,(bars or {}).get(sym,[]),session,now,bars)
                    regime=classify(frame)
                    for sid,strategy in self.registry.objects.items():
                        evaluation=strategy.evaluate(frame,session)
                        reason=evaluation.reason
                        if not self.registry.permitted(sid,'LIVE'):reason='NOT_LIVE_ENABLED_WITH_VALIDATION'
                        elif not eligible(sid,regime):reason='REGIME_INELIGIBLE'
                        elif evaluation.signal:
                            signals.append(replace(evaluation.signal,regime=regime.labels));continue
                        audit.append(dict(strategy=sid,symbol=sym,at=frame.at,reason=reason,features=frame.audit(),regime=regime.labels,regime_reasons=regime.reasons))
                except (Unsafe,KeyError,ValueError,TypeError) as exc:audit.append(dict(symbol=sym,reason='FEATURES_UNAVAILABLE:'+str(exc)))
        # Include current ownership here. During preflight allow only the exact
        # setup already reserved; the ledger still enforces symbol ownership.
        owned={t['symbol']:t for t in self.ledger.trades()}
        signals=[s for s in signals if s.symbol not in owned or s.setup_id==owned[s.symbol]['setup_id']]
        resolution=resolve(signals,now,priorities=self.registry.priorities)
        for signal in resolution.selected:candidates[signal.symbol]=signal.candidate()
        for sym in self.cfg.universe:
            if sym not in candidates:
                original=obs['candidates'].get(sym,{})
                # Preserve derived VWAP/bar state for continuous position exits.
                candidates[sym]=dict(original,state='NO_ELIGIBLE_STRATEGY',reason='No uniquely eligible current candidate')
        obs['candidates']=candidates
        with self.ledger.tx():
            self.ledger.put('observation',obs)
            self.ledger.event(now,'STRATEGY_ARBITRATION',dict(registry_fingerprint=self.registry.fingerprint,
                competing=[s.audit() for s in signals],selected=[s.audit() for s in resolution.selected],
                rejected=list(resolution.rejected)+audit))
        return obs


def make_coordinator(root,ledger,**kwargs):
    path=Path(root)/'config/multistrategy.json'
    if not path.exists():return Coordinator(root,ledger,**kwargs)
    registry=Registry.load(path)
    if registry.document['mode']=='SHADOW':raise Unsafe('SHADOW configuration cannot start the LIVE worker; use the shadow observer')
    if registry.document['mode']=='MULTI_LIVE':return MultiCoordinator(root,ledger,registry=registry,registry_path=path,**kwargs)
    if registry.document.get('shadow_enabled'):
        from .shadow import ShadowObservedCoordinator
        return ShadowObservedCoordinator(root,ledger,registry=registry,**kwargs)
    return Coordinator(root,ledger,**kwargs)
