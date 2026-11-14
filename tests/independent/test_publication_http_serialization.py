"""Event-controlled HTTP transaction overlap; same-file SQLite only."""
from contextlib import closing
from threading import Event, Thread, current_thread
import pytest
from test_local_assembly_contract import assembly_case
from test_publication_http_contract import environment, headers, source, state


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.mark.parametrize('operation',['draft','audit'])
def test_second_request_attempts_begin_while_first_write_uncommitted(environment,monkeypatch,operation):
    repo,_,_,_,app=environment
    draft=None
    if operation=='audit':
        response=app.test_client().post('/api/publication-drafts',json=source(),headers=headers())
        assert response.status_code==201
        draft=response.json['data']['draft_id']
    first_paused=Event();second_begin=Event();release=Event();second_acquired=Event()
    target='INSERT INTO '+('publication_drafts' if operation=='draft' else 'publication_reviews')
    original_connect=repo.store.connect

    class Connection:
        def __init__(self,db):self.db=db
        def __enter__(self):self.db.__enter__();return self
        def __exit__(self,*args):return self.db.__exit__(*args)
        def __getattr__(self,name):return getattr(self.db,name)
        def execute(self,sql,*args,**kwargs):
            name=current_thread().name
            if sql.strip().upper()=='BEGIN IMMEDIATE' and name=='independent-second':
                assert first_paused.is_set(),'Second started before first pause'
                second_begin.set()
                result=self.db.execute(sql,*args,**kwargs)
                second_acquired.set()
                return result
            result=self.db.execute(sql,*args,**kwargs)
            if sql.startswith(target) and name=='independent-first':
                assert self.db.in_transaction,'Pause must hold an actual write transaction'
                first_paused.set()
                assert release.wait(10),'Controller failed to release transaction'
            return result

    monkeypatch.setattr(repo.store,'connect',lambda:Connection(original_connect()))
    results=[];errors=[]
    def request():
        try:
            with app.test_client() as client:
                response=(client.post('/api/publication-drafts',json=source(),headers=headers()) if operation=='draft' else
                    client.post(f'/api/publication-drafts/{draft}/audit',json={},headers=headers('reviewer')))
                results.append((response.status_code,response.json))
        except BaseException as exc:errors.append(repr(exc))
    first=Thread(target=request,name='independent-first',daemon=True)
    second=Thread(target=request,name='independent-second',daemon=True)
    first.start()
    try:
        assert first_paused.wait(10),'First never reached transaction write'
        # Independent reader cannot observe the uncommitted row or freeze.
        with closing(original_connect()) as db:
            assert db.execute('SELECT COUNT(*) FROM publication_reviews').fetchone()[0]==0
            if operation=='draft':
                assert db.execute('SELECT COUNT(*) FROM publication_drafts').fetchone()[0]==0
                assert db.execute("SELECT COUNT(*) FROM evidence_records WHERE kind='recommendation'").fetchone()[0]==0
        second.start()
        assert second_begin.wait(10),'Second never attempted internal BEGIN'
        assert not second_acquired.is_set(),'Second acquired write transaction before first commit'
    finally:
        release.set()
        first.join(15)
        if second.ident is not None:second.join(15)
    assert not first.is_alive() and not second.is_alive() and errors==[]
    assert second_acquired.is_set()
    assert len(results)==2 and all(code in {200,201} for code,_ in results),results
    field='draft_id' if operation=='draft' else 'review_id'
    assert len({body['data'][field] for _,body in results})==1
    with closing(original_connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publication_drafts').fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM evidence_records WHERE kind='recommendation'").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM publication_reviews').fetchone()[0]==(operation=='audit')
    # Retain explicit same-key/different-body conflict after successful overlap.
    before=state(repo)
    with app.test_client() as client:
        if operation=='draft':
            response=client.post('/api/publication-drafts',json=dict(source(),task_id='different-task'),headers=headers())
        else:
            new=client.post('/api/publication-drafts',json=source(),headers=headers(key='other-draft'))
            assert new.status_code==201
            before=state(repo)
            response=client.post('/api/publication-drafts/'+new.json['data']['draft_id']+'/audit',json={},headers=headers('reviewer'))
    assert response.status_code==409 and state(repo)==before
