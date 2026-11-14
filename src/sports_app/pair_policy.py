"""Offline regular-pair policy gate over an admin-frozen server batch.

No routes, fetching or Codex invocation. Ingestion is an internal trusted
adapter boundary, not proof of source truth/completeness. Missing adapter
metadata fails closed. Approved policy registry must be supplied by assembly.
"""
from contextlib import closing
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from itertools import product
import json
from zoneinfo import ZoneInfo
from .auth import require
from .core import score_assessment, validate_option, Option, _decimal
from .evidence import WEIGHTS, VERSION, aware, text
from .manifest import POLICY_FILES
from .publication import canonical, fingerprint
from .publication_ticket import prepare_snapshot

TZ = ZoneInfo('Asia/Shanghai')
STRUCTURES = {tuple(sorted(p)) for p in [('HAD','DD'),('HAD','TTG'),('HHAD','TTG'),
              ('HHAD','DD'),('HAFU','DD'),('HAFU','TTG'),('HAD','HAD'),('HAFU','HAFU')]}
SCHEMA = """
CREATE TABLE IF NOT EXISTS pair_policy_batches (
 batch_id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS pair_batch_no_update BEFORE UPDATE ON pair_policy_batches
 BEGIN SELECT RAISE(ABORT,'immutable batch'); END;
CREATE TRIGGER IF NOT EXISTS pair_batch_no_delete BEFORE DELETE ON pair_policy_batches
 BEGIN SELECT RAISE(ABORT,'immutable batch'); END;
"""


def stamp(value):
    result = datetime.fromisoformat(value)
    if not aware(result):
        raise ValueError('timezone required')
    return result


def periods(batch):
    """Server full schedule determines football early/middle merge, not selection."""
    day = date.fromisoformat(batch['business_day'])
    start = datetime.combine(day,time(12),TZ)
    stop = datetime.combine(day+timedelta(days=1),time(9),TZ)
    events, raw = {}, {}
    for event in batch['events']:
        ident = event['identity']
        if ident['business_day'] != batch['business_day'] or ident['sport'] != batch['sport']:
            raise ValueError('schedule identity mismatch')
        key = ident['match_id']
        if key in events:
            raise ValueError('duplicate schedule match')
        kickoff = stamp(ident['kickoff']).astimezone(TZ)
        if batch['sport'] == 'football':
            if not start <= kickoff <= stop:
                raise ValueError('football event outside business window')
            label = 'early' if kickoff <= start.replace(hour=19,minute=30) else 'middle' if kickoff < start.replace(hour=23) else 'late'
        elif batch['sport'] == 'basketball':
            # Business-day membership comes from the trusted full schedule.
            # Do not infer a basketball end boundary or apply football's window.
            label = 'early' if kickoff < datetime.combine(day,time(23),TZ) else 'late'
        else:
            raise ValueError('unsupported sport')
        events[key],raw[key] = event,label
    if not events:
        raise ValueError('empty schedule')
    middle = sum(label=='middle' for label in raw.values())
    early = sum(label=='early' for label in raw.values())
    merge = batch['sport']=='football' and middle<2 and early+middle>=2
    return events,{key:('early_middle' if merge and label in {'early','middle'} else label) for key,label in raw.items()}


def category(options):
    markets = [o['market'] for o in options]
    if len(options)==2 and sorted(markets)==['HAD','HHAD']:
        if any(o['selection']!='D' for o in options):
            raise ValueError('DD requires HAD draw and HHAD draw')
        line = _decimal(next(o['line'] for o in options if o['market']=='HHAD'))
        if line==0:
            raise ValueError('DD overlapping zero handicap events unsupported')
        return 'DD'
    if all(m=='TTG' for m in markets) and len(options)==2:
        codes = [7 if o['selection']=='7+' else int(o['selection']) for o in options]
        if len(set(codes))!=2 or abs(codes[0]-codes[1])!=1:
            raise ValueError('TTG requires two adjacent distinct buckets')
        return 'TTG'
    if len(options)==1 and markets[0] in {'HAD','HHAD','HAFU','MNL','HDC','HILO'}:
        return markets[0]
    raise ValueError('unsupported regular-pair option composition')


def market_probability(option, batch, at):
    market = batch['markets'][option['market_snapshot_id']]
    if (market['match_id']!=option['identity']['match_id'] or market['market']!=option['market']
            or market['line']!=option['line'] or market['version']!=batch['market_version']
            or not text(market['source_id']) or market['on_sale'] is not True
            or market['conflict'] is not False
            or not stamp(market['as_of'])<=at<stamp(market['valid_until'])
            or not at<stamp(market['sales_close']) or market['supports_pair'] is not True):
        raise ValueError('unverified, stale, mismatched or unsaleable market')
    expected = {'HAD':{'H','D','A'},'HHAD':{'H','D','A'},
                'HAFU':{a+b for a in 'HDA' for b in 'HDA'},
                'TTG':{str(i) for i in range(7)}|{'7+'},
                'MNL':{'H','A'},'HDC':{'H','A'},'HILO':{'O','U'}}
    odds = market['outcomes']
    if set(odds)!=expected[option['market']]:
        raise ValueError('incomplete mutually exclusive market')
    values = {key:_decimal(value) for key,value in odds.items()}
    if any(value<=1 for value in values.values()):
        raise ValueError('invalid complete market odds')
    if _decimal(option['original_odds'])!=values[option['selection']]:
        raise ValueError('original price differs from market snapshot')
    return (Decimal(1)/values[option['selection']])/sum(Decimal(1)/v for v in values.values())


class RegularPairPolicy:
    def __init__(self, evidence_repository, *, approved_manifest, decision_loader=None, review_repository=None):
        self.evidence = evidence_repository
        self.decision_loader = decision_loader  # authenticated server runtime record lookup only
        self.review_repository = review_repository
        self.store = evidence_repository.store
        self.approved_manifest = json.loads(canonical(approved_manifest))
        if (approved_manifest.get('runtime_provider')!='codex'
                or set(approved_manifest.get('rule_hashes',{}))!=set(POLICY_FILES)
                or any(not text(approved_manifest.get(key)) for key in
                       ('policy_revision','analysis_revision','schema_hash','code_hash'))):
            raise ValueError('approved complete manifest required')
        with closing(self.store.connect()) as db, db:
            db.executescript(SCHEMA)
            db.executescript("""
CREATE TABLE IF NOT EXISTS agent_input_records (
 input_id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
 batch_id TEXT NOT NULL, batch_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS agent_input_no_update BEFORE UPDATE ON agent_input_records
 BEGIN SELECT RAISE(ABORT,'immutable agent input'); END;
CREATE TRIGGER IF NOT EXISTS agent_input_no_delete BEFORE DELETE ON agent_input_records
 BEGIN SELECT RAISE(ABORT,'immutable agent input'); END;
""")

    def freeze_batch(self, principal, csrf, batch_id, batch):
        require(principal,{'admin'},csrf)
        if not text(batch_id):
            raise ValueError('empty batch id')
        # This method records adapter output; no client assertion can refresh it.
        self._batch_contract(batch)
        body = canonical(batch)
        with closing(self.store.connect()) as db, db:
            db.execute('INSERT INTO pair_policy_batches VALUES(?,?,?,?,?)',
                       (batch_id,body,fingerprint(batch),principal.actor_id,self.evidence.clock().isoformat()))

    def _batch_contract(self,batch):
        if (batch['manifest']!=self.approved_manifest or batch['evidence_score_version']!=VERSION
                or not text(batch['evidence_version']) or not text(batch['market_version'])
                or batch['schedule_complete'] is not True or batch['candidate_universe_complete'] is not True
                or not text(batch['adapter_id']) or batch['conflict'] is not False):
            raise ValueError('missing trusted full universe/version bindings')
        periods(batch)
        if not isinstance(batch['candidates'],dict) or not batch['candidates']:
            raise ValueError('empty candidate universe')

    def _load(self,batch_id):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM pair_policy_batches WHERE batch_id=?',(batch_id,)).fetchone()
        if row is None:
            raise ValueError('batch not found')
        batch=json.loads(row['body'])
        if fingerprint(batch)!=row['digest']:
            raise ValueError('corrupt batch')
        self._batch_contract(batch)
        at=self.evidence.clock()
        if not stamp(batch['as_of'])<=at<stamp(batch['valid_until']):
            raise ValueError('batch no longer current; refresh upstream')
        return batch,at

    def _candidate(self,batch,at,candidate_id):
        candidate=batch['candidates'][candidate_id]
        ids=candidate['option_ids']
        if len(ids)!=len(set(ids)) or not ids:
            raise ValueError('duplicate/empty candidate options')
        options=[batch['options'][key] for key in ids]
        events,labels=periods(batch)
        groups={}
        assessments={}
        probabilities={}
        for option in options:
            ident=option['identity'];event=events[ident['match_id']]
            if (event['identity']!=ident or event['on_sale'] is not True
                    or event['conflict'] is not False or not text(event['source_id'])
                    or not stamp(event['as_of'])<=at<stamp(event['valid_until'])
                    or not at<stamp(event['sales_close']) or not at<stamp(ident['kickoff'])
                    or option['option_id'] not in ids or option['evidence_version']!=batch['evidence_version']):
                raise ValueError('identity, sale, cutoff or evidence binding invalid')
            groups.setdefault(option['leg_id'],[]).append(option)
            probabilities[option['option_id']]=market_probability(option,batch,at)
            context=self.evidence.load_context(option['option_id'],as_of=at)
            if any(c.conflict and set(c.dimension_ids)&set(WEIGHTS) for c in context.claims):
                raise ValueError('known evidence conflict')
            assessment=score_assessment(option['option_id'],tuple(tuple(v) for v in option['dimensions']),
                          reasons=tuple(option['reasons']),failure_paths=tuple(option['failure_paths']),
                          withdraw_if=tuple(option['withdraw_if']),claim_ids=tuple(option['claim_ids']),
                          assessment_id=option['assessment_id'],evidence_context=context)
            if validate_option(Option(option['option_id'],assessment),conditional=True):
                raise ValueError('option research or hard gate invalid')
            if not context.claims and not any('仅按市场基准' in reason for reason in option['reasons']):
                raise ValueError('source-free conditional research must disclose market-only basis')
            assessments[option['option_id']]=assessment
        if len(groups)!=2:
            raise ValueError('two legs required')
        legs=list(groups.values())
        leg_periods={labels[leg[0]['identity']['match_id']] for leg in legs}
        if len(leg_periods)!=1 or candidate['period'] not in leg_periods:
            raise ValueError('cross-period or mislabeled pair')
        cats=[category(leg) for leg in legs]
        if batch['sport']=='football' and tuple(sorted(cats)) not in STRUCTURES:
            raise ValueError('not an authorized eight structure')
        if batch['sport']=='basketball' and any(cat not in {'MNL','HDC','HILO'} for cat in cats):
            raise ValueError('invalid basketball structure')
        snapshot={'options':options,'equal_stake':candidate['equal_stake'],
                  'total_investment':candidate['total_investment']}
        prepared=prepare_snapshot(snapshot)  # distinct matches, original odds, all branches
        joint=Decimal(1)
        for leg in legs:
            joint*=sum(probabilities[o['option_id']] for o in leg)
        return prepared,assessments,joint

    def build(self,batch_id,candidate_id, *, _vetoed_options=frozenset()):
        batch,at=self._load(batch_id)
        viable={}
        for key in batch['candidates']:
            if set(batch['candidates'][key]['option_ids']) & set(_vetoed_options):
                continue
            try:
                viable[key]=self._candidate(batch,at,key)
            except (ValueError,KeyError,TypeError):
                continue
        if candidate_id not in viable:
            raise ValueError('selected candidate fails hard gates')
        prepared,assessments,joint=viable[candidate_id]
        period=batch['candidates'][candidate_id]['period']
        same={key:value for key,value in viable.items() if batch['candidates'][key]['period']==period}
        qualified={key:value for key,value in same.items() if all(a.eligible for a in value[1].values())}
        pool=qualified or same
        ranking=sorted(pool,key=lambda key:(-pool[key][2],key))
        if candidate_id not in pool:
            raise ValueError('qualified candidates exist; conditional fallback prohibited')
        status='evidence_qualified' if qualified else 'conditional_low_confidence'
        # Every current period uses one deterministic slot; replacement uses existing publication transaction.
        prepared.update(recommendation_type='regular_pair',recommendation_status=status,
                         slot_key=f"{batch['sport']}:{batch['business_day']}:{period}",
                         policy_batch_id=batch_id,candidate_id=candidate_id,
                         manifest_hash=fingerprint(batch['manifest']),
                         selection_hash=fingerprint(prepared['settlement_ticket']),
                         evidence_version=batch['evidence_version'],market_version=batch['market_version'],
                         policy_manifest=batch['manifest'],joint_p_mkt=str(joint),
                         benchmark_method='proportional_devig_labelled_independence_approximation',
                         assessment_summary={key:{'score':a.evidence_score,'eligible':a.eligible,
                                                   'quality_gaps':list(a.quality_gaps)} for key,a in assessments.items()},
                         market_ranking=ranking,decision_status='candidate_only_agent_incomplete',
                         model_probability=None,EV=None)
        if any(category(leg)=='DD' for leg in self._group(prepared['options'])):
            prepared['benchmark_risks']=['incoherent_cross_market_benchmark']
        return prepared

    @staticmethod
    def _group(options):
        groups={}
        for option in options:
            groups.setdefault(option['leg_id'],[]).append(option)
        return list(groups.values())

    def validate_candidate(self,snapshot):
        """Candidate plumbing only; NEVER a publication authorization gate."""
        try:
            expected=self.build(snapshot['policy_batch_id'],snapshot['candidate_id'])
            actual={key:value for key,value in snapshot.items() if key!='recommendation_freeze_id'}
            return canonical(expected)==canonical(actual)
        except (ValueError,KeyError,TypeError):
            return False

    def freeze_agent_input(self,principal,csrf,*,input_id,batch_id,assembled_input,transaction=None):
        require(principal,{'admin'},csrf)
        batch,at=self._load(batch_id)
        if not text(input_id) or not isinstance(assembled_input,dict):
            raise ValueError('invalid frozen input')
        # Freeze the actual assembled content AND the complete server universe;
        # input_hash is computed here, never accepted as an authentication flag.
        body={'assembled_input':assembled_input,'batch':batch}
        digest=fingerprint(body)
        values=(input_id,canonical(body),digest,batch_id,fingerprint(batch),
                fingerprint(batch['manifest']),principal.actor_id,at.isoformat())
        if transaction is not None:
            if not transaction.in_transaction:
                raise ValueError('active caller transaction required')
            transaction.execute('INSERT INTO agent_input_records VALUES(?,?,?,?,?,?,?,?)',values)
        else:
            with closing(self.store.connect()) as db, db:
                db.execute('INSERT INTO agent_input_records VALUES(?,?,?,?,?,?,?,?)',values)
        return digest

    def _input_binding(self,record,batch_id,batch):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM agent_input_records WHERE input_id=?',
                           (record.get('input_id'),)).fetchone()
        if row is None:
            raise ValueError('server input record missing')
        body=json.loads(row['body'])
        if (fingerprint(body)!=row['digest'] or record.get('input_hash')!=row['digest']
                or row['batch_id']!=batch_id or row['batch_hash']!=fingerprint(batch)
                or row['manifest_hash']!=fingerprint(batch['manifest']) or body['batch']!=batch):
            raise ValueError('execution input hash/universe/version mismatch')
        return body

    def _risk_vetoes(self,record,batch,at):
        vetoed=set()
        for candidate_id,veto in record.get('candidate_vetoes',{}).items():
            if candidate_id not in batch['candidates'] or not isinstance(veto,dict):
                raise ValueError('unknown risk veto')
            scope=veto.get('option_ids')
            if (not isinstance(scope,list) or not scope or len(scope)!=len(set(scope))
                    or not set(scope)<=set(batch['candidates'][candidate_id]['option_ids'])
                    or not text(veto.get('reason')) or not veto.get('claim_ids')):
                raise ValueError('risk veto must bind explicit affected options and sources')
            for option_id in scope:
                context=self.evidence.load_context(option_id,as_of=at)
                available={claim.claim_id:claim for claim in context.claims}
                linked=[available[key] for key in veto['claim_ids'] if key in available]
                if not linked or any(claim.source_verified is not True or claim.conflict for claim in linked):
                    raise ValueError('risk veto lacks verified source for each scoped option')
            if self.review_repository is None:
                raise ValueError('server risk review repository not connected')
            self.review_repository.validate(veto.get('review_id'),batch_id=record['_verified_batch_id'],
                                            batch=batch,kind='risk',scope=scope,at=at)
            vetoed.update(scope)
        return vetoed

    def final_snapshot(self,batch_id,candidate_id,decision_id):
        """Validate an immutable server execution record; no synthetic fallback.

        decision_loader must load records from the trusted Codex runtime store.
        No loader is installed by default and no HTTP route accepts such records.
        """
        if self.decision_loader is None:
            raise ValueError('EXECUTION_FAILURE: Codex execution not connected')
        batch,at=self._load(batch_id)
        record=self.decision_loader(decision_id)
        if (not isinstance(record,dict) or record.get('provider')!='codex'
                or record.get('status')!='completed' or record.get('execution_id')!=decision_id
                or record.get('batch_hash')!=fingerprint(batch)
                or record.get('selected_candidate_id')!=candidate_id
                or record.get('manifest_hash')!=fingerprint(batch['manifest'])
                or not text(record.get('input_hash')) or not text(record.get('raw_response'))
                or record.get('response_hash')!=fingerprint(record['raw_response'])
                or not stamp(batch['as_of'])<=stamp(record['completed_at'])<=at):
            raise ValueError('missing/mismatched frozen Codex execution')
        self._input_binding(record,batch_id,batch)
        vetoed=self._risk_vetoes(dict(record,_verified_batch_id=batch_id),batch,at)
        candidate=self.build(batch_id,candidate_id,_vetoed_options=vetoed)
        comparisons=record.get('option_comparisons',{})
        for option in candidate['options']:
            comparison=comparisons.get(option['option_id'])
            if (not isinstance(comparison,dict) or not text(comparison.get('rationale'))
                    or comparison.get('selected_option_id')!=option['option_id']
                    or comparison.get('market_version')!=batch['market_version']
                    or not comparison.get('alternative_option_ids')):
                raise ValueError('missing same-match alternative comparison')
            if option['option_id'] in comparison['alternative_option_ids']:
                raise ValueError('self comparison is not an alternative')
            alternatives=[batch['options'][key] for key in comparison['alternative_option_ids']]
            if any(alt['identity']!=option['identity'] for alt in alternatives):
                raise ValueError('comparison alternative identity mismatch')
            if self.review_repository is None:
                raise ValueError('server comparison repository not connected')
            reviewed=self.review_repository.validate(comparison.get('review_id'),batch_id=batch_id,
                        batch=batch,kind='comparison',scope=[option['option_id']],at=at)
            expected_alternatives=set(reviewed['computed'][option['option_id']]['alternatives'])
            if set(comparison['alternative_option_ids'])!=expected_alternatives:
                raise ValueError('comparison omitted server alternative universe')
        ranking=candidate['market_ranking']
        if ranking[0]!=candidate_id:
            raise ValueError('higher feasible candidate remains without valid sourced veto')
        candidate['vetoed_option_ids']=sorted(vetoed)
        # Original scores/p_mkt remain unchanged; only the feasible pool changes.
        candidate.update(decision_status='validated_server_execution',decision_id=decision_id,
                         decision_record_hash=fingerprint(record))
        return candidate

    def __call__(self,snapshot):
        try:
            expected=self.final_snapshot(snapshot['policy_batch_id'],snapshot['candidate_id'],snapshot['decision_id'])
            actual={key:value for key,value in snapshot.items() if key!='recommendation_freeze_id'}
            return canonical(expected)==canonical(actual)
        except (ValueError,KeyError,TypeError):
            return False
