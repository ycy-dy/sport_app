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
        if not isinstance(event, dict) or any(key not in event or event[key] in (None, '') for key in IDENTITY_FIELDS):
            raise ValueError('incomplete event identity')
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
