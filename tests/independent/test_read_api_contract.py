"""Seven read APIs: independent public projections, no external data."""
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import pytest
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.read_api import ReadViews
from sports_app.publication import canonical
from test_pair_policy_contract import AT,ADMIN

ROUTES=('/api/schedules','/api/analyses','/api/recommendations','/api/ledger','/api/reviews',
        '/api/systems/analysis','/api/systems/recommendation')


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.fixture
def environment(tmp_path):
    root=Path(__file__).resolve().parents[2];assert tmp_path.resolve().is_relative_to(root)
    store=SQLiteStore(tmp_path/'synthetic',app_root=root);views=ReadViews(store,clock=lambda:AT)
    def loader(req):
        role=req.headers.get('X-Role','viewer')
        return None if role=='anonymous' else Principal(role,role,'csrf')
    app=create_app(data_dir=str(store.data_dir),principal_loader=loader,read_views=views)
    return store,views,app.test_client()


def identity(sport='football',day='2026-11-14',ident='m'):
    return dict(sport=sport,league='synthetic',business_day=day,match_id=ident,
                kickoff='2026-11-14T18:00:00+08:00',home_id='h',away_id='a')


def feed(views,status='ready',feed_name='schedules',ident='snapshot',rows=None):
    if rows is None:rows=[dict(event_id='event',status='scheduled',identity=identity())] if status=='ready' else []
    views.record(ADMIN,'a',snapshot_id=ident,feed=feed_name,status=status,as_of=AT.isoformat(),
                 valid_until=(AT+timedelta(minutes=5)).isoformat(),rows=rows)


def legacy(store,ident='publication',status='published',legs=None):
    original=dict(recommendation_status='conditional_low_confidence',raw_response='PRIVATE_RAW',
                  source_quotes={'quote':'PRIVATE_QUOTE'})
    if legs is not None:original['settlement_ticket']={'legs':legs}
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                   (ident,'slot-'+ident,'selection','manifest',status,canonical(original),'synthetic-time'))


@pytest.mark.parametrize('route',ROUTES)
def test_seven_routes_authentication_and_authorized_read(environment,route):
    _,_,client=environment
    assert client.get(route,headers={'X-Role':'anonymous'}).status_code==401
    assert client.get(route,headers={'X-Role':'unknown'}).status_code==403
    for role in ('admin','reviewer','viewer'):
        response=client.get(route,headers={'X-Role':role})
        assert response.status_code==200 and response.json['data']==[]
        assert response.json['meta']['model_probability'] is None and response.json['meta']['EV'] is None


def test_missing_empty_failed_stale_are_distinct(environment):
    _,views,client=environment
    assert client.get('/api/schedules').json['meta']['status']=='not_configured'
    feed(views,'empty',ident='empty');assert client.get('/api/schedules').json['meta']['status']=='empty'
    feed(views,'failed',ident='failed');assert client.get('/api/schedules').json['meta']['status']=='failed'
    feed(views,ident='ready');assert client.get('/api/schedules').json['meta']['status']=='ready'
    views.clock=lambda:AT+timedelta(minutes=5)
    result=client.get('/api/schedules').json
    assert result['meta']['status']=='stale' and len(result['data'])==1


@pytest.mark.parametrize('query',['limit=0','limit=x','offset=-1','sport=unknown','business_day=2026-99-99','extra=1'])
def test_invalid_filters_are_400(environment,query):
    assert environment[2].get('/api/schedules?'+query).status_code==400


def test_filter_before_pagination_and_metadata(environment):
    _,views,client=environment
    rows=[dict(event_id=str(i),status='scheduled',identity=identity('football' if i%2==0 else 'basketball',ident=str(i))) for i in range(6)]
    feed(views,rows=rows)
    result=client.get('/api/schedules?sport=football&limit=1&offset=1').json
    assert result['meta']['total']==3 and [r['event_id'] for r in result['data']]==['2']


def test_nested_publication_fields_cannot_smuggle_private_objects(environment):
    store,_,client=environment
    # Legacy malformed nested fields must be omitted or safely rejected, never serialized raw.
    legs=[dict(leg_id='leg',identity=dict(identity(),home_id={'token':'PRIVATE_IDENTITY'}),
               options=[dict(option_id='o',market='HAD',selection='H',line={'quote':'PRIVATE_LINE'},original_odds='4')])]
    legacy(store,legs=legs)
    for route in ('/api/recommendations','/api/ledger','/api/reviews'):
        response=client.get(route)
        assert 'PRIVATE_' not in response.text
        assert response.status_code in {200,500}


def test_feed_projection_drops_arbitrary_nested_fields(environment):
    _,views,client=environment
    row=dict(event_id='e',status='scheduled',identity=dict(identity(),source_url='PRIVATE_URL'),
             raw_response={'token':'PRIVATE_RAW'},claims=[{'quote':'PRIVATE_QUOTE'}])
    feed(views,rows=[row]);response=client.get('/api/schedules')
    assert response.status_code==200 and 'PRIVATE_' not in response.text


def test_viewer_hides_withdrawn_superseded_and_review_coverage(environment):
    store,_,client=environment
    for status in ('published','frozen','withdrawn','superseded'):legacy(store,status,status)
    for route in ('/api/ledger','/api/reviews'):
        rows=client.get(route).json['data']
        assert {r['status'] for r in rows}=={'published','frozen'}
        assert len(client.get(route,headers={'X-Role':'admin'}).json['data'])==4
    rows=client.get('/api/reviews').json['data']
    assert all(r['outcome']=='pending' and r['leg_statistics_coverage']=='insufficient_frozen_structure' for r in rows)


def test_sport_and_day_must_match_same_identity(environment):
    store,_,client=environment
    legs=[dict(leg_id='left',identity=identity('football','2026-11-14','left'),options=[]),
          dict(leg_id='right',identity=identity('basketball','2026-11-15','right'),options=[])]
    legacy(store,legs=legs)
    for route in ('/api/recommendations','/api/ledger','/api/reviews'):
        response=client.get(route+'?sport=football&business_day=2026-11-15')
        assert response.status_code==200 and response.json['data']==[]
        assert response.json['meta']['total']==0
