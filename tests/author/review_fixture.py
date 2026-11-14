"""Explicit synthetic reviewed evidence fixture; not real semantic verification."""
from datetime import timedelta
from hashlib import sha256
from sports_app.evidence import Claim
from sports_app.decision_review import DecisionReviewRepository
from test_pair_policy import ADMIN,REVIEWER,AT


def add_coverage(batch):
    by_match={}
    for key,option in batch['options'].items():
        by_match.setdefault(option['identity']['match_id'],[]).append(key)
    for keys in by_match.values():
        catalog={'catalog_id':'synthetic-'+keys[0],'version':'synthetic-v1',
                 'source_id':'synthetic-source','exhaustive':True,'universe':sorted(keys)}
        for key in keys:
            batch['options'][key]['coverage_contract']=catalog
            batch['options'][key]['covered_atoms']=[key]


def attach_reviews(policy,record,batch_id):
    batch,_=policy._load(batch_id)
    repo=DecisionReviewRepository(policy);policy.review_repository=repo
    def sources(option_id):
        ident='review-source-'+option_id
        context=policy.evidence.load_context(option_id,as_of=AT)
        existing=next((c for c in context.claims if c.claim_id==ident),None)
        quote='synthetic interpretation source '+option_id
        digest=sha256(quote.encode()).hexdigest()
        if existing is None:
            claim=Claim(ident,option_id,('01',),'https://review.example.org/'+option_id,'L1',
                        AT-timedelta(hours=1),AT-timedelta(hours=1),'review-group',quote,True,False)
            policy.evidence.ingest(ADMIN,'a',claim)
            policy.evidence.verify(REVIEWER,'r',receipt_id=ident+'receipt',claim_id=ident,
                                   quote_hash=digest,conclusion='verified')
        return {ident:digest}
    for option_id,comparison in record.get('option_comparisons',{}).items():
        review_id='comparison-'+option_id
        repo.save(REVIEWER,'r',review_id=review_id,batch_id=batch_id,kind='comparison',scope=[option_id],
                  source_quotes={option_id:sources(option_id)},rationale='synthetic reviewed interpretation',
                  valid_until=(AT+timedelta(minutes=30)).isoformat())
        comparison['review_id']=review_id
        comparison['alternative_option_ids']=[key for key,value in batch['options'].items()
                      if key!=option_id and value['identity']==batch['options'][option_id]['identity']]
        comparison.pop('server_comparison_verified',None)
    for candidate_id,veto in record.get('candidate_vetoes',{}).items():
        review_id='risk-'+candidate_id
        repo.save(REVIEWER,'r',review_id=review_id,batch_id=batch_id,kind='risk',scope=veto['option_ids'],
                  source_quotes={key:sources(key) for key in veto['option_ids']},
                  rationale='synthetic reviewed risk scope',valid_until=(AT+timedelta(minutes=30)).isoformat())
        veto['review_id']=review_id
    return record
