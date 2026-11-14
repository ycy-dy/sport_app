"""Independent task HTTP acceptance: synthetic runtime, per-request principals."""
from contextlib import closing
from datetime import timedelta
from threading import Barrier, Thread
import pytest
from test_pair_policy_contract import setup, AT
from test_manual_runtime_contract import prepare
from sports_app.app import create_app
from sports_app.auth import Principal


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def application(runtime):
    def loader(req):
        role=req.headers.get('X-Test-Role','admin')
        if role=='anonymous':return None
        return Principal(req.headers.get('X-Test-Actor','http-'+role),role,'csrf')
    app=create_app(data_dir=str(runtime.store.data_dir),principal_loader=loader,task_runtime=runtime)
    app.config['TESTING']=True
    return app


def headers(role='admin',actor=None,key='key'):
    result={'X-Test-Role':role,'X-CSRF':'csrf','Idempotency-Key':key}
    if actor:result['X-Test-Actor']=actor
    return result


def body():return dict(registry_id='rules',batch_id='batch',deadline=(AT+timedelta(minutes=5)).isoformat())


def snapshot(repo):
    tables=('manual_tasks','agent_input_records','task_transitions','task_http_requests')
    with closing(repo.store.connect()) as db:
        return {table:sorted(tuple(r) for r in db.execute('SELECT * FROM '+table)) for table in tables}


def test_create_read_execute_db_binding_and_no_raw_event_leak(setup):
    calls=[]
    repo,_,runtime,_=prepare(setup,lambda value:(calls.append(value) or {'raw_response':'PRIVATE_SYNTHETIC_RESPONSE'}))
    client=application(runtime).test_client()
    response=client.post('/api/analysis-runs',json=body(),headers=headers())
    assert response.status_code==201 and response.json['data']['status']=='created'
    task=response.json['data']['task_id']
    assert client.get('/api/analysis-runs').json['data'][0]['task_id']==task
    assert client.post(f'/api/analysis-runs/{task}/execute',headers=headers()).json['data']['status']=='completed'
    client.post(f'/api/analysis-runs/{task}/execute',headers=headers())
    assert len(calls)==1
    events=client.get(f'/api/analysis-runs/{task}/events')
    assert 'PRIVATE_SYNTHETIC_RESPONSE' not in events.text
    data=events.json['data']
    assert data['stage']=='executor_return_recorded_not_decision_validated'
    assert data['model_probability'] is None and data['EV'] is None
    with closing(repo.store.connect()) as db:
        states=[r[0] for r in db.execute('SELECT state FROM task_transitions WHERE task_id=? ORDER BY version',(task,))]
        row=db.execute('SELECT input_id FROM manual_tasks WHERE task_id=?',(task,)).fetchone()
        assert row[0]==data['input_id']
    assert states==[e['status'] for e in data['events']]==['created','running','completed']


@pytest.mark.parametrize('path',['','/detail','/events'])
def test_reviewer_reads_sanitized_tasks_but_never_writes(setup,path):
    _,_,runtime,_=prepare(setup,lambda value:{'raw_response':'PRIVATE_SYNTHETIC'})
    client=application(runtime).test_client()
    task=client.post('/api/analysis-runs',json=body(),headers=headers()).json['data']['task_id']
    target='/api/analysis-runs' if not path else f'/api/analysis-runs/{task}'+('/events' if path=='/events' else '')
    response=client.get(target,headers=headers('reviewer'))
    assert response.status_code==200
    assert 'PRIVATE_SYNTHETIC' not in response.text
    assert client.post('/api/analysis-runs',json=body(),headers=headers('reviewer')).status_code==403
    for action in ('execute','cancel'):
        assert client.post(f'/api/analysis-runs/{task}/{action}',headers=headers('reviewer')).status_code==403


@pytest.mark.parametrize('role,expected',[('anonymous',401),('viewer',403)])
def test_task_management_reads_and_writes_reject_unauthorized(setup,role,expected):
    repo,_,runtime,_=prepare(setup,lambda value:{'raw_response':'synthetic'})
    client=application(runtime).test_client();before=snapshot(repo)
    for path in ('/api/analysis-runs','/api/analysis-runs/missing','/api/analysis-runs/missing/events'):
        assert client.get(path,headers=headers(role)).status_code==expected
    assert client.post('/api/analysis-runs',json=body(),headers=headers(role)).status_code==expected
    assert snapshot(repo)==before


def test_csrf_and_idempotency_actor_scope_and_cancel_endpoint(setup):
    repo,_,runtime,_=prepare(setup,lambda value:{'raw_response':'synthetic'})
    client=application(runtime).test_client();before=snapshot(repo)
    assert client.post('/api/analysis-runs',json=body(),headers={'Idempotency-Key':'key'}).status_code==403
    assert snapshot(repo)==before
    first=client.post('/api/analysis-runs',json=body(),headers=headers(actor='a'))
    task=first.json['data']['task_id'];saved=snapshot(repo)
    assert client.post('/api/analysis-runs',json=body(),headers=headers(actor='a')).json['data']['task_id']==task
    assert snapshot(repo)==saved
    assert client.post('/api/analysis-runs',json=dict(body(),deadline=(AT+timedelta(minutes=4)).isoformat()),headers=headers(actor='a')).status_code==409
    other=client.post('/api/analysis-runs',json=body(),headers=headers(actor='b'))
    assert other.status_code==201 and other.json['data']['task_id']!=task
    assert client.post(f'/api/analysis-runs/{task}/cancel',headers=headers(actor='a')).json['data']['status']=='cancelled'
    assert client.post(f'/api/analysis-runs/{task}/execute',headers=headers(actor='a')).json['data']['status']=='cancelled'


def test_concurrent_same_key_creates_one_atomic_task(setup):
    repo,_,runtime,_=prepare(setup,lambda value:{'raw_response':'synthetic'})
    app=application(runtime);barrier=Barrier(2);responses=[];errors=[]
    def submit():
        try:
            with app.test_client() as client:
                barrier.wait(timeout=10)
                response=client.post('/api/analysis-runs',json=body(),headers=headers())
                responses.append((response.status_code,response.json['data']['task_id']))
        except BaseException as exc:errors.append(repr(exc))
    threads=[Thread(target=submit,daemon=True) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(15)
    assert not any(t.is_alive() for t in threads) and errors==[]
    assert sorted(r[0] for r in responses)==[200,201]
    assert len({r[1] for r in responses})==1
    assert all(len(rows)==1 for rows in snapshot(repo).values())


@pytest.mark.parametrize('mode',['failure','deadline','cancel_late'])
def test_http_failed_cancelled_or_expired_never_success(setup,mode):
    holder={}
    def fake(value):
        runtime=holder['runtime']
        if mode=='failure':raise RuntimeError('PRIVATE_EXCEPTION_CONTENT')
        if mode=='deadline':runtime.clock=lambda:AT+timedelta(minutes=5)
        if mode=='cancel_late':runtime.cancel(Principal('http-admin','admin','csrf'),'csrf',holder['task'])
        return {'raw_response':'PRIVATE_LATE_RESPONSE'}
    repo,_,runtime,_=prepare(setup,fake);holder['runtime']=runtime
    client=application(runtime).test_client()
    holder['task']=client.post('/api/analysis-runs',json=body(),headers=headers()).json['data']['task_id']
    response=client.post('/api/analysis-runs/'+holder['task']+'/execute',headers=headers())
    assert response.json['data']['status']=={'failure':'failed','deadline':'expired','cancel_late':'cancelled'}[mode]
    assert 'PRIVATE_' not in response.text
    assert runtime.load_execution(holder['task']) is None
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT state FROM task_transitions ORDER BY version DESC LIMIT 1').fetchone()[0]==response.json['data']['status']


def test_default_unconfigured_executor_has_no_creation_side_effect(setup):
    repo,_,runtime,_=prepare(setup,None);client=application(runtime).test_client();before=snapshot(repo)
    assert client.post('/api/analysis-runs',json=body(),headers=headers()).status_code==501
    assert snapshot(repo)==before


@pytest.mark.parametrize('deadline',['not-a-timestamp','2026-11-14T12:00:00'])
def test_bad_deadline_is_400_without_internal_exception_disclosure(setup,deadline):
    repo,_,runtime,_=prepare(setup,lambda value:{'raw_response':'synthetic'})
    client=application(runtime).test_client();before=snapshot(repo)
    response=client.post('/api/analysis-runs',json=dict(body(),deadline=deadline),headers=headers())
    assert response.status_code==400
    assert 'Invalid isoformat string' not in response.text and 'timezone required' not in response.text
    assert snapshot(repo)==before


def test_internal_value_error_is_not_echoed_to_client(setup,monkeypatch):
    repo,_,runtime,_=prepare(setup,lambda value:{'raw_response':'synthetic'})
    client=application(runtime).test_client();before=snapshot(repo)
    def fail(*args,**kwargs):raise ValueError('PRIVATE_INTERNAL_PATH_AND_TOKEN')
    monkeypatch.setattr(runtime,'create',fail)
    response=client.post('/api/analysis-runs',json=body(),headers=headers())
    assert 'PRIVATE_INTERNAL_PATH_AND_TOKEN' not in response.text
    assert response.status_code>=400 and snapshot(repo)==before
