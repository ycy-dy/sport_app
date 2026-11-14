"""Typed approved public snapshot projections; no internal text auto-disclosure."""
from math import isfinite
import re


def viewer_manifest(value):
    """Shape check only: never authentication, reverse lookup or rehashing."""
    valid=isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None
    return {'manifest_hash':value if valid else None,
            'field_coverage':{'manifest_hash':'available' if valid else 'unknown'}}

# Public research phases, NOT ManualRuntime lifecycle states.
TASK_STAGES=frozenset({'collecting','researching','drafting','evidence_review'})
EVIDENCE_STATES=frozenset({'unknown','partial','qualified','conflict'})


def string(value):
    if value is not None and not isinstance(value,str):raise ValueError('invalid public string')
    return value


def strings(value):
    if value is None:return []
    if not isinstance(value,list) or any(not isinstance(item,str) for item in value):
        raise ValueError('invalid public string array')
    return list(value)


def fields(row,texts=(),arrays=()):
    answer={key:string(row.get(key)) for key in texts}
    answer.update({key:strings(row.get(key)) for key in arrays})
    answer['field_coverage']={key:'available' if key in row and row[key] is not None else 'unknown'
                              for key in (*texts,*arrays)}
    return answer


def enrich_feed(feed,row,result):
    if feed=='schedules':
        extra=fields(row,('match_id','timezone','home_team','away_team','period','sale_status',
                         'sales_deadline','source_snapshot_at'),('exclusion_reasons',))
    elif feed=='analyses':
        extra=fields(row,('publication_id','match_id','task_stage','evidence_status','evidence_as_of','strongest_counterevidence',
                         'source_published_at','source_effective_at'),
                     ('quality_gaps','covered_paths','excluded_paths','failure_paths','withdraw_if','reasons'))
        for key in ('risk','no_pair_reason','withdrawal_reason'):
            extra[key]=string(row.get(key))
            extra['field_coverage'][key]='available' if row.get(key) is not None else 'unknown'
        score=row.get('evidence_score')
        if score is not None and (type(score) not in (int,float) or not isfinite(score) or not 0<=score<=100):
            raise ValueError('invalid public score')
        extra['evidence_score']=score
        extra['field_coverage']['evidence_score']='available' if score is not None else 'unknown'
        if extra['task_stage'] is not None and extra['task_stage'] not in TASK_STAGES:
            raise ValueError('invalid public research phase')
        if extra['evidence_status'] is None:extra['evidence_status']='unknown'
        if extra['evidence_status'] not in EVIDENCE_STATES:
            raise ValueError('invalid public evidence state')
        # Approval is permission to disclose, never proof of evidence quality.
        if extra['evidence_status']=='qualified' and (score is None or score<70 or extra['quality_gaps']):
            raise ValueError('qualified evidence requires score >=70 and no declared gaps')
    else:
        texts=['policy_revision','analysis_revision','coverage_status','validation_level']
        arrays=['notices']
        if feed=='analysis-system':texts+=['runtime_provider'];arrays+=['dimensions','event_coverage']
        else:texts+=['conditional_boundary'];arrays+=['recommendation_kinds','quality_gates','publication_gates','prohibitions']
        extra=fields(row,texts,arrays)
        if extra.get('runtime_provider') not in (None,'codex'):raise ValueError('invalid public provider')
    result.update(extra)
    if 'identity' in result:
        coverage=result['field_coverage']
        for key in ('sport','league','business_day','match_id','kickoff','home_id','away_id'):
            coverage['identity.'+key]='available' if result['identity'].get(key) is not None else 'unknown'
    return result
