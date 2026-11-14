from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from sports_app.read_api import ReadViews
from sports_app.storage import SQLiteStore
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.publication import canonical
from test_pair_policy import ADMIN,AT

ROUTES=['/api/schedules','/api/analyses','/api/recommendations','/api/ledger','/api/reviews',
        '/api/systems/analysis','/api/systems/recommendation']


@pytest.fixture
def setup():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.read-api-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        yield ReadViews(store,clock=lambda:AT)


def client(views,role='viewer'):
    return create_app(data_dir='unused',principal_loader=lambda req:Principal('reader',role,'csrf'),read_views=views).test_client()


def record(views,feed,status='ready',rows=None,ident='snapshot'):
    views.record(ADMIN,'a',snapshot_id=ident,feed=feed,status=status,as_of=AT.isoformat(),
                 valid_until=(AT+timedelta(minutes=5)).isoformat(),rows=rows or [])


@pytest.mark.parametrize('path',ROUTES)
def test_unconfigured_is_not_fake_empty(path):
    response=client(None).get(path)
    assert response.status_code==200 and response.json['meta']['status']=='not_configured'
    assert response.json['data']==[] and response.json['meta']['EV'] is None


def test_snapshot_empty_failed_stale_are_distinct(setup):
    views=setup;http=client(views)
    assert http.get('/api/schedules').json['meta']['status']=='not_configured'
    record(views,'schedules','empty',ident='empty')
    assert http.get('/api/schedules').json['meta']['status']=='empty'
    record(views,'schedules','failed',ident='failed')
    assert http.get('/api/schedules').json['meta']['status']=='failed'
    record(views,'schedules',rows=[{'event_id':'m','status':'scheduled','identity':{'sport':'football'}}],ident='ready')
    views.clock=lambda:AT+timedelta(minutes=6)
    assert http.get('/api/schedules').json['meta']['status']=='stale'


def test_projection_filters_pagination_and_no_private_fields(setup):
    views=setup
    record(views,'analyses',rows=[{'analysis_id':'a','title':'published summary','status':'published',
           'identity':{'sport':'football','business_day':'2026-11-14'},
           'raw_response':'PRIVATE','source_url':'PRIVATE','claim_ids':['PRIVATE']}])
    http=client(views)
    response=http.get('/api/analyses?sport=football&business_day=2026-11-14&limit=1')
    assert response.json['meta']['total']==1
    assert 'PRIVATE' not in response.get_data(as_text=True)
    assert http.get('/api/analyses?sport=basketball').json['data']==[]
    for query in ('limit=0','limit=x','offset=-1','sport=other','business_day=bad','status=unknown','private=1'):
        assert http.get('/api/analyses?'+query).status_code==400
    with pytest.raises(ValueError):record(views,'analyses',rows=[{'analysis_id':'draft','status':'draft'}],ident='draft')


def test_viewer_publication_universe_is_visible_subset_only(setup):
    views=setup
    with closing(views.store.connect()) as db,db:
        for i,status in enumerate(('published','frozen','withdrawn','superseded')):
            original={'recommendation_status':'conditional_low_confidence','raw_response':'PRIVATE',
                      'source_url':'PRIVATE','settlement_ticket':{'legs':[]}}
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                       (f'p{i}',f's{i}','s','m',status,canonical(original),'synthetic-time'))
    for route in ('/api/recommendations','/api/ledger','/api/reviews'):
        response=client(views).get(route)
        assert len(response.json['data'])==2
        assert 'PRIVATE' not in response.get_data(as_text=True)
    assert len(client(views,'admin').get('/api/ledger').json['data'])==4
    assert client(views).get('/api/reviews').json['data'][0]['outcome']=='pending'


def insert_history(views,original):
    with closing(views.store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                   ('history','slot','s','m','published',canonical(original),'synthetic-time'))


def public_leg(ident,sport,day):
    return {'leg_id':ident,'identity':{'sport':sport,'business_day':day},
            'options':[{'option_id':ident+'o','market':'HAD','selection':'H','line':None,'original_odds':'3'}]}


def test_sport_day_filter_matches_same_identity_with_positive_control(setup):
    views=setup
    insert_history(views,{'settlement_ticket':{'legs':[
        public_leg('one','football','2026-11-14'),public_leg('two','basketball','2026-11-15')]}})
    http=client(views)
    assert http.get('/api/ledger?sport=football&business_day=2026-11-15').json['data']==[]
    assert http.get('/api/ledger?sport=basketball&business_day=2026-11-14').json['data']==[]
    assert len(http.get('/api/ledger?sport=football&business_day=2026-11-14').json['data'])==1
    assert len(http.get('/api/ledger?sport=basketball&business_day=2026-11-15').json['data'])==1


@pytest.mark.parametrize('field',['recommendation_status','leg_id','sport','option_id','line','original_odds'])
def test_nested_historical_public_fields_rejected_without_marker_leak(setup,field):
    views=setup;leg=public_leg('one','football','2026-11-14')
    original={'recommendation_status':'conditional_low_confidence','settlement_ticket':{'legs':[leg]}}
    marker={'nested':['SYNTHETIC_PRIVATE_MARKER']}
    if field=='recommendation_status':original[field]=marker
    elif field=='leg_id':leg[field]=marker
    elif field=='sport':leg['identity'][field]=marker
    else:leg['options'][0][field]=marker
    insert_history(views,original)
    for route in ('/api/recommendations','/api/ledger','/api/reviews'):
        response=client(views).get(route)
        assert response.status_code==500 and response.json['request_id']
        assert 'SYNTHETIC_PRIVATE_MARKER' not in response.get_data(as_text=True)


def test_approved_analysis_and_system_minimum_fields(setup):
    views=setup
    record(views,'analyses',rows=[{'analysis_id':'a','publication_id':'history','status':'completed',
        'task_stage':'evidence_review','evidence_score':68,'evidence_status':'partial',
        'quality_gaps':['synthetic gap'],'covered_paths':['path'],'excluded_paths':[],
        'failure_paths':['failure'],'withdraw_if':['withdraw'],'reasons':['approved reason'],
        'strongest_counterevidence':'approved counter','risk':'approved-risk'}])
    analysis=client(views).get('/api/analyses').json['data'][0]
    assert analysis['evidence_score']==68 and analysis['task_stage']=='evidence_review'
    assert analysis['source_published_at'] is None
    assert analysis['field_coverage']['source_published_at']=='unknown'
    insert_history(views,{'recommendation_type':'regular_pair','recommendation_status':'conditional_low_confidence',
                          'settlement_ticket':{'legs':[]},'reasons':['INTERNAL UNAPPROVED']})
    recommendation=client(views).get('/api/recommendations').json['data'][0]
    assert recommendation['reasons']==['approved reason'] and recommendation['risk']=='approved-risk'
    assert recommendation['frozen_at'] is None and recommendation['period'] is None
    assert 'quality_status' not in recommendation
    record(views,'analysis-system',rows=[{'status':'not_integrated','policy_revision':'p','analysis_revision':'a',
        'runtime_provider':'codex','coverage_status':'partial','validation_level':'defined',
        'dimensions':['identity'],'event_coverage':['summary'],'notices':['not real execution']}],ident='system')
    system=client(views).get('/api/systems/analysis').json['data'][0]
    assert system['runtime_provider']=='codex' and system['validation_level']=='defined'
    assert system['notices']==['not real execution']


def test_unknown_research_not_automatically_disclosed_and_safe_error_contract(setup):
    views=setup
    insert_history(views,{'recommendation_status':'conditional_low_confidence','reasons':['INTERNAL UNAPPROVED'],
                          'risk':'high','settlement_ticket':{'legs':[]}})
    recommendation=client(views).get('/api/recommendations').json['data'][0]
    assert recommendation['reasons']==[] and recommendation['risk'] is None
    assert recommendation['field_coverage']['risk']=='unknown'
    error=client(views).get('/api/analyses?limit=0').json
    assert error['category']=='HARD_BLOCK' and error['retryable'] is False
    assert error['field'] is None and error['stage']=='projection'


@pytest.mark.parametrize('stage',['collecting','researching','drafting','evidence_review',None])
def test_public_research_stage_explicit_enum(setup,stage):
    record(setup,'analyses',rows=[{'analysis_id':'a','status':'completed','task_stage':stage}])
    row=client(setup).get('/api/analyses').json['data'][0]
    assert row['task_stage']==stage and row['evidence_status']=='unknown'


@pytest.mark.parametrize('state',['unknown','partial','qualified','conflict'])
def test_public_evidence_state_explicit_enum(setup,state):
    record(setup,'analyses',rows=[{'analysis_id':'a','status':'completed','evidence_status':state,
                                  'evidence_score':80 if state=='qualified' else None,'quality_gaps':[]}])
    assert client(setup).get('/api/analyses').json['data'][0]['evidence_status']==state


@pytest.mark.parametrize('extra',[{'task_stage':'decision_validated'},{'task_stage':'arbitrary-success'},
    {'evidence_status':'passed'},{'evidence_status':'qualified','evidence_score':None},
    {'evidence_status':'qualified','evidence_score':69},
    {'evidence_status':'qualified','evidence_score':80,'quality_gaps':['missing receipt']}])
def test_approval_does_not_allow_invalid_or_inconsistent_enums(setup,extra):
    with pytest.raises(ValueError):
        record(setup,'analyses',rows=[dict(analysis_id='a',status='completed',**extra)])


def test_anonymous_unknown_role_and_internal_error_safe(setup,monkeypatch):
    views=setup
    app=create_app(data_dir='unused',principal_loader=lambda req:None,read_views=views)
    assert app.test_client().get('/api/schedules').status_code==401
    assert client(views,'unknown').get('/api/schedules').status_code==403
    def failure(name):raise RuntimeError('PRIVATE INTERNAL MARKER')
    monkeypatch.setattr(views,'feed',failure)
    response=client(views).get('/api/schedules')
    assert response.status_code==500 and response.json['request_id']
    assert 'PRIVATE' not in response.get_data(as_text=True)
