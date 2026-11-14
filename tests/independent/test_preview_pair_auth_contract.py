"""Synthetic pairing secrets stay in memory; no printing, real issuance or server."""
from contextlib import closing
from pathlib import Path
from threading import Barrier,Thread
import sqlite3
import pytest
from sports_app.preview_auth import LocalPairAuth,COOKIE,digest
from sports_app.storage import SQLiteStore
from sports_app.app import create_app
from sports_app.auth import Principal

ORIGIN='http://127.0.0.1:3666'


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.fixture
def auth(tmp_path):
    root=Path(__file__).resolve().parents[2];assert tmp_path.resolve().is_relative_to(root)
    clock=[10000.0]
    store=SQLiteStore(tmp_path/'synthetic-auth',app_root=root)
    result=LocalPairAuth(store,origin=ORIGIN,actors={'preview-a':'admin','preview-r':'reviewer','preview-v':'viewer'},clock=lambda:clock[0])
    return result,clock


def test_synthetic_code_digest_only_one_use_and_failed_session_insert_rolls_back(auth):
    service,_=auth;code=service.issue('preview-a');challenge=service.challenge()
    with closing(service.store.connect()) as db,db:
        assert db.execute('SELECT digest FROM preview_pair_codes').fetchone()[0]==digest(code)
        db.execute("CREATE TRIGGER independent_fail_session BEFORE INSERT ON preview_sessions BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):service.exchange(code,challenge)
    with closing(service.store.connect()) as db,db:
        assert db.execute('SELECT used FROM preview_pair_codes').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM preview_challenges').fetchone()[0]==1
        db.execute('DROP TRIGGER independent_fail_session')
    session,status=service.exchange(code,challenge);assert status==200 and bool(session)
    assert service.exchange(code,service.challenge())[1]!=200


def test_new_challenge_cannot_reset_exhausted_attempt_budget(auth):
    service,_=auth
    for _ in range(5):service.exchange('synthetic-invalid',service.challenge())
    # Strict red: renewed challenge must not restore an unlimited guessing budget.
    assert service.exchange('synthetic-invalid',service.challenge())[1]==429


def test_same_code_concurrent_exchange_consumed_once(auth):
    service,_=auth;code=service.issue('preview-v');challenges=[service.challenge(),service.challenge()]
    barrier=Barrier(2);statuses=[];errors=[]
    def worker(challenge):
        try:barrier.wait(timeout=10);statuses.append(service.exchange(code,challenge)[1])
        except BaseException:errors.append('worker-failed')
    threads=[Thread(target=worker,args=(c,),daemon=True) for c in challenges]
    for t in threads:t.start()
    for t in threads:t.join(15)
    assert not any(t.is_alive() for t in threads) and not errors
    assert statuses.count(200)==1
    with closing(service.store.connect()) as db:assert db.execute('SELECT COUNT(*) FROM preview_sessions').fetchone()[0]==1


@pytest.mark.parametrize('elapsed',[120,121])
def test_pair_code_exact_expiry(auth,elapsed):
    service,clock=auth;code=service.issue('preview-v');challenge=service.challenge();clock[0]+=elapsed
    assert service.exchange(code,challenge)[1]!=200


def request_for(session,csrf=None):
    from flask import Flask,request
    app=Flask(__name__)
    headers={'Cookie':COOKIE+'='+session}
    if csrf is not None:headers['X-CSRF']=csrf
    return app.test_request_context('/auth/logout',headers=headers)


@pytest.mark.parametrize('mode',['idle','absolute'])
def test_session_expiry_boundaries(auth,mode):
    from flask import request
    service,clock=auth;session,status=service.exchange(service.issue('preview-v'),service.challenge());assert status==200
    if mode=='absolute':
        for _ in range(3):
            clock[0]+=500
            with request_for(session):assert service.principal(request) is not None
        clock[0]=11800
    else:clock[0]+=600
    with request_for(session):assert service.principal(request) is None


def test_reauthentication_rotates_csrf_revokes_old_and_logout(auth):
    from flask import request
    service,_=auth
    old,status=service.exchange(service.issue('preview-a'),service.challenge());assert status==200
    with request_for(old):old_principal=service.principal(request)
    new,status=service.exchange(service.issue('preview-r'),service.challenge(),old);assert status==200 and new!=old
    with request_for(old):assert service.principal(request) is None
    with request_for(new):current=service.principal(request)
    assert current.role=='reviewer' and current.csrf_token!=old_principal.csrf_token
    with request_for(new,old_principal.csrf_token):assert service.logout(request)==403
    with request_for(new,current.csrf_token):assert service.logout(request)==200
    with request_for(new):assert service.principal(request) is None


@pytest.mark.parametrize('host,origin,forwarded',[
    ('evil.example:3666',ORIGIN,False),('127.0.0.1:3666','http://evil.example',False),
    ('127.0.0.1:3666',None,False),('127.0.0.1:3666','null',False),('127.0.0.1:3666',ORIGIN,True)])
def test_host_origin_forwarded_rejection(auth,host,origin,forwarded):
    from flask import Flask,request
    service,_=auth;headers={'Host':host}
    if origin is not None:headers['Origin']=origin
    if forwarded:headers['X-Forwarded-Host']='127.0.0.1:3666'
    with Flask(__name__).test_request_context('/auth/pair',method='POST',base_url=ORIGIN,headers=headers):assert service.guard(request)==403


def test_valid_guard_and_registered_roles_only(auth):
    from flask import Flask,request
    service,_=auth
    with Flask(__name__).test_request_context('/auth/pair',method='POST',base_url=ORIGIN,headers={'Origin':ORIGIN}):assert service.guard(request) is None
    with pytest.raises(ValueError):service.issue('unregistered-admin')
    for actor,role in service.actors.items():
        session,status=service.exchange(service.issue(actor),service.challenge());assert status==200
        with request_for(session):assert service.principal(request).role==role


def test_pairing_http_anonymous_surface_and_no_test_loader_mix(auth):
    service,_=auth
    with pytest.raises(ValueError):create_app(data_dir='unused',principal_loader=lambda req:Principal('test','admin','test'),pairing_auth=service)
    app=create_app(data_dir='unused',principal_loader=None,pairing_auth=service)
    client=app.test_client()
    login=client.get('/auth/login',base_url=ORIGIN)
    assert login.status_code==200 and 'no-store' in login.headers['Cache-Control']
    assert 'HttpOnly' in login.headers.get('Set-Cookie','') and 'SameSite=Strict' in login.headers.get('Set-Cookie','')
    for path in ('/','/static/app.js','/static/app.css','/api/session'):
        assert client.get(path,base_url=ORIGIN).status_code==401
    assert client.get('/auth/login?code=synthetic',base_url=ORIGIN).status_code==400
    assert client.post('/auth/pair',base_url=ORIGIN,json={'code':'synthetic','role':'admin'},headers={'Origin':ORIGIN}).status_code==400
