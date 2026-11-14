"""Viewer fingerprint projection cannot expose arbitrary historical values."""
from contextlib import closing
from types import SimpleNamespace
import json
import pytest
from test_read_api_contract import environment
from sports_app.publication import canonical
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.read_api import ReadViews


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.mark.parametrize('stored,expected',[
    ('a'*64,'a'*64),('/PRIVATE_HISTORY/internal/rules.yaml',None),
    ('ordinary-private-text',None),('b'*63,None),('b'*65,None),('A'*64,None),
    ({'private':'PRIVATE_NESTED_VALUE'},None)])
def test_viewer_all_public_lists_and_detail_hide_bad_fingerprint_without_history_rewrite(environment,stored,expected):
    store,views,_=environment
    # Use the factory once for this app. The existing fixture app stays untouched.
    # Only a read-only store handle is supplied; no executor or publish service.
    app=create_app(data_dir=str(store.data_dir),
                   principal_loader=lambda req:Principal('viewer','viewer','csrf'),
                   publication_chain=SimpleNamespace(store=store),read_views=views)
    client=app.test_client()
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
            ('fingerprint-publication','slot','selection',canonical(stored) if isinstance(stored,dict) else stored,
             'published',canonical({'recommendation_status':'conditional_low_confidence'}),'synthetic-time'))
        original=tuple(db.execute("SELECT * FROM publications WHERE publication_id='fingerprint-publication'").fetchone())
    for route in ('/api/recommendations','/api/ledger','/api/reviews','/api/publications',
                  '/api/publications/fingerprint-publication'):
        response=client.get(route,headers={'X-Role':'viewer'})
        assert response.status_code==200,(route,response.text)
        data=response.json['data'];row=data[0] if isinstance(data,list) else data
        assert row['manifest_hash']==expected
        assert row['field_coverage']['manifest_hash']==('available' if expected else 'unknown')
        if expected is None:
            assert 'PRIVATE_' not in response.text and 'ordinary-private-text' not in response.text
            assert str(stored) not in response.text
        with closing(store.connect()) as db:
            assert tuple(db.execute("SELECT * FROM publications WHERE publication_id='fingerprint-publication'").fetchone())==original
