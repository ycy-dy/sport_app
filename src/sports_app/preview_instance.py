"""Shared private instance descriptor; existing-only issuer/server attachment."""
from contextlib import closing
from pathlib import Path
import os
import json
import sqlite3
import stat
from .storage import reject_link_chain
from .private_auth_store import validate_private_store
from .publication import canonical,fingerprint

SCHEMA='''
CREATE TABLE IF NOT EXISTS preview_instance_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS preview_binding_no_update BEFORE UPDATE ON preview_instance_binding BEGIN SELECT RAISE(ABORT,'immutable instance'); END;
CREATE TRIGGER IF NOT EXISTS preview_binding_no_delete BEFORE DELETE ON preview_instance_binding BEGIN SELECT RAISE(ABORT,'immutable instance'); END;
'''


def validate_descriptor(body):
    from urllib.parse import urlsplit
    if not isinstance(body,dict) or set(body)!={'instance_id','origin','database','actors'}:raise ValueError('instance fields')
    if not isinstance(body['instance_id'],str) or not body['instance_id'].strip() or body['database']!='app.sqlite3':raise ValueError('instance identity')
    parsed=urlsplit(body['origin'])
    if (parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','localhost'}
            or parsed.port is None or parsed.path or parsed.query or parsed.fragment or parsed.username):raise ValueError('instance origin')
    if not isinstance(body['actors'],dict) or not body['actors']:raise ValueError('instance actors')
    from .auth import ROLES
    if any(not isinstance(k,str) or not k.startswith('preview-') or v not in ROLES for k,v in body['actors'].items()):raise ValueError('instance actor role')
    return body


def bind_instance(store,*,descriptor_path,instance_id,origin,actors):
    """Initialization only; explicit confirmed config, never issues a credential."""
    validate_private_store(store)
    path=reject_link_chain(descriptor_path)
    if path.parent!=Path(store.data_dir).absolute() or path.exists():raise ValueError('new private descriptor required')
    body=validate_descriptor({'instance_id':instance_id,'origin':origin,'database':'app.sqlite3','actors':dict(actors)})
    with closing(store.connect()) as db,db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO preview_instance_binding VALUES(1,?)',(fingerprint(body),))
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:handle.write(canonical(body))
    return path


class ExistingInstanceStore:
    def __init__(self,directory):
        self.data_dir=reject_link_chain(directory);self.path=self.data_dir/'app.sqlite3'
        validate_private_store(self)
        if not self.path.is_file():raise ValueError('existing database required')
    def connect(self):
        validate_private_store(self)
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True)
        db.row_factory=sqlite3.Row
        return db


def load_instance(descriptor_path):
    """No mkdir, tables, migrations, role/origin/database overrides."""
    path=reject_link_chain(descriptor_path)
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):raise ValueError('private descriptor required')
    store=ExistingInstanceStore(path.parent)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,encoding='utf-8') as handle:body=validate_descriptor(json.load(handle))
    with closing(store.connect()) as db:
        row=db.execute('SELECT digest FROM preview_instance_binding WHERE singleton=1').fetchone()
        required={'preview_pair_codes','preview_challenges','preview_sessions','preview_submission_failures'}
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if row is None or row['digest']!=fingerprint(body) or not required<=names:raise ValueError('instance/database binding mismatch')
    from .preview_auth import LocalPairAuth
    return LocalPairAuth(store,origin=body['origin'],actors=body['actors'],initialize=False)


def attach_instance(descriptor_path):
    """Server factory with same bound config; no executor or publisher."""
    auth=load_instance(descriptor_path)
    from .app import create_app
    from .read_api import ReadViews
    from .published_settlement import PublishedSettlementService
    # These service constructors may create their own non-auth tables; issuer never does.
    return create_app(data_dir=str(auth.store.data_dir),principal_loader=None,pairing_auth=auth,
                      read_views=ReadViews(auth.store),review_service=PublishedSettlementService(auth.store))
