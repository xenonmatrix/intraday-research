"""Separate implementation, evidence, eligibility and operator enablement.

Evidence is an operator-reviewed artifact, never inferred from a config label.
Runtime config is immutable for the lifetime of a coordinator.
"""
from pathlib import Path
import json,re
from ..models import Unsafe
from .strategies import STRATEGIES,Parameters,fingerprint

STATUSES={'REJECTED','RESEARCH_ONLY','SHADOW_ELIGIBLE','LIVE_CAPABLE_PENDING_MORE_EVIDENCE','LIVE_ELIGIBLE'}
REQUIRED=('positive_validation','positive_test','positive_stress','uncertainty_lower_positive','outlier_independence',
          'drawdown','period_stability','parameter_stability','degradation','untouched_test','live_feature_parity','shadow_execution_evidence')


class Registry:
    def __init__(self,document):
        self.document=json.loads(json.dumps(document));self.fingerprint=fingerprint(self.document)
        if set(document)-{'version','mode','shadow_enabled','strategies'}:raise Unsafe('Unknown multi-strategy configuration field')
        if document.get('version')!=1 or document.get('mode') not in ('BASELINE','MULTI_LIVE','SHADOW'):raise Unsafe('Explicit valid strategy mode required')
        if type(document.get('shadow_enabled',False)) is not bool:raise Unsafe('Boolean shadow enablement required')
        self.entries={};self.objects={}
        for row in self.document.get('strategies',[]):
            if set(row)-{'id','status','live_enabled','shadow_enabled','priority','parameters','evidence'}:raise Unsafe('Unknown strategy configuration field')
            sid=row['id']
            if sid not in STRATEGIES or sid in self.entries:raise Unsafe('Unknown or duplicate strategy')
            if row.get('status') not in STATUSES or type(row.get('live_enabled')) is not bool or type(row.get('shadow_enabled',False)) is not bool:raise Unsafe('Explicit status and boolean enablement required')
            if type(row.get('priority',100)) is not int:raise Unsafe('Integer arbitration priority required')
            self.objects[sid]=STRATEGIES[sid](Parameters(**row.get('parameters',{})));self.entries[sid]=row
            if row['live_enabled']:
                if document['mode']!='MULTI_LIVE':raise Unsafe('No automatic SHADOW/BASELINE to LIVE fallback')
                self.require_live(sid)

    def require_live(self,sid):
        row=self.entries.get(sid,{})
        if self.document['mode']!='MULTI_LIVE' or row.get('status')!='LIVE_ELIGIBLE' or row.get('live_enabled') is not True:raise Unsafe('Strategy is not explicitly live eligible and enabled')
        evidence=row.get('evidence',{})
        if evidence.get('strategy_fingerprint')!=self.objects[sid].fingerprint or any(not isinstance(evidence.get(k),str) or not re.fullmatch('[0-9a-f]{64}',evidence[k]) for k in ('protocol_hash','data_digest','report_digest')):
            raise Unsafe('Versioned validation provenance required')
        if any(evidence.get('checks',{}).get(k) is not True for k in REQUIRED):raise Unsafe('Required validation evidence missing or failed')
        return self.objects[sid]

    def permitted(self,sid,mode):
        if mode=='SHADOW':return self.entries.get(sid,{}).get('shadow_enabled') is True
        if mode!='LIVE':raise Unsafe('Unknown dispatch mode')
        try:self.require_live(sid);return True
        except Unsafe:return False

    @property
    def priorities(self):return {sid:r.get('priority',100) for sid,r in self.entries.items()}

    @classmethod
    def load(cls,path):return cls(json.loads(Path(path).read_text()))
