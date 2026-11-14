from dataclasses import asdict
from contextlib import closing
import pytest
from test_published_settlement import setup,publish,result
from sports_app.app import create_app
from sports_app.auth import Principal


def http(service,role='admin'):
    return create_app(data_dir='unused',principal_loader=lambda req:Principal('http-'+role,role,'csrf'),
                      review_service=service).test_client()
H={'X-CSRF':'csrf','Idempotency-Key':'key'}


def test_initiate_comment_admin_settle_correction_history_statistics(setup):
    store,pub,service=setup;snapshot=publish(pub)
    reviewer=http(service,'reviewer');admin=http(service)
    first=reviewer.post('/api/reviews',json={'publication_id':'p1'},headers=H)
    assert first.status_code==201
    assert reviewer.post('/api/reviews',json={'publication_id':'p1'},headers=H).json==first.json
    assert reviewer.post('/api/reviews/p1/comments',json={'comment':'synthetic private opinion'},headers=H).status_code==201
    results={key:asdict(result(snapshot,key)) for key in ('l1','l2')}
    assert reviewer.post('/api/reviews/p1/settle',json={'results':results},headers=H).status_code==403
    response=admin.post('/api/reviews/p1/settle',json={'results':results},headers=H)
    assert response.status_code==200 and response.json['data']['version']==1
    assert admin.post('/api/reviews/p1/settle',json={'results':results},headers=H).json['data']['version']==1
    results['l1']=asdict(result(snapshot,'l1',(0,1)))
    assert admin.post('/api/reviews/p1/settle',json={'results':results},headers=H).status_code==409
    correction={'results':results,'correction_reason':'official synthetic correction'}
    assert admin.post('/api/reviews/p1/corrections',json=correction,headers=H).json['data']['version']==2
    assert admin.post('/api/reviews/p1/corrections',json=correction,headers=H).json['data']['version']==2
    viewer=http(service,'viewer')
    history=viewer.get('/api/reviews/p1/history?limit=1').json
    assert history['meta']['total']==2 and len(history['data'])==1
    assert 'synthetic-authority' not in str(history) and 'private opinion' not in str(history)
    assert viewer.get('/api/reviews/p1').json['data']['outcome']=='miss'
    assert viewer.get('/api/review-statistics').json['data']['actual_recommendations']==1
    with closing(store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM review_actions').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==2


@pytest.mark.parametrize('role',['viewer','reviewer'])
def test_nonadmin_cannot_settle_or_correct(setup,role):
    _,pub,service=setup;publish(pub)
    client=http(service,role)
    for path in ('settle','corrections'):
        assert client.post('/api/reviews/p1/'+path,json={},headers=H).status_code==403
    if role=='viewer':assert client.post('/api/reviews',json={'publication_id':'p1'},headers=H).status_code==403


def test_no_original_override_and_missing_publication_failure_unchanged(setup):
    store,pub,service=setup;publish(pub);client=http(service)
    assert client.post('/api/reviews/p1/settle',json={'results':{},'legs':[]},headers=H).status_code==400
    assert client.post('/api/reviews',json={'publication_id':'missing'},headers=H).status_code==404
    assert client.post('/api/reviews/p1/settle',json={'results':{}},headers={}).status_code==403
    with closing(store.connect()) as db:
        assert not list(db.execute('SELECT * FROM review_actions'))
        assert not list(db.execute('SELECT * FROM published_settlement_versions'))


def test_statistics_all_history_not_page_and_viewer_scope(setup):
    store,pub,service=setup
    for i in (1,2,3):
        from test_published_settlement import original
        snapshot=original();snapshot['slot_key']=str(i);publish(pub,f'p{i}',snapshot)
    with closing(store.connect()) as db,db:db.execute("UPDATE publications SET status='superseded' WHERE publication_id='p2'")
    assert http(service).get('/api/review-statistics?limit=1').json['data']['actual_recommendations']==3
    visible=http(service,'viewer').get('/api/review-statistics').json['data']
    assert visible['actual_recommendations']==2 and visible['scope']=='visible_publications'
    assert http(service,'viewer').get('/api/reviews/p2/history').status_code==404


def test_comments_metadata_read_permission_and_pagination(setup):
    _,pub,service=setup;publish(pub)
    reviewer=http(service,'reviewer')
    reviewer.post('/api/reviews/p1/comments',json={'comment':'PRIVATE SOURCE https://internal.invalid'},headers=H)
    for role in ('admin','reviewer'):
        response=http(service,role).get('/api/reviews/p1/comments?limit=1')
        assert response.status_code==200 and response.json['meta']['total']==1
        assert response.json['data'][0]['content'] is None
        assert 'PRIVATE SOURCE' not in response.get_data(as_text=True)
    assert http(service,'viewer').get('/api/reviews/p1/comments').status_code==403


def test_pending_reason_preserves_confirmed_miss(setup):
    _,pub,service=setup;snapshot=publish(pub)
    response=http(service).post('/api/reviews/p1/settle',json={'results':{'l1':asdict(result(snapshot,'l1',(0,1)))}},headers=H)
    assert response.status_code==200 and response.json['data']['outcome']=='miss'
    pending=response.json['data']['pending_reasons']
    assert {row['level'] for row in pending}=={'option','leg'}  # branches already confirmed miss


@pytest.mark.parametrize('field',['summary','identity'])
def test_detail_history_safe_reject_nested_historical_values(setup,field):
    from sports_app.publication import canonical
    store,pub,service=setup;snapshot=publish(pub)
    original_hash='synthetic';marker={'secret':['SYNTHETIC_PRIVATE']}
    summary={'outcome':'hit','legs':[{'leg_id':'l1','outcome':marker}]} if field=='summary' else {'outcome':'hit'}
    if field=='identity':
        # Separate synthetic legacy publication, never alter immutable source.
        snapshot['settlement_ticket']['legs'][0]['identity']['home_id']=marker
        publish(pub,'legacy',dict(snapshot,slot_key='legacy-slot'))
    ident='p1' if field=='summary' else 'legacy'
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO published_settlement_versions VALUES(?,?,?,?,?,?,?,?)',
                   (ident,1,original_hash,'{}',canonical(summary),'','synthetic','synthetic-time'))
    for suffix in ('','/history'):
        response=http(service).get('/api/reviews/'+ident+suffix)
        assert response.status_code==500
        assert response.json['code']=='PROJECTION_FAILED' and response.json['request_id']
        assert 'SYNTHETIC_PRIVATE' not in response.get_data(as_text=True)


@pytest.mark.parametrize('exception',[KeyError('SYNTHETIC_PRIVATE'),ValueError('SYNTHETIC_PRIVATE')])
def test_committed_settlement_projection_failure_is_500_and_retry_same_version(setup,monkeypatch,exception):
    from sports_app import review_projection
    store,pub,service=setup;snapshot=publish(pub)
    payload={'results':{key:asdict(result(snapshot,key)) for key in ('l1','l2')}}
    original=review_projection.safe_summary
    def fail(value):raise exception
    monkeypatch.setattr(review_projection,'safe_summary',fail)
    response=http(service).post('/api/reviews/p1/settle',json=payload,headers=H)
    assert response.status_code==500 and response.json['code']=='PROJECTION_FAILED'
    assert response.json['request_id'] and 'SYNTHETIC_PRIVATE' not in response.get_data(as_text=True)
    with closing(store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1
    monkeypatch.setattr(review_projection,'safe_summary',original)
    assert http(service).post('/api/reviews/p1/settle',json=payload,headers=H).json['data']['version']==1
    with closing(store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1


def test_default_unavailable_and_no_anonymous():
    app=create_app(data_dir='unused',principal_loader=lambda req:Principal('a','admin','csrf'))
    assert app.test_client().post('/api/reviews',json={'publication_id':'p'},headers=H).status_code==501
    app=create_app(data_dir='unused',principal_loader=lambda req:None)
    assert app.test_client().get('/api/review-statistics').status_code==401
