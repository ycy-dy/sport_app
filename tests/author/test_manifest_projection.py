from contextlib import closing
from types import SimpleNamespace
import pytest
from test_read_api import setup
from sports_app.app import create_app
from sports_app.auth import Principal
from sports_app.publication import canonical
from sports_app.public_projection import viewer_manifest


@pytest.mark.parametrize('value,valid',[('a'*64,True),('0123456789abcdef'*4,True),
 ('/synthetic/private/path',False),('text',False),('a'*63,False),('A'*64,False),
 ({'nested':'SYNTHETIC_PRIVATE'},False),(['SYNTHETIC_PRIVATE'],False),(None,False)])
def test_pure_projection_does_not_rehash_or_mutate(value,valid):
    result=viewer_manifest(value)
    assert result['manifest_hash']==(value if valid else None)
    assert result['field_coverage']['manifest_hash']==('available' if valid else 'unknown')


@pytest.mark.parametrize('value',['f'*64,'/synthetic/private/path','a'*63,{'private':'SYNTHETIC_PRIVATE'}])
def test_both_viewer_paths_hide_invalid_fingerprint_and_preserve_original(setup,value):
    views=setup
    # SQLite manifest column is text; malformed object encoded as historical text.
    stored=value if isinstance(value,str) else canonical(value)
    with closing(views.store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                   ('p','slot','s',stored,'published',canonical({'settlement_ticket':{'legs':[]}}),'time'))
    app=create_app(data_dir='unused',principal_loader=lambda req:Principal('v','viewer','v'),
                   read_views=views,publication_chain=SimpleNamespace(store=views.store))
    http=app.test_client()
    for path in ('/api/recommendations','/api/ledger','/api/reviews','/api/publications','/api/publications/p'):
        response=http.get(path)
        assert response.status_code==200
        row=response.json['data'] if path.endswith('/p') else response.json['data'][0]
        assert row['manifest_hash']==(stored if stored=='f'*64 else None)
        assert row['field_coverage']['manifest_hash']==('available' if stored=='f'*64 else 'unknown')
        if stored!='f'*64:assert stored not in response.get_data(as_text=True)
    with closing(views.store.connect()) as db:
        assert db.execute('SELECT manifest_hash FROM publications').fetchone()[0]==stored
