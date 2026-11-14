"""Synthetic pairing credentials generated only inside isolated tests; never print."""
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import pytest
from sports_app.storage import SQLiteStore
from sports_app.preview_auth import LocalPairAuth,COOKIE,CHALLENGE
from sports_app.app import create_app

ORIGIN='http://127.0.0.1:18991'


@pytest.fixture
def setup():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.pair-auth-test-',dir=root) as temp:
        clock=[1000.0]
        auth=LocalPairAuth(SQLiteStore(temp,app_root=root),origin=ORIGIN,
                           actors={'preview-admin':'admin','preview-reviewer':'reviewer','preview-viewer':'viewer'},clock=lambda:clock[0])
        app=create_app(data_dir=temp,principal_loader=None,pairing_auth=auth)
        yield auth,app,clock


def login(auth,http,actor):
    http.get('/auth/login',base_url=ORIGIN)
    code=auth.issue(actor)
    return http.post('/auth/pair',base_url=ORIGIN,json={'code':code},headers={'Origin':ORIGIN})


@pytest.mark.parametrize('actor,role',[('preview-admin','admin'),('preview-reviewer','reviewer'),('preview-viewer','viewer')])
def test_pair_roles_context_and_logout(setup,actor,role):
    auth,app,_=setup;http=app.test_client()
    assert http.get('/api/session',base_url=ORIGIN).status_code==401
    assert login(auth,http,actor).status_code==200
    response=http.get('/api/session',base_url=ORIGIN)
    assert response.json['data']['role']==role
    csrf=response.json['data']['csrf_token']
    assert http.post('/auth/logout',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF':csrf}).status_code==200
    assert http.get('/api/session',base_url=ORIGIN).status_code==401


def test_single_atomic_exchange_reauth_rotates_and_revokes(setup):
    auth,app,_=setup;http=app.test_client()
    login(auth,http,'preview-admin')
    old=http.get_cookie(COOKIE,domain='127.0.0.1').value
    csrf=http.get('/api/session',base_url=ORIGIN).json['data']['csrf_token']
    login(auth,http,'preview-viewer')
    assert http.get_cookie(COOKIE,domain='127.0.0.1').value!=old
    assert http.get('/api/session',base_url=ORIGIN).json['data']['csrf_token']!=csrf
    other=app.test_client();other.set_cookie(COOKIE,old,domain='127.0.0.1')
    assert other.get('/api/session',base_url=ORIGIN).status_code==401
    code=auth.issue('preview-admin');challenges=[auth.challenge(),auth.challenge()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda c:auth.exchange(code,c)[1],challenges))
    assert sorted(results)==[200,401]


def test_attempt_limit_expiry_idle_and_absolute(setup):
    auth,app,clock=setup
    challenge=auth.challenge()
    for _ in range(4):assert auth.exchange('synthetic-wrong',challenge)[1]==401
    assert auth.exchange('synthetic-wrong',challenge)[1]==429
    code=auth.issue('preview-admin');challenge=auth.challenge();clock[0]+=120
    assert auth.exchange(code,challenge)[1]==401
    http=app.test_client();login(auth,http,'preview-admin');clock[0]+=600
    assert http.get('/api/session',base_url=ORIGIN).status_code==401
    login(auth,http,'preview-admin')
    for _ in range(3):
        clock[0]+=500;assert http.get('/api/session',base_url=ORIGIN).status_code==200
    clock[0]+=300
    assert http.get('/api/session',base_url=ORIGIN).status_code==401


def test_host_origin_forwarded_csrf_and_no_test_loader_mix(setup):
    auth,app,_=setup;http=app.test_client()
    assert http.get('/auth/login',base_url='http://evil.invalid:18991').status_code==403
    assert http.get('/auth/login',base_url=ORIGIN,headers={'X-Forwarded-Host':'localhost'}).status_code==403
    http.get('/auth/login',base_url=ORIGIN)
    assert http.post('/auth/pair',base_url=ORIGIN,json={'code':'synthetic'},headers={'Origin':'http://evil.invalid'}).status_code==403
    login(auth,http,'preview-admin')
    assert http.post('/auth/logout',base_url=ORIGIN,headers={'Origin':ORIGIN}).status_code==403
    with pytest.raises(ValueError):create_app(data_dir='unused',principal_loader=lambda req:None,pairing_auth=auth)
