from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from sports_app.storage import SQLiteStore
from sports_app.auth import Principal
from sports_app.publication import PublicationService
from sports_app.published_settlement import PublishedSettlementService
from sports_app.settlement import Identity, ResultSnapshot

ADMIN=Principal('synthetic-admin','admin','csrf')
REVIEWER=Principal('synthetic-reviewer','reviewer','rcsrf')


def original():
    legs=[]
    for i in (1,2):
        ident=Identity('football','league','2026-11-14',f'm{i}',
                       '2026-11-14T18:00:00+08:00',f'h{i}',f'a{i}')
        selections=['H','D'] if i == 1 else ['H']
        legs.append({'leg_id':f'l{i}','identity':asdict(ident),'scope':'regulation_90',
                     'options':[{'option_id':f'o{i}{s}','market':'HAD','selection':s,
                                 'line':None,'original_odds':'4'} for s in selections]})
    return {'slot_key':'slot','selection_hash':'synthetic-selection','manifest_hash':'synthetic-manifest',
            'recommendation_status':'conditional_low_confidence',
            'settlement_ticket':{'legs':legs,'equal_stake':'1','total_investment':'2',
                                  'branches':[{'branch_id':f'b{s}','option_ids':[f'o1{s}','o2H']}
                                              for s in ('H','D')]}}


@pytest.fixture
def setup():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.published-settlement-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        publications=PublicationService(store,trusted_gate=lambda s: True)
        yield store,publications,PublishedSettlementService(store)


def publish(service, ident='p1', snapshot=None):
    snapshot=original() if snapshot is None else snapshot
    service.save_draft(ADMIN,'csrf',ident,snapshot)
    service.review(REVIEWER,'rcsrf',ident,'r'+ident)
    service.confirm(ADMIN,'csrf',draft_id=ident,review_id='r'+ident,publication_id=ident,
                    request_id='req'+ident,human_confirmed=True)
    return snapshot


def result(snapshot, leg_id, score=(1,0)):
    leg=next(v for v in snapshot['settlement_ticket']['legs'] if v['leg_id']==leg_id)
    return ResultSnapshot(Identity(**leg['identity']),'finished','H:A',score,
                          'regulation_90',source_id='synthetic-authority')


def test_original_loaded_from_publication_and_complete_branches_aggregated(setup):
    store,pub,service=setup
    snapshot=publish(pub)
    summary=service.record(ADMIN,'csrf',publication_id='p1',results={
        'l1':result(snapshot,'l1',(0,0)),'l2':result(snapshot,'l2')})
    assert summary['options']['o1H']['outcome']=='miss'
    assert summary['options']['o1D']['outcome']=='hit'
    assert summary['legs'][0]['outcome']=='hit' and summary['outcome']=='hit'
    assert {b['outcome'] for b in summary['branches']}=={'hit','miss'}
    assert all(b['original_odds']==['4','4'] and b['total_investment']=='2' for b in summary['branches'])
    assert summary['profitability'] is None


def test_failed_option_does_not_fail_leg_with_pending_alternative(setup):
    _,pub,service=setup
    snapshot=original()
    snapshot['settlement_ticket']['legs'][0]['options'][1]['market']='HAFU'
    publish(pub,snapshot=snapshot)
    summary=service.record(ADMIN,'csrf',publication_id='p1',results={
        'l1':result(snapshot,'l1',(0,1)),'l2':result(snapshot,'l2')})
    assert summary['options']['o1H']['outcome']=='miss'
    assert summary['options']['o1D']['outcome']=='pending'
    assert summary['legs'][0]['outcome']=='pending' and summary['outcome']=='pending'


def test_confirmed_failed_leg_with_other_leg_pending(setup):
    _,pub,service=setup
    snapshot=publish(pub)
    summary=service.record(ADMIN,'csrf',publication_id='p1',results={'l1':result(snapshot,'l1',(0,1))})
    assert summary['outcome']=='miss' and summary['legs'][1]['outcome']=='pending'
    stats=service.statistics()
    assert stats['settled_denominator']==1 and stats['leg_settled_denominator']==1


def test_publication_universe_keeps_unreviewed_withdrawn_and_superseded(setup):
    store,pub,service=setup
    publish(pub)
    for i,status in ((2,'withdrawn'),(3,'superseded')):
        snapshot=original();snapshot['slot_key']=f'slot{i}'
        publish(pub,f'p{i}',snapshot)
        with closing(store.connect()) as db,db:
            db.execute('UPDATE publications SET status=? WHERE publication_id=?',(status,f'p{i}'))
    assert service.statistics()['actual_recommendations']==3
    assert service.statistics()['pending']==3
    assert {row['publication_status'] for row in service.ledger()}=={'published','withdrawn','superseded'}


def test_correction_replay_latest_only_and_frozen_prices_unchanged(setup):
    store,pub,service=setup
    snapshot=publish(pub)
    values={'l1':result(snapshot,'l1'),'l2':result(snapshot,'l2')}
    assert service.record(ADMIN,'csrf',publication_id='p1',results=values)['version']==1
    assert service.record(ADMIN,'csrf',publication_id='p1',results=values)['version']==1
    values['l1']=result(snapshot,'l1',(0,1))
    with pytest.raises(ValueError):service.record(ADMIN,'csrf',publication_id='p1',results=values)
    assert service.record(ADMIN,'csrf',publication_id='p1',results=values,correction_reason='official correction')['version']==2
    assert service.record(ADMIN,'csrf',publication_id='p1',results=values,correction_reason='official correction')['version']==2
    assert service.statistics()['actual_recommendations']==1 and service.statistics()['miss']==1
    with closing(store.connect()) as db:
        assert len(list(db.execute('SELECT * FROM published_settlement_versions')))==2


@pytest.mark.parametrize('mutation',['missing_branch','wrong_total','missing_price'])
def test_malformed_frozen_ticket_rejected_before_settlement_write(setup,mutation):
    store,pub,service=setup
    snapshot=original(); ticket=snapshot['settlement_ticket']
    if mutation=='missing_branch':ticket['branches'].pop()
    elif mutation=='wrong_total':ticket['total_investment']='1'
    else:del ticket['legs'][0]['options'][0]['original_odds']
    publish(pub,snapshot=snapshot)
    with pytest.raises((ValueError,KeyError)):
        service.record(ADMIN,'csrf',publication_id='p1',results={})
    with closing(store.connect()) as db:
        assert not list(db.execute('SELECT * FROM published_settlement_versions'))


def test_unknown_publication_or_result_leg_and_viewer_rejected(setup):
    _,pub,service=setup
    snapshot=publish(pub)
    with pytest.raises(ValueError):service.record(ADMIN,'csrf',publication_id='unknown',results={})
    with pytest.raises(ValueError):service.record(ADMIN,'csrf',publication_id='p1',results={'unknown':result(snapshot,'l1')})
    with pytest.raises(PermissionError):service.record(Principal('v','viewer','v'),'v',publication_id='p1',results={})
