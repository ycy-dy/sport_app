"""Independent immutable-original/union/ledger checks. Synthetic gate only."""
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import pytest
from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.publication import PublicationService, canonical
from sports_app.published_settlement import PublishedSettlementService
from sports_app.settlement import Identity, ResultSnapshot

ADMIN = Principal('independent-admin','admin','csrf')
REVIEWER = Principal('independent-reviewer','reviewer','review-csrf')


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


@pytest.fixture
def setup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Use project-owned --basetemp'
    store = SQLiteStore(tmp_path/'synthetic', app_root=root)
    return store, PublicationService(store,trusted_gate=lambda body: True), PublishedSettlementService(store)


def original():
    legs=[]
    for i in (1,2):
        identity=Identity('football','synthetic-league','2026-11-14',f'm{i}',
                          '2026-11-14T18:00:00+08:00',f'h{i}',f'a{i}')
        options=[dict(option_id=f'o{i}{s}',market='HHAD',selection=s,line='-1',original_odds='4')
                 for s in (('H','D') if i==1 else ('H',))]
        legs.append(dict(leg_id=f'l{i}',identity=asdict(identity),scope='regulation_90',options=options))
    return dict(slot_key='synthetic-slot',selection_hash='synthetic-selection',manifest_hash='synthetic-manifest',
                recommendation_status='conditional_low_confidence',settlement_ticket=dict(legs=legs,
                    equal_stake='1',total_investment='2',branches=[dict(branch_id=f'b{s}',option_ids=[f'o1{s}','o2H']) for s in ('H','D')]))


def publish(pub, body, ident='p1', replace_id=None):
    pub.save_draft(ADMIN,'csrf',ident,body)
    pub.review(REVIEWER,'review-csrf',ident,'r'+ident)
    pub.confirm(ADMIN,'csrf',draft_id=ident,review_id='r'+ident,publication_id=ident,
                request_id='req'+ident,human_confirmed=True,replace_id=replace_id)


def result(body, index, score):
    leg=body['settlement_ticket']['legs'][index]
    return ResultSnapshot(Identity(**leg['identity']),'finished','H:A',score,
                          'regulation_90',source_id='synthetic-result')


def version_count(store):
    with closing(store.connect()) as db:
        return db.execute('SELECT COUNT(*) FROM published_settlement_versions').fetchone()[0]


def test_frozen_handicap_prices_and_original_survive_caller_mutation(setup):
    store,pub,svc=setup
    body=original(); publish(pub,body)
    results={'l1':result(body,0,(2,1)),'l2':result(body,1,(3,0))}
    before=canonical(body)
    body['settlement_ticket']['legs'][0]['identity']['home_id']='forged'
    body['settlement_ticket']['legs'][0]['options'][0].update(line='99',original_odds='999')
    summary=svc.record(ADMIN,'csrf',publication_id='p1',results=results)
    assert summary['options']['o1H']['outcome']=='miss'
    assert summary['options']['o1D']['outcome']=='hit'
    assert summary['outcome']=='hit'
    assert all(row['original_odds']==['4','4'] for row in summary['branches'])
    assert summary['profitability'] is None and summary['actual_return'] is None
    with closing(store.connect()) as db:
        assert db.execute("SELECT original_json FROM publications WHERE publication_id='p1'").fetchone()[0]==before
    with pytest.raises(TypeError):
        svc.record(ADMIN,'csrf',publication_id='p1',results=results,original=body)
    assert version_count(store)==1


@pytest.mark.parametrize('mutation',['missing_branch','duplicate_pair','wrong_total','missing_price'])
def test_invalid_frozen_universe_never_writes_settlement(setup,mutation):
    store,pub,svc=setup
    body=original(); ticket=body['settlement_ticket']
    if mutation=='missing_branch': ticket['branches'].pop()
    elif mutation=='duplicate_pair': ticket['branches'][1]['option_ids']=ticket['branches'][0]['option_ids'][:]
    elif mutation=='wrong_total': ticket['total_investment']='1'
    else: del ticket['legs'][0]['options'][0]['original_odds']
    publish(pub,body)
    with pytest.raises((ValueError,KeyError)):
        svc.record(ADMIN,'csrf',publication_id='p1',results={})
    assert version_count(store)==0


def test_one_miss_one_pending_option_keeps_union_pending(setup):
    _,pub,svc=setup
    body=original()
    body['settlement_ticket']['legs'][0]['options'][1]['market']='HAFU'
    publish(pub,body)
    summary=svc.record(ADMIN,'csrf',publication_id='p1',results={
        'l1':result(body,0,(0,1)),'l2':result(body,1,(3,0))})
    assert summary['options']['o1H']['outcome']=='miss'
    assert summary['options']['o1D']['outcome']=='pending'
    assert summary['legs'][0]['outcome']=='pending' and summary['outcome']=='pending'
    assert {row['outcome'] for row in summary['branches']}=={'miss','pending'}


def test_full_leg_miss_with_other_pending_is_ticket_miss(setup):
    _,pub,svc=setup
    body=original();publish(pub,body)
    summary=svc.record(ADMIN,'csrf',publication_id='p1',results={'l1':result(body,0,(0,1))})
    assert summary['outcome']=='miss' and summary['legs'][1]['outcome']=='pending'
    stats=svc.statistics()
    assert stats['settled_denominator']==stats['leg_settled_denominator']==1
    assert stats['leg_pending']==1


def test_history_universe_correction_and_replay_count_once(setup):
    store,pub,svc=setup
    body=original();publish(pub,body)
    replacement=original();publish(pub,replacement,'p2',replace_id='p1')
    third=original();third['slot_key']='another-slot';publish(pub,third,'p3')
    with closing(store.connect()) as db,db:
        db.execute("UPDATE publications SET status='withdrawn' WHERE publication_id='p3'")
    assert svc.statistics()['actual_recommendations']==3 and svc.statistics()['pending']==3
    values={'l1':result(body,0,(3,0)),'l2':result(body,1,(3,0))}
    first=svc.record(ADMIN,'csrf',publication_id='p1',results=values)
    assert svc.record(ADMIN,'csrf',publication_id='p1',results=values)==first
    values['l1']=result(body,0,(0,1))
    with pytest.raises(ValueError): svc.record(ADMIN,'csrf',publication_id='p1',results=values)
    corrected=svc.record(ADMIN,'csrf',publication_id='p1',results=values,correction_reason='synthetic correction')
    assert svc.record(ADMIN,'csrf',publication_id='p1',results=values)==corrected
    assert corrected['version']==2 and version_count(store)==2
    rows=svc.ledger()
    assert len(rows)==3
    assert {r['publication_status'] for r in rows}=={'published','withdrawn','superseded'}
    stats=svc.statistics()
    assert stats['actual_recommendations']==3 and stats['settled_denominator']==1
    assert stats['miss']==1 and stats['pending']==2
