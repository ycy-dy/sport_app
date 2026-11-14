"""HTTP uses fake executor and synthetic isolated repository only."""
from datetime import timedelta
from contextlib import closing
import pytest
from test_pair_policy import setup,ADMIN,AT
from test_task_runtime import prepare
from sports_app.app import create_app
from sports_app.auth import Principal


def client(runtime,role='admin'):
    actor=Principal('http-'+role,role,'csrf')
    app=create_app(data_dir=str(runtime.store.data_dir),principal_loader=lambda req:actor,task_runtime=runtime)
    app.config['TESTING']=True
    return app.test_client()


def body():return {'registry_id':'rules','batch_id':'b','deadline':(AT+timedelta(minutes=5)).isoformat()}
HEADERS={'X-CSRF':'csrf','Idempotency-Key':'create1'}


def test_create_query_execute_and_events_match_repository(setup):
    evidence,policy=setup
    calls=[]
    runtime=prepare(policy,evidence,lambda value:(calls.append(value) or {'raw_response':'synthetic'}))
    http=client(runtime)
    response=http.post('/api/analysis-runs',json=body(),headers=HEADERS)
    assert response.status_code==201
    task=response.json['data']['task_id']
    assert response.json['data']['status']=='created'
    assert http.post('/api/analysis-runs',json=body(),headers=HEADERS).json['data']['task_id']==task
    assert http.get('/api/analysis-runs').json['data'][0]['task_id']==task
    assert http.post(f'/api/analysis-runs/{task}/execute',headers=HEADERS).json['data']['status']=='completed'
    assert http.post(f'/api/analysis-runs/{task}/execute',headers=HEADERS).status_code==200
    assert len(calls)==1
    data=http.get(f'/api/analysis-runs/{task}/events').json['data']
    assert [e['status'] for e in data['events']]==['created','running','completed']
    assert data['stage']=='executor_return_recorded_not_decision_validated'
    assert data['model_probability'] is None and data['EV'] is None
    with closing(evidence.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM manual_tasks').fetchone()[0]==1


@pytest.mark.parametrize('role',['viewer','reviewer'])
def test_nonadmin_denied_task_write_and_raw_research_read(setup,role):
    evidence,policy=setup;runtime=prepare(policy,evidence,lambda v:{'raw_response':'synthetic'})
    http=client(runtime,role)
    assert http.post('/api/analysis-runs',json=body(),headers=HEADERS).status_code==403
    assert http.get('/api/analysis-runs').status_code==(200 if role=='reviewer' else 403)


def test_missing_executor_csrf_and_same_key_different_body(setup):
    evidence,policy=setup;runtime=prepare(policy,evidence,None)
    http=client(runtime)
    assert http.post('/api/analysis-runs',json=body(),headers=HEADERS).status_code==501
    runtime._executor=lambda v:{'raw_response':'synthetic'}
    assert http.post('/api/analysis-runs',json=body(),headers={'Idempotency-Key':'x'}).status_code==403
    assert http.post('/api/analysis-runs',json=body(),headers=HEADERS).status_code==201
    assert http.post('/api/analysis-runs',json=dict(body(),batch_id='other'),headers=HEADERS).status_code==409
    assert http.get('/api/analysis-runs?limit=0').status_code==400
    assert http.get('/api/analysis-runs/missing').status_code==404


@pytest.mark.parametrize('mode',['cancel','failure','late'])
def test_http_terminal_states_not_success(setup,mode):
    evidence,policy=setup
    runtime=None
    def fake(value):
        if mode=='failure':raise RuntimeError('private synthetic detail')
        if mode=='late':runtime.clock=lambda:AT+timedelta(minutes=6)
        return {'raw_response':'synthetic'}
    runtime=prepare(policy,evidence,fake);http=client(runtime)
    task=http.post('/api/analysis-runs',json=body(),headers=HEADERS).json['data']['task_id']
    if mode=='cancel':
        assert http.post(f'/api/analysis-runs/{task}/cancel',headers=HEADERS).status_code==200
    response=http.post(f'/api/analysis-runs/{task}/execute',headers=HEADERS)
    assert response.json['data']['status']=={'cancel':'cancelled','failure':'failed','late':'expired'}[mode]
    assert 'private synthetic detail' not in response.get_data(as_text=True)


def db_counts(store):
    with closing(store.connect()) as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                     ('agent_input_records','manual_tasks','task_transitions','task_http_requests'))


@pytest.mark.parametrize('deadline',['not-a-date','2026-11-14T12:00:00','not_applicable'])
def test_bad_deadline_400_without_repository_changes(setup,deadline):
    evidence,policy=setup;runtime=prepare(policy,evidence,lambda v:{'raw_response':'synthetic'})
    http=client(runtime);before=db_counts(evidence.store)
    response=http.post('/api/analysis-runs',json=dict(body(),deadline=deadline),headers=HEADERS)
    assert response.status_code==400 and response.json['request_id']
    assert db_counts(evidence.store)==before


@pytest.mark.parametrize('exception',[ValueError('synthetic-private-marker'),RuntimeError('synthetic-private-marker')])
def test_internal_errors_safe_and_no_writes(setup,exception,monkeypatch):
    evidence,policy=setup;runtime=prepare(policy,evidence,lambda v:{'raw_response':'synthetic'})
    http=client(runtime);before=db_counts(evidence.store)
    def fail(*args,**kwargs):raise exception
    monkeypatch.setattr(runtime,'create',fail)
    response=http.post('/api/analysis-runs',json=body(),headers=HEADERS)
    assert response.status_code==500
    assert response.json['code']=='INTERNAL_ERROR' and response.json['request_id']
    assert 'synthetic-private-marker' not in response.get_data(as_text=True)
    assert db_counts(evidence.store)==before


def test_reviewer_reads_redacted_details_and_events_but_cannot_write(setup):
    evidence,policy=setup;runtime=prepare(policy,evidence,lambda v:{'raw_response':'private raw result'})
    admin=client(runtime)
    task=admin.post('/api/analysis-runs',json=body(),headers=HEADERS).json['data']['task_id']
    admin.post(f'/api/analysis-runs/{task}/execute',headers=HEADERS)
    reviewer=client(runtime,'reviewer')
    for path in ('/api/analysis-runs',f'/api/analysis-runs/{task}',f'/api/analysis-runs/{task}/events'):
        response=reviewer.get(path)
        assert response.status_code==200 and 'private raw result' not in response.get_data(as_text=True)
    assert reviewer.post(f'/api/analysis-runs/{task}/cancel',headers=HEADERS).status_code==403
    assert reviewer.post(f'/api/analysis-runs/{task}/execute',headers=HEADERS).status_code==403


def test_anonymous_401_without_creating_runtime():
    app=create_app(data_dir='unused',principal_loader=lambda req:None)
    assert app.test_client().get('/api/analysis-runs').status_code==401
