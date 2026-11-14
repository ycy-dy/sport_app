"""Independent review HTTP; full synthetic publication chain, no real results."""
from contextlib import closing
from dataclasses import asdict
from copy import deepcopy
from threading import Barrier,Thread
import pytest
from test_local_assembly_contract import assembly_case,build_chain
from test_pair_policy_contract import ADMIN,REVIEWER
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.publication_http import LocalPublicationChain
from sports_app.published_settlement import PublishedSettlementService
from sports_app.publication import canonical
from sports_app.settlement import Identity,ResultSnapshot


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def h(role='admin',key='key',csrf=True):
    result={'X-Role':role,'Idempotency-Key':key}
    if csrf:result['X-CSRF']={'admin':'a','reviewer':'r','viewer':'v','anonymous':'x'}[role]
    return result


@pytest.fixture
def case(assembly_case):
    repo,policy,runtime,_=build_chain(assembly_case)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    chain=LocalPublicationChain(runtime);service=PublishedSettlementService(repo.store)
    def loader(req):
        role=req.headers.get('X-Role','admin')
        return None if role=='anonymous' else {'admin':ADMIN,'reviewer':REVIEWER,'viewer':Principal('v','viewer','v')}[role]
    app=create_app(data_dir=str(repo.store.data_dir),principal_loader=loader,publication_chain=chain,review_service=service)
    client=app.test_client()
    draft=client.post('/api/publication-drafts',json=dict(batch_id='batch',candidate_id='pair',task_id='task'),headers=h()).json['data']['draft_id']
    review=client.post(f'/api/publication-drafts/{draft}/audit',json={},headers=h('reviewer')).json['data']['review_id']
    publication=client.post(f'/api/publications/{draft}/confirm',json=dict(review_id=review,human_confirmed=True),headers=h()).json['data']['publication_id']
    with closing(repo.store.connect()) as db:
        original=db.execute('SELECT original_json FROM publications WHERE publication_id=?',(publication,)).fetchone()[0]
    import json
    ticket=json.loads(original)['settlement_ticket']
    results={leg['leg_id']:asdict(ResultSnapshot(Identity(**leg['identity']),'finished','H:A',(2,0),
        'regulation_90',source_id='PRIVATE_SOURCE')) for leg in ticket['legs']}
    return repo,service,app,publication,results,original


def rows(repo):
    with closing(repo.store.connect()) as db:
        return {table:sorted(tuple(row) for row in db.execute('SELECT * FROM '+table)) for table in
                ('publications','published_settlement_versions','review_actions')}


def test_requests_comments_idempotent_and_never_settle(case):
    repo,_,app,p,_,_=case;client=app.test_client()
    for role in ('admin','reviewer'):
        a=client.post('/api/reviews',json={'publication_id':p},headers=h(role))
        b=client.post('/api/reviews',json={'publication_id':p},headers=h(role))
        assert a.status_code==201 and a.json==b.json
        a=client.post(f'/api/reviews/{p}/comments',json={'comment':'PRIVATE_COMMENT'},headers=h(role))
        assert client.post(f'/api/reviews/{p}/comments',json={'comment':'PRIVATE_COMMENT'},headers=h(role)).json==a.json
        assert client.post(f'/api/reviews/{p}/comments',json={'comment':'different'},headers=h(role)).status_code==409
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM review_actions').fetchone()[0]==4


@pytest.mark.parametrize('role,expected',[('viewer',403),('reviewer',403),('anonymous',401)])
def test_settlement_write_role_boundary(case,role,expected):
    repo,_,app,p,results,_=case;before=rows(repo);client=app.test_client()
    for action,payload in [('settle',{'results':results}),('corrections',{'results':results,'correction_reason':'x'})]:
        assert client.post(f'/api/reviews/{p}/{action}',json=payload,headers=h(role)).status_code==expected
    assert rows(repo)==before


def test_csrf_default_and_forbidden_original_fields(case):
    repo,_,app,p,results,_=case;client=app.test_client();before=rows(repo)
    assert client.post('/api/reviews',json={'publication_id':p},headers=h(csrf=False)).status_code==403
    assert client.post(f'/api/reviews/{p}/settle',json={'results':results,'original':{}},headers=h()).status_code==400
    assert rows(repo)==before
    default=create_app(data_dir='unused',principal_loader=lambda req:ADMIN).test_client()
    assert default.post('/api/reviews',json={'publication_id':p},headers=h()).status_code==501


def test_initial_correction_replay_history_and_original_immutable(case):
    repo,_,app,p,results,original=case;client=app.test_client()
    assert client.post(f'/api/reviews/{p}/corrections',json={'results':results,'correction_reason':'first'},headers=h()).status_code==409
    first=client.post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
    assert first.status_code==200 and first.json['data']['version']==1
    assert client.post(f'/api/reviews/{p}/settle',json={'results':results},headers=h()).json==first.json
    changed=deepcopy(results);next(iter(changed.values()))['scores']=[0,2]
    assert client.post(f'/api/reviews/{p}/settle',json={'results':changed},headers=h()).status_code==409
    corrected=client.post(f'/api/reviews/{p}/corrections',json={'results':changed,'correction_reason':'synthetic correction'},headers=h())
    assert corrected.status_code==200 and corrected.json['data']['version']==2
    assert client.post(f'/api/reviews/{p}/corrections',json={'results':changed,'correction_reason':'synthetic correction'},headers=h()).json==corrected.json
    history=client.get(f'/api/reviews/{p}/history?limit=1&offset=1').json
    assert history['meta']['total']==2 and len(history['data'])==1
    assert client.get('/api/review-statistics?limit=1').json['data']['actual_recommendations']==1
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT original_json FROM publications WHERE publication_id=?',(p,)).fetchone()[0]==original


@pytest.mark.parametrize('mode',['miss_pending','wrong_identity','reverse'])
def test_score_direction_and_pending_http(case,mode):
    _,_,app,p,results,_=case;values=deepcopy(results);ids=list(values)
    if mode=='miss_pending':values={ids[0]:dict(values[ids[0]],scores=[0,2])}
    elif mode=='wrong_identity':values[ids[0]]['identity']['match_id']='wrong'
    else:
        for value in values.values():value.update(orientation='A:H',scores=[0,2])
    response=app.test_client().post(f'/api/reviews/{p}/settle',json={'results':values},headers=h())
    assert response.status_code==200
    assert response.json['data']['outcome']=={'miss_pending':'miss','wrong_identity':'pending','reverse':'hit'}[mode]


def test_settlement_insert_fault_has_no_side_effect(case):
    repo,_,app,p,results,_=case
    with closing(repo.store.connect()) as db,db:
        db.execute("CREATE TRIGGER independent_settlement_failure BEFORE INSERT ON published_settlement_versions BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before=rows(repo)
    assert app.test_client().post(f'/api/reviews/{p}/settle',json={'results':results},headers=h()).status_code>=400
    assert rows(repo)==before


def test_concurrent_identical_correction_only_one_version(case):
    repo,_,app,p,results,_=case;app.test_client().post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
    values=deepcopy(results);next(iter(values.values()))['scores']=[0,2]
    barrier=Barrier(2);responses=[];errors=[]
    def worker():
        try:
            with app.test_client() as client:
                barrier.wait(timeout=10)
                r=client.post(f'/api/reviews/{p}/corrections',json={'results':values,'correction_reason':'same'},headers=h())
                responses.append((r.status_code,r.json))
        except BaseException as exc:errors.append(repr(exc))
    threads=[Thread(target=worker,daemon=True) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(15)
    assert not any(t.is_alive() for t in threads) and not errors
    assert len(responses)==2 and all(code==200 for code,_ in responses)
    assert all(body['data']['version']==2 for _,body in responses)
    with closing(repo.store.connect()) as db:assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==2


@pytest.mark.parametrize('mutation',['summary','identity'])
def test_malformed_legacy_public_projection_never_leaks(case,mutation):
    repo,_,app,p,results,_=case;client=app.test_client()
    if mutation=='summary':
        # Append legacy version before any settlement, preserving immutable record semantics.
        with closing(repo.store.connect()) as db,db:
            db.execute('INSERT INTO published_settlement_versions VALUES(?,?,?,?,?,?,?,?)',
                (p,1,'synthetic',canonical(results),canonical({'outcome':'pending','legs':[{'private':'PRIVATE_SUMMARY'}]}),'legacy','admin','synthetic'))
    else:
        with closing(repo.store.connect()) as db,db:
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',('legacy','legacy-slot','s','m','published',
                canonical({'settlement_ticket':{'legs':[{'leg_id':'l','identity':{'home_id':{'token':'PRIVATE_IDENTITY'}}}]}}),'synthetic'))
        p='legacy'
    for route in (f'/api/reviews/{p}',f'/api/reviews/{p}/history'):
        response=client.get(route,headers=h('viewer'))
        assert 'PRIVATE_' not in response.text
        assert response.status_code in {200,500}


def test_statistics_visible_universe_not_history_page_and_legacy_coverage(case):
    repo,_,app,p,results,_=case;client=app.test_client()
    client.post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
    with closing(repo.store.connect()) as db,db:
        for status in ('withdrawn','superseded'):
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',(status,status,'s','m',status,'{}','synthetic'))
    admin=client.get('/api/review-statistics').json['data']
    viewer=client.get('/api/review-statistics',headers=h('viewer')).json['data']
    assert admin['actual_recommendations']==3 and admin['tickets_missing_leg_structure']==2
    assert viewer['actual_recommendations']==1 and viewer['scope']=='visible_publications'
    assert client.get('/api/reviews/withdrawn',headers=h('viewer')).status_code==404
