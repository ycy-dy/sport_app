"""Synthetic cooldown, disposable permissions and fully simulated TTY only."""
from pathlib import Path
from types import SimpleNamespace
from io import StringIO
import os
import pytest
from test_preview_pair_auth_contract import auth
from sports_app.private_auth_store import validate_private_store,PrivateAuthStore
from sports_app.storage import SQLiteStore
import sports_app.pair_issuer as issuer


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('External IO forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.mark.parametrize('elapsed,expected',[(299,429),(300,401),(301,401)])
def test_same_submission_cooldown_endpoint(auth,elapsed,expected):
    service,clock=auth
    for _ in range(5):service.exchange('independent-synthetic-invalid',service.challenge())
    clock[0]+=elapsed
    assert service.exchange('independent-synthetic-invalid',service.challenge())[1]==expected


def test_unrelated_bad_submissions_never_lock_valid_actor(auth):
    service,_=auth
    for i in range(8):service.exchange('independent-bad-'+str(i),service.challenge())
    session,status=service.exchange(service.issue('preview-v'),service.challenge())
    assert status==200 and bool(session)


@pytest.mark.parametrize('target',['directory','database','wal','shm','journal'])
def test_wrong_permissions_rejected_not_repaired(auth,target):
    service,_=auth;store=service.store
    path=Path(store.data_dir) if target=='directory' else Path(str(store.path)+{'database':'','wal':'-wal','shm':'-shm','journal':'-journal'}[target])
    if not path.exists():path.write_bytes(b'')
    os.chmod(path,0o755 if target=='directory' else 0o644)
    before=path.stat().st_mode
    with pytest.raises(ValueError):validate_private_store(store)
    assert path.stat().st_mode==before


@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_database_auxiliary_links_rejected(auth,kind):
    service,_=auth;store=service.store
    target=Path(store.data_dir)/'synthetic-material';target.write_bytes(b'synthetic');os.chmod(target,0o600)
    link=Path(str(store.path)+'-journal')
    if kind=='symlink':link.symlink_to(target)
    else:os.link(target,link)
    with pytest.raises(ValueError):validate_private_store(store)
    assert target.read_bytes()==b'synthetic'


def test_original_symlink_directory_not_lost_by_generic_factory(auth):
    service,_=auth;root=Path(__file__).resolve().parents[2]
    link=Path(service.store.data_dir).parent/'synthetic-link'
    link.symlink_to(service.store.data_dir,target_is_directory=True)
    # Must reject before resolve() erases provenance, not merely validate resolved target.
    with pytest.raises(ValueError):
        PrivateAuthStore(SQLiteStore(link,app_root=root))
    assert link.is_symlink()


class Terminal(StringIO):
    def isatty(self):return True
    def close(self):pass


@pytest.mark.parametrize('mode',['success','cancel','unknown','non_tty'])
def test_issuer_only_simulated_terminal_no_standard_output(monkeypatch,capsys,mode):
    import sys
    calls=[]
    fake=SimpleNamespace(store=object(),actors={'preview-synthetic':'viewer'},
                         issue=lambda actor:(calls.append(actor) or 'SYNTHETIC_NOT_A_REAL_CODE'))
    terminal=Terminal('unknown\nYES\n' if mode=='unknown' else 'preview-synthetic\n'+('NO\n' if mode=='cancel' else 'YES\n'))
    # Separate read and write positions, like a terminal rather than StringIO's shared cursor.
    written=[];terminal.write=lambda text:(written.append(text) or len(text))
    monkeypatch.setattr(issuer,'validate_private_store',lambda store:None)
    for stream in (sys.stdin,sys.stdout,sys.stderr):monkeypatch.setattr(stream,'isatty',lambda:mode!='non_tty')
    def open_simulated(path,*args,**kwargs):
        assert path=='/dev/tty';return terminal
    monkeypatch.setattr(issuer,'open',open_simulated,raising=False)
    if mode=='unknown':
        with pytest.raises(ValueError):issuer.issue_interactive(fake)
    elif mode=='non_tty':
        with pytest.raises(PermissionError):issuer.issue_interactive(fake)
    else:assert issuer.issue_interactive(fake) is (mode=='success')
    assert calls==(['preview-synthetic'] if mode=='success' else [])
    assert any('SYNTHETIC_NOT_A_REAL_CODE' in text for text in written) is (mode=='success')
    captured=capsys.readouterr();assert captured.out==captured.err==''
