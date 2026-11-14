"""Independent synthetic regular-pair filtering; not trusted adapter/AI acceptance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import pytest
from independent_ticket_fixture import research_snapshot
from sports_app.auth import Principal
from sports_app.evidence import Claim, WEIGHTS
from sports_app.evidence_store import EvidenceRepository
from sports_app.manifest import POLICY_FILES
from sports_app.pair_policy import RegularPairPolicy, periods, category

AT=datetime(2026,11,14,4,tzinfo=timezone.utc)
ADMIN=Principal('independent-admin','admin','a')
REVIEWER=Principal('independent-reviewer','reviewer','r')


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs): raise AssertionError('External IO forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def batch():
    options=research_snapshot()['options'];events=[];markets={}
    for option in options:
        option.update(evidence_version='ev',market_snapshot_id=option['option_id'],
                      dimensions=[(f'{i:02d}','unknown') for i in range(1,24)],
                      reasons=['仅按市场基准，缺独立资料','合成风险说明'],claim_ids=['missing'])
        event=dict(identity=deepcopy(option['identity']),on_sale=True,conflict=False,source_id='synthetic',
                   as_of='2026-11-14T11:00:00+08:00',valid_until='2026-11-14T23:00:00+08:00',sales_close='2026-11-14T17:00:00+08:00')
        events.append(event)
        markets[option['option_id']]=dict(match_id=option['identity']['match_id'],market='HAD',line=None,
            version='mv',source_id='synthetic',on_sale=True,conflict=False,supports_pair=True,
            as_of=event['as_of'],valid_until=event['valid_until'],sales_close=event['sales_close'],
            outcomes={'H':'4','D':'4','A':'4'})
    manifest=dict(runtime_provider='codex',policy_revision='synthetic-policy',analysis_revision='synthetic-analysis',
                  schema_hash='schema',code_hash='code',rule_hashes={p:'synthetic-hash' for p in POLICY_FILES})
    return dict(manifest=manifest,evidence_score_version='evidence-policy-1.0.0',evidence_version='ev',market_version='mv',
                schedule_complete=True,candidate_universe_complete=True,adapter_id='synthetic',conflict=False,
                sport='football',business_day='2026-11-14',as_of='2026-11-14T11:00:00+08:00',valid_until='2026-11-14T23:00:00+08:00',
                events=events,markets=markets,options={o['option_id']:o for o in options},
                candidates={'pair':dict(option_ids=['o1','o2'],period='early_middle',equal_stake='1',total_investment='1')})


@pytest.fixture
def setup(tmp_path):
    from sports_app.storage import SQLiteStore
    root=Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root)
    repo=EvidenceRepository(SQLiteStore(tmp_path/'data',app_root=root),clock=lambda:AT)
    return repo,RegularPairPolicy(repo,approved_manifest=batch()['manifest'])


def test_legal_conditional_retains_gaps(setup):
    _,policy=setup;value=batch();policy.freeze_batch(ADMIN,'a','b',value)
    result=policy.build('b','pair')
    assert result['recommendation_status']=='conditional_low_confidence'
    assert all(not a['eligible'] and a['quality_gaps'] for a in result['assessment_summary'].values())
    assert result['model_probability'] is None and result['EV'] is None


@pytest.mark.parametrize('mutation',['identity','sale','source','cutoff','kickoff','price','market_missing',
                                     'market_source','market_version','evidence_version','manifest','schedule','universe'])
def test_hard_input_rejection_not_conditionally_bypassed(setup,mutation):
    _,policy=setup;value=batch()
    if mutation=='identity':value['events'][0]['identity']['home_id']='forged'
    elif mutation=='sale':value['events'][0]['on_sale']=False
    elif mutation=='source':value['events'][0]['source_id']=''
    elif mutation=='cutoff':value['events'][0]['sales_close']=AT.isoformat()
    elif mutation=='kickoff':
        for obj in (value['events'][0]['identity'],value['options']['o1']['identity']):obj['kickoff']=AT.isoformat()
    elif mutation=='price':value['markets']['o1']['outcomes']['H']='9'
    elif mutation=='market_missing':del value['markets']['o1']['outcomes']['A']
    elif mutation=='market_source':value['markets']['o1']['source_id']=''
    elif mutation=='market_version':value['markets']['o1']['version']='changed'
    elif mutation=='evidence_version':value['options']['o1']['evidence_version']='changed'
    elif mutation=='manifest':value['manifest']['code_hash']='changed'
    elif mutation=='schedule':value['schedule_complete']=False
    else:value['candidate_universe_complete']=False
    with pytest.raises(ValueError):
        policy.freeze_batch(ADMIN,'a','b',value)
        policy.build('b','pair')


@pytest.mark.parametrize('kickoffs,expected',[
    (['2026-11-14T12:00:00+08:00','2026-11-14T19:30:00+08:00'],['early_middle']*2),
    (['2026-11-14T19:30:01+08:00','2026-11-14T22:59:59+08:00'],['middle']*2),
    (['2026-11-14T23:00:00+08:00','2026-11-15T09:00:00+08:00'],['late']*2),
    (['2026-11-14T18:00:00+08:00','2026-11-14T20:00:00+08:00'],['early_middle']*2)])
def test_football_endpoints_and_full_schedule_merge(kickoffs,expected):
    value=batch()
    for event,kickoff in zip(value['events'],kickoffs):event['identity']['kickoff']=kickoff
    assert list(periods(value)[1].values())==expected


@pytest.mark.parametrize('kickoff',['2026-11-14T11:59:59+08:00','2026-11-15T09:00:01+08:00'])
def test_football_window_escape(kickoff):
    value=batch();value['events'][0]['identity']['kickoff']=kickoff
    with pytest.raises(ValueError):periods(value)


def test_basketball_next_day_early_hours_remain_late_business_segment():
    value=batch();value['sport']='basketball'
    for event,kickoff in zip(value['events'],['2026-11-14T23:00:00+08:00','2026-11-15T01:00:00+08:00']):
        event['identity'].update(sport='basketball',kickoff=kickoff)
    assert list(periods(value)[1].values())==['late','late']


@pytest.mark.parametrize('options',[
    [dict(market='HAD',selection='H'),dict(market='HHAD',selection='D',line='-1')],
    [dict(market='HAD',selection='D'),dict(market='HHAD',selection='D',line='0')],
    [dict(market='TTG',selection='2'),dict(market='TTG',selection='4')]])
def test_bad_dd_or_nonadjacent_ttg_rejected(options):
    with pytest.raises(ValueError):category(options)


@pytest.mark.parametrize('line',['-1','1'])
def test_dd_composition_control(line):
    assert category([dict(market='HAD',selection='D'),dict(market='HHAD',selection='D',line=line)])=='DD'


def test_known_omitted_conflict_cannot_be_market_only_condition(setup):
    repo,policy=setup;value=batch()
    claim=Claim('conflict','o1',('04',),'https://independent.example.org/conflict','L1',AT-timedelta(hours=2),
                AT-timedelta(hours=1),'group','synthetic conflicting quote',True,False)
    repo.ingest(ADMIN,'a',claim)
    repo.verify(REVIEWER,'r',receipt_id='receipt',claim_id='conflict',quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='conflict')
    policy.freeze_batch(ADMIN,'a','b',value)
    with pytest.raises(ValueError):policy.build('b','pair')


def test_qualified_positive_control(setup):
    repo,policy=setup;value=batch()
    for option in value['options'].values():
        option['dimensions']=[(f'{i:02d}','verified') for i in range(1,24)];option['claim_ids']=[]
        for i in (1,2):
            ident=option['option_id']+str(i)
            claim=Claim(ident,option['option_id'],tuple(WEIGHTS),f'https://source{i}.example.org/report','L1',
                        AT-timedelta(hours=2),AT-timedelta(hours=1),f'group{i}','synthetic quote',True,False)
            repo.ingest(ADMIN,'a',claim)
            repo.verify(REVIEWER,'r',receipt_id=ident+'r',claim_id=ident,quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
            option['claim_ids'].append(ident)
    policy.freeze_batch(ADMIN,'a','b',value)
    result=policy.build('b','pair')
    assert result['recommendation_status']=='evidence_qualified'
    assert all(a['eligible'] and a['score']==100 for a in result['assessment_summary'].values())
