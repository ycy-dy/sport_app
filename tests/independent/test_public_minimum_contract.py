"""Independent section-9 minimum projection, synthetic explicit approvals only."""
from contextlib import closing
import json
import pytest
from test_read_api_contract import environment, feed, identity, legacy
from sports_app.publication import canonical


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


CASES=[('schedules','/api/schedules',dict(event_id='e',status='scheduled',identity=identity(),match_id='m',
    timezone='Asia/Shanghai',home_team='Synthetic Home',away_team='Synthetic Away',period='middle',sale_status='on_sale',
    sales_deadline='2026-11-14T17:00:00+08:00',source_snapshot_at='2026-11-14T12:00:00+08:00',exclusion_reasons=[])),
 ('analyses','/api/analyses',dict(analysis_id='a',status='published',title='Synthetic analysis',identity=identity(),
    publication_id='publication',match_id='m',task_stage='evidence_review',evidence_status='qualified',evidence_score=70,
    evidence_as_of='2026-11-14T12:00:00+08:00',strongest_counterevidence='synthetic counterevidence',
    source_published_at='2026-11-14T11:00:00+08:00',source_effective_at='2026-11-14T11:30:00+08:00',
    quality_gaps=[],covered_paths=['path'],excluded_paths=['excluded'],failure_paths=['failure'],withdraw_if=['stop'],
    reasons=['approved reason'],risk='approved risk',no_pair_reason=None,withdrawal_reason=None)),
 ('analysis-system','/api/systems/analysis',dict(version='v',status='ready',title='Public analysis summary',
    policy_revision='p',analysis_revision='a',coverage_status='not_integrated',validation_level='not_validated',
    runtime_provider='codex',dimensions=['01'],event_coverage=['synthetic scope'],notices=['not real AI'])),
 ('recommendation-system','/api/systems/recommendation',dict(version='v',status='ready',title='Public recommendation summary',
    policy_revision='p',analysis_revision='a',coverage_status='not_integrated',validation_level='not_validated',
    conditional_boundary='regular_pair only',recommendation_kinds=['regular_pair'],quality_gates=['synthetic gate'],
    publication_gates=['manual admin'],prohibitions=['no payment'],notices=['not real AI']))]


@pytest.mark.parametrize('name,path,row',CASES)
def test_four_explicit_public_snapshot_full_controls(environment,name,path,row):
    _,views,client=environment;feed(views,feed_name=name,rows=[row])
    response=client.get(path);assert response.status_code==200
    actual=response.json['data'][0]
    for key,value in row.items():assert actual[key]==value
    for key,coverage in actual['field_coverage'].items():
        if not key.startswith('identity.'):
            assert coverage==('unknown' if row.get(key) is None else 'available')
    assert actual['model_probability'] is None and actual['EV'] is None


@pytest.mark.parametrize('name,path,row',CASES)
def test_missing_public_fields_keep_explicit_unknown_coverage(environment,name,path,row):
    _,views,client=environment
    minimal={'status':row['status']}
    feed(views,feed_name=name,rows=[minimal])
    actual=client.get(path).json['data'][0]
    for key,coverage in actual['field_coverage'].items():
        assert coverage=='unknown'
        assert actual[key] is None or actual[key]==[] or (key=='evidence_status' and actual[key]=='unknown')


@pytest.mark.parametrize('field,value',[('reasons',{'private':'MARKER'}),('risk',['bad']),('evidence_score',True),
    ('evidence_score',101),('task_stage','invented-stage'),('evidence_status','invented-evidence')])
def test_bad_analysis_type_or_enum_rejected(environment,field,value):
    _,views,_=environment;row=dict(CASES[1][2]);row[field]=value
    with pytest.raises(ValueError):feed(views,feed_name='analyses',rows=[row])


def test_internal_prose_never_becomes_public_without_approved_link(environment):
    store,views,client=environment
    original=dict(recommendation_type='regular_pair',recommendation_status='conditional_low_confidence',
                  reasons=['PRIVATE_INTERNAL_REASON'],risk='PRIVATE_INTERNAL_RISK')
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',('p','slot','s','m','published',canonical(original),'created'))
    row=client.get('/api/recommendations').json['data'][0]
    assert row['reasons']==[] and row['risk'] is None
    assert row['field_coverage']['reasons']=='unknown'
    approved=dict(CASES[1][2],publication_id='different-publication')
    feed(views,feed_name='analyses',rows=[approved],ident='unrelated')
    assert client.get('/api/recommendations').json['data'][0]['reasons']==[]
    approved['publication_id']='p';feed(views,feed_name='analyses',rows=[approved],ident='linked')
    row=client.get('/api/recommendations').json['data'][0]
    assert row['reasons']==['approved reason'] and row['risk']=='approved risk'
    assert row['frozen_at'] is None and row['created_at']=='created'
    assert not {'quality_status','recommendation_kind','original_quality_status'} & set(row)


def test_price_hierarchy_and_role_sensitive_replacement_reference(environment):
    store,_,client=environment
    legs=[dict(leg_id='l',identity=identity(),options=[dict(option_id='o',market='HAD',selection='H',line=None,original_odds='4')])]
    legacy(store,'prior','superseded',legs);legacy(store,'current','published',legs)
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO audit_events(actor_id,role,event,payload,created_at) VALUES(?,?,?,?,?)',
            ('admin','admin','publication_confirmed',canonical({'publication_id':'current','replaces':'prior'}),'synthetic'))
    for role in ('admin','reviewer','viewer'):
        rows=client.get('/api/ledger',headers={'X-Role':role}).json['data'];current=next(r for r in rows if r['publication_id']=='current')
        assert current['replaces_publication_id']==(None if role=='viewer' else 'prior')
        assert current['legs'][0]['options'][0]['original_odds']=='4'
        assert current['legs'][0]['options'][0]['line'] is None
        assert 'original_odds' not in current and 'line' not in current
        assert current['legs'][0]['home_team'] is None


def test_safe_projection_error_shape(environment):
    response=environment[2].get('/api/schedules?limit=bad')
    assert response.status_code==400
    error=response.json
    assert {'category','code','message','request_id','retryable','field','stage'}<=set(error)
    assert type(error['retryable']) is bool and error['field'] is None and error['stage']=='projection'


@pytest.mark.parametrize('change',[{'evidence_score':69},{'quality_gaps':['missing verified source']}])
def test_qualified_summary_cannot_have_low_score_or_quality_gap(environment,change):
    _,views,_=environment
    row=dict(CASES[1][2],**change)
    assert row['evidence_status']=='qualified'
    with pytest.raises(ValueError):feed(views,feed_name='analyses',rows=[row])
