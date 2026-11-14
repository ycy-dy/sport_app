"""Complete synthetic server-chain HTTP, no permissive gate injection."""
from contextlib import closing
from threading import Barrier, Thread
import pytest
from test_local_assembly_contract import assembly_case, build_chain
from test_pair_policy_contract import ADMIN, REVIEWER, AT
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.publication_http import LocalPublicationChain


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.fixture
def environment(assembly_case):
    repo,policy,runtime,_=build_chain(assembly_case)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    connected=LocalPublicationChain(runtime)
    def loader(req):
        role=req.headers.get('X-Role','admin')
        if role=='anonymous':return None
        return {'admin':ADMIN,'reviewer':REVIEWER,'viewer':Principal('v','viewer','v')}[role]
    app=create_app(data_dir=str(repo.store.data_dir),principal_loader=loader,publication_chain=connected)
    app.config['TESTING']=True
    return repo,policy,runtime,connected,app


def headers(role='admin',key='key',csrf=True):
    h={'X-Role':role,'Idempotency-Key':key}
    if csrf:h['X-CSRF']={'admin':'a','reviewer':'r','viewer':'v','anonymous':'none'}[role]
    return h


def source():return dict(batch_id='batch',candidate_id='pair',task_id='task')


def state(repo):
    with closing(repo.store.connect()) as db:
        return {table:sorted(tuple(r) for r in db.execute('SELECT * FROM '+table))
                for table in ('evidence_records','publication_drafts','publication_reviews','publications','publication_requests','audit_events')}


def audited(client,key='d'):
    response=client.post('/api/publication-drafts',json=source(),headers=headers(key=key))
    assert response.status_code==201,response.text
    draft=response.json['data']['draft_id']
    response=client.post(f'/api/publication-drafts/{draft}/audit',json={},headers=headers('reviewer','r'+key))
    assert response.status_code==200,response.text
    return draft,response.json['data']['review_id']


def confirm(client,draft,review,key='confirm',replace=None):
    payload=dict(review_id=review,human_confirmed=True)
    if replace:payload['replace_id']=replace
    return client.post(f'/api/publications/{draft}/confirm',json=payload,headers=headers(key=key))


def test_complete_server_chain_and_sanitized_viewer_ledger(environment):
    repo,_,_,_,app=environment;client=app.test_client();draft,review=audited(client)
    response=confirm(client,draft,review);assert response.status_code==200
    assert confirm(client,draft,review).json==response.json
    listing=client.get('/api/publications',headers=headers('viewer'))
    assert listing.status_code==200 and len(listing.json['data'])==1
    assert 'raw_response' not in listing.text and 'source_quotes' not in listing.text
    assert listing.json['data'][0]['model_probability'] is None and listing.json['data'][0]['EV'] is None
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0]==1


@pytest.mark.parametrize('extra',[{'evidence_score':100},{'eligible':True},{'settlement_ticket':{}},{'audited':True}])
def test_client_authorization_fields_rejected_without_writes(environment,extra):
    repo,_,_,_,app=environment;before=state(repo)
    response=app.test_client().post('/api/publication-drafts',json=dict(source(),**extra),headers=headers())
    assert response.status_code==400 and state(repo)==before


@pytest.mark.parametrize('role,expected',[('anonymous',401),('viewer',403),('reviewer',403)])
def test_draft_creation_role_matrix(environment,role,expected):
    repo,_,_,_,app=environment;before=state(repo)
    assert app.test_client().post('/api/publication-drafts',json=source(),headers=headers(role)).status_code==expected
    assert state(repo)==before


def test_csrf_and_reviewer_cannot_confirm(environment):
    repo,_,_,_,app=environment;client=app.test_client();before=state(repo)
    assert client.post('/api/publication-drafts',json=source(),headers=headers(csrf=False)).status_code==403
    assert state(repo)==before
    draft,review=audited(client);before=state(repo)
    assert client.post(f'/api/publications/{draft}/confirm',json=dict(review_id=review,human_confirmed=True),headers=headers('reviewer')).status_code==403
    assert state(repo)==before


def test_default_no_chain_501(environment):
    repo,_,_,_,_=environment
    app=create_app(data_dir=str(repo.store.data_dir),principal_loader=lambda req:ADMIN)
    assert app.test_client().post('/api/publication-drafts',json=source(),headers=headers()).status_code==501


def test_missing_execution_does_not_freeze_or_save_draft(environment):
    repo,_,_,_,app=environment;before=state(repo)
    response=app.test_client().post('/api/publication-drafts',json=dict(source(),task_id='missing'),headers=headers())
    assert response.status_code in {403,409} and state(repo)==before


def test_freeze_and_draft_insert_failure_roll_back_together(environment):
    repo,_,_,_,app=environment
    with closing(repo.store.connect()) as db,db:
        db.execute("CREATE TRIGGER independent_draft_failure BEFORE INSERT ON publication_drafts BEGIN SELECT RAISE(ABORT,'synthetic draft failure'); END")
    before=state(repo)
    response=app.test_client().post('/api/publication-drafts',json=source(),headers=headers())
    assert response.status_code>=400
    assert state(repo)==before,'Failed draft must not leave orphan recommendation freeze'


@pytest.mark.parametrize('operation',['draft','audit'])
def test_concurrent_same_key_draft_and_audit_idempotent(environment,operation):
    repo,_,_,_,app=environment;client=app.test_client()
    draft=None
    if operation=='audit':draft=client.post('/api/publication-drafts',json=source(),headers=headers()).json['data']['draft_id']
    barrier=Barrier(2);responses=[];errors=[]
    def worker():
        try:
            with app.test_client() as local:
                barrier.wait(timeout=10)
                response=(local.post('/api/publication-drafts',json=source(),headers=headers()) if operation=='draft' else
                          local.post(f'/api/publication-drafts/{draft}/audit',json={},headers=headers('reviewer')))
                responses.append((response.status_code,response.json))
        except BaseException as exc:errors.append(repr(exc))
    threads=[Thread(target=worker,daemon=True) for _ in range(2)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(15)
    assert not any(thread.is_alive() for thread in threads) and errors==[]
    assert all(code in {200,201} for code,_ in responses),responses
    field='draft_id' if operation=='draft' else 'review_id'
    assert len({body['data'][field] for _,body in responses})==1
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publication_drafts').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM publication_reviews').fetchone()[0]==(1 if operation=='audit' else 0)


@pytest.mark.parametrize('drift',['source_conflict','version'])
def test_confirmation_rechecks_sources_and_versions(environment,drift):
    from hashlib import sha256
    from independent_decision_review_fixture import READER
    repo,policy,_,_,app=environment;client=app.test_client();draft,review=audited(client)
    if drift=='source_conflict':
        claim=next(c for c in repo.load_context('o1',as_of=AT).claims if c.claim_id=='review-source-o1')
        repo.verify(READER,'r',receipt_id='new-conflict',claim_id=claim.claim_id,
                    quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='conflict')
    else:policy.approved_manifest=dict(policy.approved_manifest,code_hash='changed')
    before=state(repo);response=confirm(client,draft,review)
    assert response.status_code in {403,409} and state(repo)==before


def test_replacement_failure_preserves_old_ticket_and_viewer_hides_history(environment):
    repo,_,_,_,app=environment;client=app.test_client();draft,review=audited(client)
    old=confirm(client,draft,review).json['data']['publication_id']
    draft2,review2=audited(client,'d2')
    with closing(repo.store.connect()) as db,db:
        db.execute("CREATE TRIGGER independent_publish_failure BEFORE INSERT ON publications BEGIN SELECT RAISE(ABORT,'synthetic publish failure'); END")
    before=state(repo);response=confirm(client,draft2,review2,'confirm2',old)
    assert response.status_code>=400 and state(repo)==before
    with closing(repo.store.connect()) as db,db:
        db.execute('DROP TRIGGER independent_publish_failure')
    new=confirm(client,draft2,review2,'confirm2',old).json['data']['publication_id']
    assert client.get('/api/publications/'+old,headers=headers('viewer')).status_code==404
    assert len(client.get('/api/publications',headers=headers('viewer')).json['data'])==1
    with closing(repo.store.connect()) as db,db:
        db.execute("UPDATE publications SET status='withdrawn' WHERE publication_id=?",(new,))
    assert client.get('/api/publications/'+new,headers=headers('viewer')).status_code==404
    assert client.get('/api/publications',headers=headers('viewer')).json['data']==[]
