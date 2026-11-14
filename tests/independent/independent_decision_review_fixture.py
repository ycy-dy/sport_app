"""Synthetic server reviews, not official coverage or semantic attestation."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from contextlib import closing
from sports_app.auth import Principal
from sports_app.evidence import Claim
from sports_app.decision_review import DecisionReviewRepository
from test_pair_policy_contract import AT

AUTHOR=Principal('review-fixture-author','admin','a')
READER=Principal('review-fixture-reviewer','reviewer','r')


def add_coverage(value):
    for option in value['options'].values():
        option['coverage_contract']=dict(catalog_id='independent-HDA',version='1',source_id='synthetic',
                                        exhaustive=True,universe=['H','D','A'])
        option['covered_atoms']=[option['selection']]
    return value


def attach_reviews(policy, record):
    """Load real frozen batch and store immutable authenticated comparison/risk reviews."""
    if record is None:return None
    result=deepcopy(record)
    batch,_=policy._load('batch')
    repo=policy.review_repository
    if repo is None:
        repo=DecisionReviewRepository(policy);policy.review_repository=repo
    def exists(ident):
        with closing(policy.store.connect()) as db:
            return db.execute('SELECT 1 FROM decision_review_records WHERE review_id=?',(ident,)).fetchone() is not None
    for option_id, comparison in result.get('option_comparisons',{}).items():
        ident='independent-comparison-'+option_id
        if not exists(ident):
            claim_id='review-source-'+option_id
            context=policy.evidence.load_context(option_id,as_of=AT)
            if not any(c.claim_id==claim_id for c in context.claims):
                quote='synthetic comparison rationale '+option_id
                claim=Claim(claim_id,option_id,('01',),'https://review.example.org/report','L1',
                            AT-timedelta(hours=1),AT-timedelta(hours=1),'review-group',quote,True,False)
                policy.evidence.ingest(AUTHOR,'a',claim)
                policy.evidence.verify(READER,'r',receipt_id=claim_id+'receipt',claim_id=claim_id,
                    quote_hash=sha256(quote.encode()).hexdigest(),conclusion='verified')
            else:quote=next(c.quote for c in context.claims if c.claim_id==claim_id)
            repo.save(READER,'r',review_id=ident,batch_id='batch',kind='comparison',scope=[option_id],
                source_quotes={option_id:{claim_id:sha256(quote.encode()).hexdigest()}},
                rationale='synthetic human comparison, no semantic proof',valid_until=(AT+timedelta(minutes=30)).isoformat())
        comparison['review_id']=ident
    for candidate_id,veto in result.get('candidate_vetoes',{}).items():
        scope=veto.get('option_ids',[]);quotes={}
        for option_id in scope:
            claims={c.claim_id:c for c in policy.evidence.load_context(option_id,as_of=AT).claims}
            quotes[option_id]={key:sha256(claims[key].quote.encode()).hexdigest()
                              for key in veto.get('claim_ids',[]) if key in claims}
        # Missing-source tests must reach the policy rejection, not fail inside this fixture.
        if not scope or any(not q for q in quotes.values()):continue
        ident='independent-risk-'+candidate_id
        if not exists(ident):
            repo.save(READER,'r',review_id=ident,batch_id='batch',kind='risk',scope=scope,source_quotes=quotes,
                rationale='synthetic scoped risk review',valid_until=(AT+timedelta(minutes=30)).isoformat())
        veto['review_id']=ident
    return result
