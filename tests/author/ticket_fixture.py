def synthetic_ticket_input():
    options=[]
    for i in (1,2):
        options.append({'option_id':f'o{i}','assessment_id':f'a{i}','leg_id':f'l{i}',
                        'identity':{'sport':'football','league':'synthetic','business_day':'2026-11-14',
                                    'match_id':f'm{i}','kickoff':'2026-11-14T18:00:00+08:00',
                                    'home_id':f'h{i}','away_id':f'a{i}'},
                        'scope':'regulation_90','market':'HAD','selection':'H','line':None,
                        'original_odds':'3'})
    return {'options':options,'equal_stake':'1','total_investment':'1'}
