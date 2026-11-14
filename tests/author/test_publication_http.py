"""Full server ID publication chain with fake executor; never real publishing."""
from contextlib import closing
from copy import deepcopy
import pytest
from test_offline_integration import chain,runtime_for,response
from test_pair_policy import ADMIN,REVIEWER
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.publication_http import LocalPublicationChain


def http(runtime,publication_chain,actor=ADMIN):
    return create_app(data_dir=str(runtime.store.data_dir),principal_loader=lambda req:actor,
                      publication_chain=publication_chain).test_client()

H={'X-CSRF':'a','Idempotency-Key':'draft1'}


def draft(client):
    return client.post('/api/publication-drafts',json={'batch_id':'batch','candidate_id':'pair1','task_id':'task'},headers=H)


def test_server_ids_audit_admin_confirm_and_redacted_ledger(chain):
    runtime=runtime_for(chain,lambda body:response());runtime.execute(ADMIN,'a','task')
    connected=LocalPublicationChain(runtime);admin=http(runtime,connected)
    result=draft(admin);assert result.status_code==201
    ident=result.json['data']['draft_id']
    assert draft(admin).json['data']['draft_id']==ident
    reviewer=http(runtime,connected,REVIEWER)
    audited=reviewer.post(f'/api/publication-drafts/{ident}/audit',json={},headers={'X-CSRF':'r','Idempotency-Key':'review'})
    assert audited.status_code==200
    review=audited.json['data']['review_id']
    payload={'review_id':review,'human_confirmed':True}
    assert reviewer.post(f'/api/publications/{ident}/confirm',json=payload,headers={'X-CSRF':'r','Idempotency-Key':'confirm'}).status_code==403
    headers={'X-CSRF':'a','Idempotency-Key':'confirm'}
    result=admin.post(f'/api/publications/{ident}/confirm',json=payload,headers=headers)
    assert result.status_code==200
    publication=result.json['data']['publication_id']
    assert admin.post(f'/api/publications/{ident}/confirm',json=payload,headers=headers).json==result.json
    viewer=http(runtime,connected,Principal('viewer','viewer','v'))
    listing=viewer.get('/api/publications')
    assert listing.status_code==200 and len(listing.json['data'])==1
    assert listing.json['data'][0]['model_probability'] is None
    assert 'raw_response' not in listing.get_data(as_text=True)
    assert viewer.get('/api/publications/'+publication).status_code==200
    with closing(runtime.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0]==1


@pytest.mark.parametrize('mutation',['score','snapshot','approval','csrf','missing_ai'])
def test_client_cannot_supply_authorization_or_publish_without_server_execution(chain,mutation):
    runtime=runtime_for(chain,lambda body:response())
    if mutation!='missing_ai':runtime.execute(ADMIN,'a','task')
    connected=LocalPublicationChain(runtime);client=http(runtime,connected)
    payload={'batch_id':'batch','candidate_id':'pair1','task_id':'task'}
    headers=dict(H)
    if mutation=='score':payload['evidence_score']=100
    elif mutation=='snapshot':payload['settlement_ticket']={}
    elif mutation=='approval':payload['eligible']=True
    elif mutation=='csrf':headers.pop('X-CSRF')
    result=client.post('/api/publication-drafts',json=payload,headers=headers)
    assert result.status_code in {400,403,409}
    with closing(runtime.store.connect()) as db:
        assert not list(db.execute('SELECT * FROM publications'))
        assert not list(db.execute('SELECT * FROM publication_drafts'))


def table_counts(store):
    with closing(store.connect()) as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                     ('evidence_records','publication_drafts','publication_reviews','publications','audit_events'))


def test_draft_insert_failure_rolls_back_recommendation_freeze(chain):
    runtime=runtime_for(chain,lambda body:response());runtime.execute(ADMIN,'a','task')
    connected=LocalPublicationChain(runtime);client=http(runtime,connected)
    before=table_counts(runtime.store)
    with closing(runtime.store.connect()) as db,db:
        db.execute("CREATE TRIGGER synthetic_draft_failure BEFORE INSERT ON publication_drafts "
                   "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    assert draft(client).status_code==409
    assert table_counts(runtime.store)==before


@pytest.mark.parametrize('operation',['draft','audit'])
def test_same_key_concurrent_requests_return_same_id(chain,operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    runtime=runtime_for(chain,lambda body:response());runtime.execute(ADMIN,'a','task')
    connected=LocalPublicationChain(runtime)
    draft_id=draft(http(runtime,connected)).json['data']['draft_id'] if operation=='audit' else None
    barrier=Barrier(2)
    def call():
        client=http(runtime,connected,REVIEWER if operation=='audit' else ADMIN)
        barrier.wait(timeout=10)
        if operation=='draft':return draft(client)
        return client.post(f'/api/publication-drafts/{draft_id}/audit',json={},
                           headers={'X-CSRF':'r','Idempotency-Key':'concurrent-audit'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(call) for _ in range(2)]
        results=[future.result(timeout=30) for future in futures]
    assert [r.status_code for r in results]==([201,201] if operation=='draft' else [200,200])
    field='draft_id' if operation=='draft' else 'review_id'
    assert results[0].json['data'][field]==results[1].json['data'][field]
    with closing(runtime.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publication_drafts').fetchone()[0]==1
        if operation=='audit':assert db.execute('SELECT COUNT(*) FROM publication_reviews').fetchone()[0]==1


def test_default_chain_unavailable_and_viewer_no_draft_write(chain):
    runtime=runtime_for(chain,lambda body:response())
    assert draft(http(runtime,None)).status_code==501
    viewer=http(runtime,LocalPublicationChain(runtime),Principal('v','viewer','v'))
    assert viewer.post('/api/publication-drafts',json={},headers={'X-CSRF':'v'}).status_code==403
    assert viewer.get('/api/publications').json['data']==[]
