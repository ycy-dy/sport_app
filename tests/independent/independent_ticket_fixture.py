"""Independent synthetic research input, no author fixture or production data."""
from copy import deepcopy


def research_snapshot(slot='slot-1'):
    options = []
    for index in (1, 2):
        options.append(dict(option_id=f'o{index}', assessment_id=f'a{index}', leg_id=f'l{index}',
            identity=dict(sport='football', league='independent-league', business_day='2026-11-14',
                          match_id=f'm{index}', kickoff='2026-11-14T18:00:00+08:00',
                          home_id=f'h{index}', away_id=f'away{index}'),
            market='HAD', selection='H', line=None, scope='regulation_90', original_odds='4',
            dimensions=[(f'{i:02d}', 'verified') for i in range(1,24)],
            reasons=['synthetic reason 1','synthetic reason 2'],
            failure_paths=['synthetic failure 1','synthetic failure 2'],
            withdraw_if=['synthetic withdrawal'], claim_ids=['c1','c2']))
    return deepcopy(dict(slot_key=slot,selection_hash='synthetic-selection',manifest_hash='synthetic-manifest',
                         model_probability=None,EV=None,equal_stake='1',total_investment='1',options=options))
