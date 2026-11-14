from pathlib import Path
from tempfile import TemporaryDirectory
import json
import pytest
from test_preview_pair_auth import setup,ORIGIN
from sports_app.preview_instance import bind_instance,load_instance
from sports_app.storage import SQLiteStore


def test_shared_descriptor_existing_store_no_code_issued(setup):
    auth,_,_=setup
    path=Path(auth.store.data_dir)/'instance.json'
    bind_instance(auth.store,descriptor_path=path,instance_id='synthetic-instance',origin=ORIGIN,actors=dict(auth.actors))
    loaded=load_instance(path)
    assert loaded.origin==auth.origin and dict(loaded.actors)==dict(auth.actors)
    assert Path(loaded.store.path)==Path(auth.store.path)


def test_descriptor_tamper_and_missing_database_no_creation(setup):
    auth,_,_=setup
    path=Path(auth.store.data_dir)/'instance.json'
    bind_instance(auth.store,descriptor_path=path,instance_id='synthetic',origin=ORIGIN,actors=dict(auth.actors))
    body=json.loads(path.read_text());body['origin']='http://127.0.0.1:18992'
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):load_instance(path)
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.missing-instance-',dir=root) as temp:
        missing=Path(temp)/'instance.json';missing.write_text(json.dumps(body));missing.chmod(0o600)
        with pytest.raises(ValueError):load_instance(missing)
        assert not (Path(temp)/'app.sqlite3').exists()


def test_raw_symlink_entry_rejected_before_database_write():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.raw-path-',dir=root) as temp:
        parent=Path(temp);target=parent/'target';target.mkdir(mode=0o700)
        link=parent/'link';link.symlink_to(target,target_is_directory=True)
        with pytest.raises(ValueError):SQLiteStore(link,app_root=parent)
        assert not (target/'app.sqlite3').exists()
