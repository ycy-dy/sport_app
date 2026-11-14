"""Disposable instance binding and simulated CLI; never real credential issuance."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
from io import StringIO
from types import SimpleNamespace
import pytest
from sports_app.storage import SQLiteStore
from sports_app.preview_auth import LocalPairAuth
from sports_app.preview_instance import bind_instance,load_instance
import sports_app.pair_issuer as issuer


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.fixture
def instance(tmp_path):
    root=Path(__file__).resolve().parents[2];assert tmp_path.resolve().is_relative_to(root)
    store=SQLiteStore(tmp_path/'synthetic-instance',app_root=root)
    actors={'preview-independent':'viewer'};origin='http://127.0.0.1:3666'
    LocalPairAuth(store,origin=origin,actors=actors) # schema initialization only
    path=bind_instance(store,descriptor_path=store.data_dir/'instance.json',instance_id='synthetic-instance',origin=origin,actors=actors)
    return store,path


def snapshot(directory):
    # Only this fixture's synthetic non-credential files; no external inventory.
    answer={}
    for name in ('instance.json','app.sqlite3','app.sqlite3-wal','app.sqlite3-shm','app.sqlite3-journal'):
        path=directory/name
        if path.exists():answer[name]=(path.stat().st_mode,path.read_bytes())
    return answer


def schema(store):
    with closing(sqlite3.connect(store.path)) as db:
        return list(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))


def test_existing_loader_same_binding_no_creation_or_schema_migration(instance):
    store,path=instance;before=snapshot(store.data_dir);ddl=schema(store)
    loaded=load_instance(path)
    assert loaded.origin=='http://127.0.0.1:3666' and dict(loaded.actors)=={'preview-independent':'viewer'}
    assert loaded.store.path==store.path
    assert snapshot(store.data_dir)==before and schema(store)==ddl


@pytest.mark.parametrize('mutation',['instance_id','origin','actors','permission','symlink','missing_db'])
def test_bad_descriptor_or_missing_db_rejected_without_side_effect(instance,mutation):
    store,path=instance
    if mutation in {'instance_id','origin','actors'}:
        body=json.loads(path.read_text())
        body[mutation]={'instance_id':'different','origin':'http://127.0.0.1:3667','actors':{'preview-independent':'admin'}}[mutation]
        path.write_text(json.dumps(body))
    elif mutation=='permission':os.chmod(path,0o644)
    elif mutation=='symlink':
        alternate=store.data_dir/'descriptor-link.json';alternate.symlink_to(path);path=alternate
    else:
        # Move only verified synthetic fixture DB, retain it to verify no recreation.
        target=store.data_dir/'held-synthetic.sqlite3'
        assert store.path.parent==store.data_dir and not target.exists()
        store.path.rename(target)
    before=snapshot(store.data_dir)
    with pytest.raises((ValueError,FileNotFoundError,sqlite3.Error)):load_instance(path)
    assert snapshot(store.data_dir)==before


class Terminal(StringIO):
    def isatty(self):return True
    def close(self):pass


@pytest.mark.parametrize('confirm,expected',[('YES',0),('NO',1)])
def test_cli_real_loading_with_fully_simulated_terminal(instance,monkeypatch,capsys,confirm,expected):
    store,path=instance
    from sports_app.preview_auth import LocalPairAuth
    # Disable actual code generation even though main uses the real instance loader.
    calls=[]
    monkeypatch.setattr(LocalPairAuth,'issue',lambda self,actor:(calls.append(actor) or 'SYNTHETIC_CODE_NOT_REAL'))
    import sys
    for stream in (sys.stdin,sys.stdout,sys.stderr):monkeypatch.setattr(stream,'isatty',lambda:True)
    terminal=Terminal('preview-independent\n'+confirm+'\n');written=[]
    terminal.write=lambda value:(written.append(value) or len(value))
    def simulated_open(name,*args,**kwargs):
        assert name=='/dev/tty';return terminal
    monkeypatch.setattr(issuer,'open',simulated_open,raising=False)
    before=snapshot(store.data_dir);ddl=schema(store)
    assert issuer.main(['--config',str(path)])==expected
    assert calls==(['preview-independent'] if confirm=='YES' else [])
    assert any('SYNTHETIC_CODE_NOT_REAL' in value for value in written)==(confirm=='YES')
    captured=capsys.readouterr();assert captured.out==captured.err==''
    assert snapshot(store.data_dir)==before and schema(store)==ddl


@pytest.mark.parametrize('args',[[],['--role','admin'],['--origin','http://127.0.0.1:3666'],['--database','other'],['--config','x','--role','admin']])
def test_cli_override_arguments_never_load_or_issue(monkeypatch,capsys,args):
    import sports_app.preview_instance as module
    calls=[]
    monkeypatch.setattr(module,'load_instance',lambda path:calls.append(path))
    # No actual terminal is opened on any error path.
    monkeypatch.setattr(issuer,'open',lambda *a,**k:(_ for _ in ()).throw(PermissionError()),raising=False)
    assert issuer.main(args)==2 and calls==[]
    captured=capsys.readouterr();assert captured.out==captured.err==''
