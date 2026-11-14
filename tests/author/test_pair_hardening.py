from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import pytest
from test_preview_pair_auth import setup
from sports_app.private_auth_store import validate_private_store
from sports_app.pair_issuer import issue_interactive


def test_cross_challenge_cooldown_exact_endpoint_and_unrelated_valid_code(setup):
    auth,app,clock=setup
    for i in range(5):
        assert auth.exchange('synthetic-repeated-wrong',auth.challenge())[1]==(429 if i==4 else 401)
    assert auth.exchange('synthetic-repeated-wrong',auth.challenge())[1]==429
    code=auth.issue('preview-viewer')
    assert auth.exchange(code,auth.challenge())[1]==200
    clock[0]+=300
    assert auth.exchange('synthetic-repeated-wrong',auth.challenge())[1]==401


def test_existing_unsafe_permission_rejected_not_repaired(setup):
    auth,_,_=setup
    path=Path(auth.store.path)
    path.chmod(0o644)  # synthetic isolated fixture only
    with pytest.raises(ValueError):validate_private_store(auth.store)
    assert path.stat().st_mode & 0o777==0o644
    path.chmod(0o600)  # restore only this test-owned fixture


def test_issuer_refuses_non_tty_before_issue(setup,monkeypatch):
    auth,_,_=setup
    import sys
    called=[]
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(auth,'issue',lambda actor:called.append(actor))
    with pytest.raises(PermissionError):issue_interactive(auth)
    assert not called


def test_auxiliary_symlink_rejected_without_following(setup):
    auth,_,_=setup
    link=Path(str(auth.store.path)+'-wal')
    link.symlink_to('nonexistent-synthetic-target')
    with pytest.raises(ValueError):validate_private_store(auth.store)
