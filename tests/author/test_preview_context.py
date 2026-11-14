"""Synthetic request auth only; no production login, socket or real data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from flask import g
from jinja2 import DictLoader
import pytest
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.preview import create_preview


@pytest.mark.parametrize('role',['admin','reviewer','viewer'])
def test_same_origin_context_page_and_api_are_request_scoped_no_store(role):
    principal=Principal('synthetic-user',role,'synthetic-csrf')
    app=create_app(data_dir='unused',principal_loader=lambda req:principal)
    # Author owns no templates/static: in-memory test template only.
    app.jinja_loader=DictLoader({'index.html':'{{ session_context.role }} {{ session_context.csrf_token }}'})
    http=app.test_client()
    page=http.get('/')
    assert page.status_code==200 and role in page.get_data(as_text=True)
    result=http.get('/api/session')
    assert result.json['data']=={'actor_id':'synthetic-user','role':role,'csrf_token':'synthetic-csrf'}
    assert page.headers['Cache-Control']==result.headers['Cache-Control']=='no-store'
    assert 'Cookie' in result.headers['Vary']


@pytest.mark.parametrize('principal',[None,{},Principal('a','admin',''),Principal('a','invented','csrf'),
    Principal(' ','viewer','csrf'),Principal('a','admin',' ')])
def test_no_default_identity_or_header_bypass(principal):
    app=create_app(data_dir='unused',principal_loader=lambda req:principal)
    http=app.test_client()
    for path in ('/','/static/app.js','/api/session','/api/schedules','/api/publications','/api/review-statistics'):
        response=http.get(path,headers={'X-Role':'admin','X-Actor':'root'})
        assert response.status_code==401
        assert set(response.json)=={'category','code','message'}
        assert 'csrf_token' not in response.get_data(as_text=True)


def test_valid_viewer_forbidden_write_remains_403():
    app=create_app(data_dir='unused',principal_loader=lambda req:Principal('viewer','viewer','csrf'))
    response=app.test_client().post('/api/analysis-runs',json={},headers={'X-CSRF':'csrf'})
    assert response.status_code==403


def test_preview_rejects_existing_data_external_directory_and_missing_auth():
    root=Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError):create_preview(preview_dir=root/'.preview/new',principal_loader=None)
    with pytest.raises(ValueError):create_preview(preview_dir=root/'not-preview',principal_loader=lambda req:None)
    preview_root=root/'.preview';preview_root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='author-',dir=preview_root) as temp:
        path=Path(temp)
        app=create_preview(preview_dir=path,principal_loader=lambda req:None)
        assert app.test_client().get('/api/session').status_code==401
        assert (path/'app.sqlite3').exists()
        with pytest.raises(ValueError):create_preview(preview_dir=path,principal_loader=lambda req:None)
