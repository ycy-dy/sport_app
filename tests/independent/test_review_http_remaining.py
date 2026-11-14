"""Remaining HTTP projection and deterministic correction overlap checks."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from threading import Event,Thread,current_thread
import json
import pytest
from test_local_assembly_contract import assembly_case
from test_review_http_contract import case,h
from sports_app.publication import canonical,fingerprint
from sports_app.settlement import Identity,ResultSnapshot


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.mark.parametrize('mode',['valid_two_ot','invalid_second_ot','bad_sum','reverse'])
def test_basketball_historical_ticket_http_periods(case,mode):
    repo,_,app,_,_,_=case
    # Explicit historical synthetic basketball original, not proof of basketball publication eligibility.
    legs=[];results={}
    for i in (1,2):
        identity=Identity('basketball','synthetic','2026-11-14',f'b{i}','2026-11-15T01:00:00+08:00',f'h{i}',f'a{i}')
        legs.append(dict(leg_id=f'b{i}',identity=asdict(identity),scope='including_ot',
                         options=[dict(option_id=f'bo{i}',market='MNL',selection='H',line=None,original_odds='4')]))
        periods=((20,20),)*4+((8,8),(10,9))
        result=ResultSnapshot(identity,'finished','H:A',(98,97),'including_ot',periods,source_id='PRIVATE_SOURCE')
        results[f'b{i}']=asdict(result)
    if mode=='invalid_second_ot':results['b1']['periods']=((20,20),)*4+((9,8),(9,9))
    elif mode=='bad_sum':results['b1']['scores']=(99,97)
    elif mode=='reverse':
        for r in results.values():
            r['orientation']='A:H';r['scores']=r['scores'][::-1];r['periods']=[p[::-1] for p in r['periods']]
    original={'settlement_ticket':dict(legs=legs,equal_stake='1',total_investment='1',
        branches=[dict(branch_id='b',option_ids=['bo1','bo2'])])}
    with closing(repo.store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',('basketball','basketball-slot','s','m','published',canonical(original),'synthetic'))
    client=app.test_client();response=client.post('/api/reviews/basketball/settle',json={'results':results},headers=h())
    assert response.status_code==200,response.text
    assert response.json['data']['outcome']==('hit' if mode in {'valid_two_ot','reverse'} else 'pending')
    detail=client.get('/api/reviews/basketball',headers=h('viewer'))
    assert detail.status_code==200 and 'PRIVATE_SOURCE' not in detail.text
    match=detail.json['data']['matches'][0]
    assert len(match['quarters'])==4 and len(match['overtime'])==2
    if mode=='reverse':assert match['home_score']==98 and match['away_score']==97


@pytest.mark.parametrize('target',['legs','branches','options'])
def test_malformed_summary_hidden_in_history_and_settle_replay(case,target):
    repo,_,app,p,results,original=case
    summary={'outcome':'pending','legs':[],'branches':[],'options':{}}
    if target=='options':summary[target]={'o':{'reason':{'secret':'PRIVATE_NESTED'}}}
    else:summary[target]=[{'outcome':{'secret':'PRIVATE_NESTED'}}]
    with closing(repo.store.connect()) as db,db:
        db.execute('INSERT INTO published_settlement_versions VALUES(?,?,?,?,?,?,?,?)',
            (p,1,fingerprint(json.loads(original)),canonical(results),canonical(summary),'legacy','admin','synthetic'))
    client=app.test_client()
    for route in (f'/api/reviews/{p}/history',f'/api/reviews/{p}'):
        response=client.get(route,headers=h('viewer'))
        assert 'PRIVATE_NESTED' not in response.text
    response=client.post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
    assert 'PRIVATE_NESTED' not in response.text
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1


def test_correction_overlap_inside_uncommitted_insert(case,monkeypatch):
    repo,_,app,p,results,_=case
    assert app.test_client().post(f'/api/reviews/{p}/settle',json={'results':results},headers=h()).status_code==200
    changed=deepcopy(results);next(iter(changed.values()))['scores']=[0,2]
    paused,attempt,release,acquired=Event(),Event(),Event(),Event()
    connect=repo.store.connect
    class Proxy:
        def __init__(self,db):self.db=db
        def __getattr__(self,name):return getattr(self.db,name)
        def __enter__(self):self.db.__enter__();return self
        def __exit__(self,*args):return self.db.__exit__(*args)
        def execute(self,sql,*args,**kwargs):
            if sql.strip().upper()=='BEGIN IMMEDIATE' and current_thread().name=='correction-second':
                attempt.set();result=self.db.execute(sql,*args,**kwargs);acquired.set();return result
            result=self.db.execute(sql,*args,**kwargs)
            if sql.startswith('INSERT INTO published_settlement_versions') and current_thread().name=='correction-first':
                assert self.db.in_transaction;paused.set();assert release.wait(10)
            return result
    monkeypatch.setattr(repo.store,'connect',lambda:Proxy(connect()))
    responses=[];errors=[]
    def worker():
        try:
            with app.test_client() as client:
                r=client.post(f'/api/reviews/{p}/corrections',json={'results':changed,'correction_reason':'same'},headers=h())
                responses.append((r.status_code,r.json))
        except BaseException as exc:errors.append(repr(exc))
    first=Thread(target=worker,name='correction-first',daemon=True);second=Thread(target=worker,name='correction-second',daemon=True)
    first.start()
    try:
        assert paused.wait(10)
        with closing(connect()) as db:assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1
        second.start();assert attempt.wait(10);assert not acquired.is_set()
    finally:
        release.set();first.join(15)
        if second.ident is not None:second.join(15)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert acquired.is_set() and len(responses)==2
    assert all(code==200 and body['data']['version']==2 for code,body in responses)
    with closing(connect()) as db:assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==2


def test_management_comments_paginated_redacted_and_viewer_denied(case):
    _,_,app,p,_,_=case;client=app.test_client()
    for index in range(3):
        response=client.post(f'/api/reviews/{p}/comments',json={'comment':f'PRIVATE_COMMENT_{index}'},headers=h('reviewer',str(index)))
        assert response.status_code==201
    for role in ('admin','reviewer'):
        response=client.get(f'/api/reviews/{p}/comments?limit=1&offset=1',headers=h(role))
        assert response.status_code==200 and 'PRIVATE_COMMENT_' not in response.text
        assert response.json['meta']['total']==3 and len(response.json['data'])==1
        row=response.json['data'][0]
        assert row['content'] is None and row['content_visibility']=='redacted'
    assert client.get(f'/api/reviews/{p}/comments',headers=h('viewer')).status_code==403


@pytest.mark.parametrize('failed_first',[False,True])
def test_pending_reason_ownership_does_not_override_miss(case,failed_first):
    _,_,app,p,results,_=case;client=app.test_client()
    values={}
    if failed_first:
        key=next(iter(results));values={key:dict(results[key],scores=[0,2])}
    response=client.post(f'/api/reviews/{p}/settle',json={'results':values},headers=h())
    assert response.status_code==200
    data=response.json['data'];pending=data['pending_reasons']
    assert data['outcome']==('miss' if failed_first else 'pending')
    assert {'option','leg'} <= {r['level'] for r in pending}
    if not failed_first:assert 'branch' in {r['level'] for r in pending}
    owners={'option':{k for k,v in data['options'].items() if v['outcome']=='pending'},
            'leg':{v['leg_id'] for v in data['legs'] if v['outcome']=='pending'},
            'branch':{v['branch_id'] for v in data['branches'] if v['outcome']=='pending'}}
    assert all(r['id'] in owners[r['level']] and isinstance(r['code'],str) for r in pending)
    if failed_first:assert not owners['branch']


def test_committed_settlement_projection_failure_retry_does_not_duplicate(case,monkeypatch):
    repo,_,app,p,results,_=case
    import sports_app.review_projection as projection
    original=projection.safe_summary
    def fail(*args,**kwargs):raise ValueError('PRIVATE_PROJECTION_FAILURE')
    with monkeypatch.context() as patch:
        patch.setattr(projection,'safe_summary',fail)
        response=app.test_client().post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
        assert response.status_code>=400 and 'PRIVATE_PROJECTION_FAILURE' not in response.text
    # Service commits before response projection: report committed state, NOT rollback.
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1
    retry=app.test_client().post(f'/api/reviews/{p}/settle',json={'results':results},headers=h())
    assert retry.status_code==200 and retry.json['data']['version']==1
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]==1
