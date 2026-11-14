"""In-process real template/assets integration; synthetic authentication only."""
from html.parser import HTMLParser
import json
import re
from pathlib import Path
import pytest
from sports_app.app import create_app
from sports_app.auth import Principal


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def application():
    def loader(req):
        key=req.headers.get('X-Synthetic-Identity')
        return {'admin':Principal('actor-admin','admin','csrf-admin'),
                'reviewer':Principal('actor-reviewer','reviewer','csrf-reviewer'),
                'viewer':Principal('actor-viewer','viewer','csrf-viewer'),
                'invalid':Principal('bad','invented-role','csrf')}.get(key)
    return create_app(data_dir='unused-no-store',principal_loader=loader)


class Markup(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.scripts=[];self.context='';self.in_context=False
    def handle_starttag(self,tag,attrs):
        attr=dict(attrs)
        if tag=='a':self.links.append(attr.get('href'))
        if tag=='script':
            self.scripts.append(attr)
            self.in_context=attr.get('id')=='session-context'
    def handle_endtag(self,tag):
        if tag=='script':self.in_context=False
    def handle_data(self,data):
        if self.in_context:self.context+=data


def test_actual_template_navigation_assets_and_read_routes():
    client=application().test_client();headers={'X-Synthetic-Identity':'viewer'}
    response=client.get('/',headers=headers);assert response.status_code==200
    parsed=Markup();parsed.feed(response.text)
    expected=['schedules','analyses','recommendations','ledger','reviews','analysis-system','recommendation-system']
    assert all('#'+key in parsed.links for key in expected)
    context=json.loads(parsed.context)
    assert context==dict(actor_id='actor-viewer',role='viewer',csrf_token='csrf-viewer')
    for path in ('/static/app.js','/static/app.css'):
        assert client.get(path,headers=headers).status_code==200
    for path in ('/api/schedules','/api/analyses','/api/recommendations','/api/ledger','/api/reviews',
                 '/api/systems/analysis','/api/systems/recommendation'):
        assert client.get(path,headers=headers).status_code==200


@pytest.mark.parametrize('identity',[None,'invalid'])
@pytest.mark.parametrize('path',['/','/static/app.js','/static/app.css','/api/session','/api/schedules'])
def test_anonymous_or_invalid_identity_never_reads_page_assets_or_session(identity,path):
    headers={} if identity is None else {'X-Synthetic-Identity':identity}
    response=application().test_client().get(path,headers=headers)
    assert response.status_code==401
    assert 'csrf-admin' not in response.text and 'csrf-viewer' not in response.text


def test_request_identity_and_no_store_current_context_only():
    client=application().test_client()
    for role in ('admin','viewer','reviewer','admin'):
        headers={'X-Synthetic-Identity':role}
        response=client.get('/api/session',headers=headers)
        assert response.json['data']==dict(actor_id='actor-'+role,role=role,csrf_token='csrf-'+role)
        for path in ('/','/api/session','/api/schedules','/api/analysis-runs'):
            result=client.get(path,headers=headers)
            assert 'no-store' in result.headers.get('Cache-Control','')
            assert 'Cookie' in result.headers.get('Vary','')
    assert client.get('/api/session').status_code==401


def test_script_context_escapes_hostile_authenticated_values():
    hostile='</script><script>alert("SYNTHETIC_XSS")</script>&'
    app=create_app(data_dir='unused',principal_loader=lambda req:Principal(hostile,'admin',hostile))
    response=app.test_client().get('/');assert response.status_code==200
    assert hostile not in response.text
    parsed=Markup();parsed.feed(response.text)
    assert len(parsed.scripts)==2
    assert json.loads(parsed.context)['actor_id']==hostile
    assert '\\u003c' in parsed.context


def test_frontend_write_protocol_source_guardrails_not_browser_execution():
    # Static guards only; browser behavior is a separate acceptance gate.
    root=Path(__file__).resolve().parents[2]
    source=(root/'static/app.js').read_text()
    assert "api('/api/session')" in source
    assert "if(!await confirmAction(" in source
    assert "'X-CSRF':current.data.csrf_token" in source
    assert "'Idempotency-Key':pending.key" in source
    assert "pendingWrites.set(pendingKey,pending)" in source
    assert "pendingWrites.delete(pendingKey)" in source
    assert 'current.data.actor_id!==context.actor_id' in source
    assert "credentials:'same-origin'" in source
    assert '.innerHTML' not in source
    # First UI intentionally exposes request/comment writes only, not settlement/publication.
    assert '/corrections' not in source and '/settle' not in source
    assert '/api/review-statistics' in source
    assert '/history' in source and '/comments' in source


def test_condition_warning_never_implies_hard_gate_waiver():
    source=(Path(__file__).resolve().parents[2]/'static/app.js').read_text()
    required='证据分未达70；条件票不豁免身份、在售、来源冲突、人工审核与发布门禁；模型概率与EV未验收。'
    assert required in source
    assert '未通过70分及人工来源门槛' not in source
