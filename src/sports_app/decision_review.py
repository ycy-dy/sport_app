"""Immutable server comparison/risk review. Human semantics, deterministic bindings.

Coverage uses explicit finite event atoms from the approved market adapter;
no score-grid truncation may be called exhaustive. This module does not prove
an adapter catalog or human interpretation true. No network or routes.
"""
from contextlib import closing
from hashlib import sha256
from decimal import Decimal
from datetime import timedelta
import json
from .auth import require
from .publication import canonical,fingerprint
from .evidence import text

SCHEMA="""
CREATE TABLE IF NOT EXISTS decision_review_records (
 review_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
 digest TEXT NOT NULL, reviewer_id TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS decision_review_no_update BEFORE UPDATE ON decision_review_records
 BEGIN SELECT RAISE(ABORT,'immutable decision review'); END;
CREATE TRIGGER IF NOT EXISTS decision_review_no_delete BEFORE DELETE ON decision_review_records
 BEGIN SELECT RAISE(ABORT,'immutable decision review'); END;
"""


class DecisionReviewRepository:
    def __init__(self,policy):
        self.policy=policy;self.store=policy.store
        with closing(self.store.connect()) as db,db:db.executescript(SCHEMA)

    def _sources(self,batch,scope,source_quotes,at):
        if set(source_quotes)!=set(scope):raise ValueError('source evidence required for every scoped option')
        bindings={}
        for ident in scope:
            context=self.policy.evidence.load_context(ident,as_of=at)
            claims={claim.claim_id:claim for claim in context.claims}
            quotes=source_quotes[ident]
            if not isinstance(quotes,dict) or not quotes:raise ValueError('quoted sources required')
            for key,digest in quotes.items():
                claim=claims.get(key)
                if (claim is None or claim.source_verified is not True or claim.conflict
                        or not claim.published_at <= claim.effective_at <= at
                        or at-claim.effective_at > timedelta(days=30)
                        or (set(claim.dimension_ids)&{'04','13','18','19','21'}
                            and at-claim.effective_at > timedelta(hours=48))
                        or sha256(claim.quote.encode()).hexdigest()!=digest):
                    raise ValueError('quote/source binding invalid')
                receipts=[r for r in context.receipts if r.claim_id==key and r.option_id==ident
                          and r.quote_hash==digest and r.conclusion=='verified' and text(r.actor_id)]
                if not receipts:raise ValueError('current human source receipt required')
                bindings[ident+':'+key]={'quote':claim.quote,'quote_hash':digest,
                                        'url':claim.url,'receipt_actors':sorted({r.actor_id for r in receipts})}
        return bindings

    def ticket_costs(self,batch,option_id,replacement_id,at):
        from .publication_ticket import prepare_snapshot
        from .published_settlement import frozen_ticket
        from .pair_policy import category, STRUCTURES, market_probability
        from .core import _decimal
        from itertools import product
        results={}
        for key,candidate in batch['candidates'].items():
            if option_id not in candidate['option_ids']:continue
            ids=[replacement_id if value==option_id else value for value in candidate['option_ids']]
            detail={'option_ids':ids,'equal_stake':candidate['equal_stake'],
                    'total_investment':candidate['total_investment'],'branches':[],
                    'compliant':False,'reasons':[]}
            try:
                if len(set(ids))!=len(ids):raise ValueError('replacement duplicates existing option')
                options=[batch['options'][ident] for ident in ids]
                groups={}
                for option in options:
                    market_probability(option,batch,at)
                    groups.setdefault(option['leg_id'],[]).append(option)
                if len(groups)!=2:raise ValueError('replacement changes frozen two-leg structure')
                cats=[category(group) for group in groups.values()]
                if batch['sport']=='football' and tuple(sorted(cats)) not in STRUCTURES:
                    raise ValueError('replacement is not an authorized structure')
                # Enumerate BEFORE cost gate so weak alternatives remain auditable.
                legs=[groups[leg] for leg in sorted(groups)]
                stake=_decimal(candidate['equal_stake']);total=_decimal(candidate['total_investment'])
                if stake<=0 or total<=0:raise ValueError('nonpositive investment')
                for a,b in product(*legs):
                    odds=[_decimal(a['original_odds']),_decimal(b['original_odds'])]
                    effective=odds[0]*odds[1]*stake/total
                    detail['branches'].append({'option_ids':[a['option_id'],b['option_id']],
                        'original_odds':[str(v) for v in odds],'odds_product':str(odds[0]*odds[1]),
                        'effective_multiple':str(effective)})
                ticket=prepare_snapshot({'options':options,'equal_stake':candidate['equal_stake'],
                                         'total_investment':candidate['total_investment']})
                frozen_ticket(ticket)
                detail['compliant']=True
            except (ValueError,KeyError,TypeError) as exc:
                detail['reasons'].append(str(exc))
            results[key]=detail
        if not results:raise ValueError('selected option not in candidate universe')
        return results

    def comparison(self,batch,option_id,at):
        from .pair_policy import market_probability
        selected=batch['options'][option_id]
        alternatives={key:value for key,value in batch['options'].items()
                      if key!=option_id and value['identity']==selected['identity']}
        if not alternatives:raise ValueError('same-match alternative universe absent')
        coverage=selected.get('coverage_contract')
        if (not isinstance(coverage,dict) or not text(coverage.get('catalog_id'))
                or not text(coverage.get('version')) or not text(coverage.get('source_id'))
                or coverage.get('exhaustive') is not True):
            raise ValueError('approved exhaustive coverage catalog required')
        universe=coverage.get('universe')
        if not isinstance(universe,list) or not universe or any(not text(v) for v in universe) or len(set(universe))!=len(universe):
            raise ValueError('invalid event atom universe')
        def atoms(option):
            if option.get('coverage_contract')!=coverage:raise ValueError('incoherent cross-market coverage catalog')
            values=option.get('covered_atoms')
            if (not isinstance(values,list) or not values or any(not text(v) for v in values)
                    or len(set(values))!=len(values) or not set(values)<=set(universe)):
                raise ValueError('invalid frozen event coverage')
            return set(values)
        chosen=atoms(selected)
        selected_probability=market_probability(selected,batch,at)
        result={}
        for key,alt in alternatives.items():
            other=atoms(alt)
            probability=market_probability(alt,batch,at)
            result[key]={'market':alt['market'],'line':alt['line'],'original_odds':alt['original_odds'],
                         'market_snapshot_id':alt['market_snapshot_id'],'p_mkt':str(probability),
                         'retained':sorted(chosen&other),'added':sorted(chosen-other),
                         'lost':sorted(other-chosen),'price_ratio':str(Decimal(selected['original_odds'])/Decimal(alt['original_odds'])),
                         'replacement_costs':self.ticket_costs(batch,option_id,key,at)}
            # A narrower event cannot enter purely for a better price.
            if (chosen<other and selected_probability<probability
                    and any(value['compliant'] for value in result[key]['replacement_costs'].values())):
                raise ValueError('narrower event below same-match compliant alternative benchmark')
        costs=self.ticket_costs(batch,option_id,option_id,at)
        if not costs:raise ValueError('selected option not in candidate universe')
        return {'selected_option_id':option_id,'selected_p_mkt':str(selected_probability),
                'alternatives':result,'candidate_costs':costs,'coverage_contract':coverage}

    def save(self,principal,csrf,*,review_id,batch_id,kind,scope,source_quotes,rationale,valid_until):
        from .pair_policy import stamp
        require(principal,{'reviewer'},csrf)
        batch,at=self.policy._load(batch_id)
        if (kind not in {'comparison','risk'} or not text(review_id) or not text(rationale)
                or not isinstance(scope,list) or not scope or any(key not in batch['options'] for key in scope)
                or len(set(scope))!=len(scope) or stamp(valid_until)<=at):
            raise ValueError('invalid review scope/rationale/lifetime')
        computed={key:self.comparison(batch,key,at) for key in scope} if kind=='comparison' else {}
        body={'batch_id':batch_id,'batch_hash':fingerprint(batch),'manifest_hash':fingerprint(batch['manifest']),
              'market_version':batch['market_version'],'evidence_version':batch['evidence_version'],
              'scope':sorted(scope),'sources':self._sources(batch,scope,source_quotes,at),
              'source_quotes':source_quotes,'rationale':rationale,'valid_until':valid_until,'computed':computed}
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO decision_review_records VALUES(?,?,?,?,?,?)',
                       (review_id,kind,canonical(body),fingerprint(body),principal.actor_id,at.isoformat()))
        return review_id

    def validate(self,review_id,*,batch_id,batch,kind,scope,at):
        from .pair_policy import stamp
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM decision_review_records WHERE review_id=?',(review_id,)).fetchone()
        if row is None or row['kind']!=kind:raise ValueError('server decision review missing')
        body=json.loads(row['body'])
        if (fingerprint(body)!=row['digest'] or not text(row['reviewer_id'])
                or not stamp(row['recorded_at'])<=at<stamp(body['valid_until'])
                or body['batch_id']!=batch_id or body['batch_hash']!=fingerprint(batch)
                or body['manifest_hash']!=fingerprint(batch['manifest']) or body['scope']!=sorted(scope)
                or body['sources']!=self._sources(batch,scope,body['source_quotes'],at)):
            raise ValueError('review stale, wrong scope, source or version')
        if kind=='comparison' and body['computed']!={key:self.comparison(batch,key,at) for key in scope}:
            raise ValueError('comparison changed')
        return body
