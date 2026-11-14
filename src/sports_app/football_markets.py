"""Explicit frozen football selection encodings, no inferred official catalog."""
import re

HAFU = frozenset(a+b for a in 'HDA' for b in 'HDA')


def exact_score(value):
    if not isinstance(value,str) or re.fullmatch(r'(0|[1-9][0-9]*):(0|[1-9][0-9]*)',value) is None:
        raise ValueError('score must be canonical home:away')
    return tuple(map(int,value.split(':')))


def validate_football_selection(market, selection, contract=None):
    if market == 'TTG':
        if selection not in {str(i) for i in range(7)} | {'7+'}:
            raise ValueError('TTG selection must be 0..6 or 7+')
    elif market == 'HAFU':
        if selection not in HAFU:
            raise ValueError('HAFU uses explicit HT/FT H,D,A pair')
    elif market == 'CRS':
        if selection in {'H_OTHER','D_OTHER','A_OTHER'}:
            if (not isinstance(contract,dict) or contract.get('kind') != 'crs_other_complement_v1'
                    or not isinstance(contract.get('source_id'),str) or not contract['source_id'].strip()
                    or contract.get('orientation') != 'H:A'
                    or contract.get('scope') != 'regulation_90'
                    or contract.get('other_codes') != ['H_OTHER','D_OTHER','A_OTHER']):
                raise ValueError('explicit complete official CRS complement contract required')
            listed = contract.get('listed_scores')
            if not isinstance(listed,list) or not listed:
                raise ValueError('complete listed exact scores required')
            # Validate type and canonical format before any hashing/deduplication.
            for value in listed:
                exact_score(value)
            if len(listed) != len(set(listed)):
                raise ValueError('duplicate listed exact scores')
        else:
            exact_score(selection)
