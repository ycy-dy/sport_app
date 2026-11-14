"""Frozen Codex input assembly only; does not invoke an agent or network."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from .manifest import POLICY_FILES
from .publication import fingerprint

REQUIRED_OPTION_FIELDS = (
    'event_definition', 'settlement_scope', 'evidence_as_of',
    'strongest_counterevidence', 'covered_paths', 'excluded_paths',
    'failure_paths', 'withdraw_if', 'market_benchmark_method', 'selection_comparison',
)
IDENTITY_FIELDS = ('sport', 'league', 'business_day', 'match_id', 'kickoff',
                   'home_team', 'away_team', 'market', 'line', 'original_odds',
                   'source_as_of')


def validate_event_binding(option):
    from .core import _decimal
    event = option['event_definition']
    no_line = {'HAD','MNL','TTG','HAFU','CRS'}
    needs_line = {'HHAD','HDC','HILO'}
    market = event['market']
    if market in no_line:
        if event['line'] is not None:
            raise ValueError('non-handicap market line must be explicit null')
    elif market in needs_line:
        if isinstance(event['line'],bool) or event['line'] is None:
            raise ValueError('market requires finite line')
        number = _decimal(event['line'])
        if market == 'HHAD' and number != number.to_integral_value():
            raise ValueError('HHAD requires integer line')
    else:
        raise ValueError('unknown market line contract')
    if option['market'] != market or option['line'] != event['line']:
        raise ValueError('event market/line differs from frozen option')
    identity = option['identity']
    for key in ('sport','league','business_day','match_id','kickoff'):
        if event[key] != identity[key]:
            raise ValueError('event identity differs from option: '+key)
    if event['home_team'] != identity['home_id'] or event['away_team'] != identity['away_id']:
        raise ValueError('event teams differ from frozen stable identities')
    if option['scope'] != option['settlement_scope']:
        raise ValueError('event settlement scope differs from option')
    expected_scope = 'regulation_90' if identity['sport']=='football' else 'including_ot' if identity['sport']=='basketball' else None
    if (expected_scope is None or option['scope'] != expected_scope
            or (identity['sport']=='football' and market not in {'HAD','HHAD','TTG','HAFU','CRS'})
            or (identity['sport']=='basketball' and market not in {'MNL','HDC','HILO'})):
        raise ValueError('invalid sport/market/scope')
    if _decimal(event['original_odds']) != _decimal(option['original_odds']):
        raise ValueError('event price differs from option')


def build_agent_input(*, options, candidates, exclusions, runtime_manifest, rule_documents):
    """Embed ALL five exact rule texts, including the complete 039 contract.

    rules/manifest come from the server's approved frozen policy registry, not
    request data. The hash binds every field and preserves the full candidate
    universe; no task is scheduled here.
    """
    if runtime_manifest.get('runtime_provider') != 'codex':
        raise ValueError('Codex only')
    if set(rule_documents) != set(POLICY_FILES):
        raise ValueError('complete 014/039 documents required')
    hashes = {}
    for name, value in rule_documents.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError('empty policy document')
        hashes[name] = sha256(value.encode('utf-8')).hexdigest()
    if hashes != runtime_manifest.get('rule_hashes'):
        raise ValueError('policy document hash mismatch')
    if not options or not candidates:
        raise ValueError('options and full candidates required')
    for option in options:
        if any(key not in option or option[key] in (None, '', []) for key in REQUIRED_OPTION_FIELDS):
            raise ValueError('incomplete 039 option')
        event = option['event_definition']
        if not isinstance(event, dict) or any(key not in event or (key != 'line' and event[key] in (None, '')) for key in IDENTITY_FIELDS):
            raise ValueError('incomplete event identity')
        validate_event_binding(option)
        for key in ('evidence_as_of',):
            stamp = datetime.fromisoformat(option[key])
            if stamp.tzinfo is None:
                raise ValueError('timezone required')
        if option.get('model_probability') is not None or option.get('EV') is not None:
            raise ValueError('unaccepted model probability/EV must be null')
    payload = deepcopy({'runtime_provider': 'codex', 'trigger': 'manual',
                        'runtime_manifest': runtime_manifest, 'rule_documents': rule_documents,
                        'options': options, 'candidates': candidates, 'exclusions': exclusions,
                        'model_probability': None, 'EV': None,
                        'execution_status': 'input_assembled_not_executed'})
    return {'input': payload, 'input_hash': fingerprint(payload)}
