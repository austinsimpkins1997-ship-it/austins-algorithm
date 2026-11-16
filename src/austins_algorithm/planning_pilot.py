"""Caller-recorded operator observations, never synthetic human outcomes."""
from datetime import datetime, timezone
import math

STAGES=('setup','input_checks','evidence_refresh','solve','verification','retries','recovery')


def observation(*,operator,pair_id,arm,project_sha256,seconds,required_checks,completed_checks,output_verified):
    if not all(isinstance(v,str) and 0<len(v)<=120 for v in (operator,pair_id)):
        raise ValueError('A caller-supplied operator label and matched pair ID are required')
    if arm not in ('baseline','planning') or not isinstance(project_sha256,str) or len(project_sha256)!=64 or any(c not in '0123456789abcdef' for c in project_sha256):
        raise ValueError('Invalid arm or project fingerprint')
    if not isinstance(seconds,dict) or set(seconds)!=set(STAGES) or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in seconds.values()):
        raise ValueError('Record finite nonnegative active seconds for every named stage')
    for checks in (required_checks,completed_checks):
        if not isinstance(checks,list) or not checks or any(not isinstance(c,str) or not c.strip() for c in checks) or len(set(checks))!=len(checks):
            raise ValueError('Check names must be explicit, unique and nonempty')
    if type(output_verified) is not bool:raise ValueError('Output verification must be explicit')
    return {'evidence_kind':'CALLER_REPORTED_OPERATOR_OBSERVATION','recorded_utc':datetime.now(timezone.utc).isoformat(),
            'operator':operator,'pair_id':pair_id,'arm':arm,'project_sha256':project_sha256,'seconds':dict(seconds),
            'required_checks':list(required_checks),'completed_checks':list(completed_checks),'output_verified':output_verified,
            'accepted':output_verified and set(required_checks)==set(completed_checks)}


def summarize(records):
    pairs={}
    for row in records:
        validated=observation(**{key:row[key] for key in ('operator','pair_id','arm','project_sha256','seconds','required_checks','completed_checks','output_verified')})
        key=(row['operator'],row['pair_id'])
        pair=pairs.setdefault(key,{})
        if row['arm'] in pair:raise ValueError('Duplicate matched-pair arm')
        pair[row['arm']]=validated
    accepted=[];held=[]
    for key,pair in pairs.items():
        if set(pair)!= {'baseline','planning'}:
            held.append({'pair':key,'reason':'Missing comparison arm'});continue
        a,b=pair['baseline'],pair['planning']
        if not a['accepted'] or not b['accepted'] or a['project_sha256']!=b['project_sha256'] or set(a['required_checks'])!=set(b['required_checks']):
            held.append({'pair':key,'reason':'Inputs, mandatory checks or output verification differ'});continue
        baseline,total=sum(a['seconds'].values()),sum(b['seconds'].values())
        accepted.append({'pair':key,'baseline_active_seconds':baseline,'planning_active_seconds':total,
                         'saved_seconds':baseline-total,'saved_fraction':None if baseline==0 else (baseline-total)/baseline})
    return {'evidence_kind':'CALLER_REPORTED_OPERATOR_COMPARISON','accepted_pairs':accepted,'held_pairs':held,
            'conclusion':'NO_MATCHED_OPERATOR_EVIDENCE' if not accepted else 'DESCRIPTIVE_PAIRED_OBSERVATIONS_ONLY',
            'causal_benefit_established':False}
