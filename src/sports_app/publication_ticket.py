"""Generate immutable settlement structure from the evidence option universe.

No separately supplied settlement options are trusted. The complete option
records below are server-loaded research records, not HTTP DTOs. No route is
exposed. All supported settlement markets must be deterministically resolvable.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from itertools import product
from .core import _decimal
from .publication import canonical


def generate_ticket(snapshot):
    from .published_settlement import frozen_ticket
    options = snapshot.get('options')
    if not isinstance(options, list) or not options:
        raise ValueError('server research options required')
    grouped = {}
    seen = set()
    for option in options:
        if not isinstance(option, dict):
            raise ValueError('invalid research option')
        ident = option.get('option_id')
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError('duplicate or empty research option')
        seen.add(ident)
        if not isinstance(option.get('assessment_id'), str) or not option['assessment_id'].strip():
            raise ValueError('option assessment binding required')
        leg_id = option.get('leg_id')
        if not isinstance(leg_id,str) or not leg_id.strip():
            raise ValueError('leg identity required')
        identity = option['identity']
        required = {'sport','league','business_day','match_id','kickoff','home_id','away_id'}
        if set(identity) != required or any(not isinstance(v,str) or not v.strip() for v in identity.values()):
            raise ValueError('complete stable identity required')
        kickoff = datetime.fromisoformat(identity['kickoff'])
        if kickoff.tzinfo is None or identity['home_id'] == identity['away_id']:
            raise ValueError('invalid kickoff or teams')
        sport, market = identity['sport'], option['market']
        scope = option['scope']
        from .football_markets import validate_football_selection, HAFU
        allowed = {'football': {'HAD': {'H','D','A'}, 'HHAD': {'H','D','A'},
                                'TTG':{str(i) for i in range(7)} | {'7+'},
                                'HAFU': HAFU, 'CRS': {option['selection']}}, 
                   'basketball': {'MNL': {'H','A'}, 'HDC': {'H','A'}, 'HILO': {'O','U'}}}
        if sport not in allowed or market not in allowed[sport] or option['selection'] not in allowed[sport][market]:
            raise ValueError('unsupported deterministic settlement market or selection')
        if sport == 'football':
            validate_football_selection(market, option['selection'], option.get('market_contract'))
        if scope != ('regulation_90' if sport == 'football' else 'including_ot'):
            raise ValueError('unsupported settlement scope')
        odds = _decimal(option['original_odds'])
        if odds <= 0 or (sport == 'football' and odds < Decimal('1.33')):
            raise ValueError('invalid frozen price')
        line = option['line']
        if market in {'HHAD','HDC','HILO'}:
            value = _decimal(line)
            if market == 'HHAD' and value != value.to_integral_value():
                raise ValueError('HHAD requires integer home line')
        elif line is not None:
            raise ValueError('unexpected line for straight result market')
        record = {'option_id':ident,'market':market,'selection':option['selection'],
                  'line':line,'original_odds':option['original_odds']}
        if option.get('market_contract') is not None:
            record['market_contract'] = deepcopy(option['market_contract'])
        if leg_id not in grouped:
            grouped[leg_id] = {'leg_id':leg_id,'identity':deepcopy(identity),'scope':scope,'options':[]}
        leg = grouped[leg_id]
        if leg['identity'] != identity or leg['scope'] != scope:
            raise ValueError('same leg has inconsistent identity or scope')
        leg['options'].append(record)
    if len(grouped) != 2:
        raise ValueError('exactly two legs required')
    legs = [grouped[key] for key in sorted(grouped)]
    if legs[0]['identity']['business_day'] != legs[1]['identity']['business_day']:
        raise ValueError('business day mismatch')
    for leg in legs:
        leg['options'].sort(key=lambda item:item['option_id'])
    stake = snapshot['equal_stake']
    total = snapshot['total_investment']
    combinations = product(*(leg['options'] for leg in legs))
    ticket = {'legs':legs,'equal_stake':stake,'total_investment':total,
              'branches':[{'branch_id':f'branch-{i}', 'option_ids':[a['option_id'],b['option_id']]}
                          for i,(a,b) in enumerate(combinations)]}
    frozen_ticket({'settlement_ticket':ticket})
    return ticket


def prepare_snapshot(snapshot):
    prepared = deepcopy(snapshot)
    generated = generate_ticket(prepared)
    if 'settlement_ticket' in prepared and canonical(prepared['settlement_ticket']) != canonical(generated):
        raise ValueError('submitted settlement structure differs from generated original')
    prepared['settlement_ticket'] = generated
    return prepared


def validate_snapshot(snapshot):
    if 'settlement_ticket' not in snapshot:
        raise ValueError('settlement structure required before review/publication')
    prepare_snapshot(snapshot)
