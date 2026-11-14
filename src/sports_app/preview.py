"""Explicit isolated local preview factory. Import never starts a server.

No default principal, executor, recommendation publisher or synthetic seeding.
The trusted authentication adapter must be provided by application assembly.
"""
from pathlib import Path
from .app import create_app
from .storage import SQLiteStore,safe_data_dir
from .read_api import ReadViews
from .published_settlement import PublishedSettlementService


def create_preview(*,preview_dir,principal_loader=None,pairing_origin=None,preview_actors=None):
    if pairing_origin is None and not callable(principal_loader):raise ValueError('trusted authentication adapter required')
    if pairing_origin is not None and principal_loader is not None:raise ValueError('cannot mix pairing and injected identity')
    app_root=Path(__file__).resolve().parents[2]
    preview_root=app_root/'.preview'
    target=safe_data_dir(preview_dir,app_root=preview_root)
    # Never reuse an existing database or silently import runtime data.
    if target.exists() and any(target.iterdir()):
        raise ValueError('preview directory must be new or empty')
    store=SQLiteStore(target,app_root=preview_root)
    pairing=None
    if pairing_origin is not None:
        from .preview_auth import LocalPairAuth
        pairing=LocalPairAuth(store,origin=pairing_origin,actors=preview_actors)
        from .preview_instance import bind_instance,load_instance
        from secrets import token_hex
        descriptor=bind_instance(store,descriptor_path=target/'instance.json',instance_id=token_hex(16),
                                 origin=pairing_origin,actors=preview_actors)
        pairing=load_instance(descriptor)
    app=create_app(data_dir=str(target),principal_loader=principal_loader,pairing_auth=pairing,
                   read_views=ReadViews(store),review_service=PublishedSettlementService(store))
    if pairing is not None:app.extensions['local_pair_auth']=pairing
    return app
