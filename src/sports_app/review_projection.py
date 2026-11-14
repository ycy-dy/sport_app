"""Strict whitelist for review summaries. Never passes arbitrary JSON leaves."""
from .publication import canonical


def text(value,nullable=False):
    if value is None and nullable:return None
    if not isinstance(value,str):raise ValueError('invalid public review text')
    return value


def outcome(value):
    if not isinstance(value,str) or value not in {'hit','miss','pending'}:
        raise ValueError('invalid review outcome')
    return value


def array(value):
    if not isinstance(value,list):raise ValueError('invalid review array')
    return value


def safe_summary(summary):
    if not isinstance(summary,dict):raise ValueError('invalid review summary')
    legs=[];branches=[];options={};pending=[]
    for row in array(summary.get('legs',[])):
        ident=text(row['leg_id']);state=outcome(row['outcome'])
        legs.append({'leg_id':ident,'outcome':state})
        if state=='pending':pending.append({'level':'leg','id':ident,'code':'UNRESOLVED_OPTION','reason':None})
    for row in array(summary.get('branches',[])):
        ident=text(row['branch_id']);state=outcome(row['outcome'])
        branches.append({'branch_id':ident,'outcome':state,
                         'option_ids':[text(v) for v in array(row['option_ids'])],
                         'original_odds':[text(v) for v in array(row['original_odds'])],
                         'equal_stake':text(row['equal_stake']),'total_investment':text(row['total_investment'])})
        if state=='pending':pending.append({'level':'branch','id':ident,'code':'UNRESOLVED_LEG','reason':None})
    if not isinstance(summary.get('options',{}),dict):raise ValueError('invalid option summary')
    for key,row in summary.get('options',{}).items():
        ident=text(key);state=outcome(row['outcome'])
        # Validate old reason type but expose only fixed classified explanation.
        text(row.get('reason'),nullable=True)
        options[ident]={'option_id':ident,'outcome':state,
                        'reason':'result_requires_reconciliation' if state=='pending' else None}
        if state=='pending':pending.append({'level':'option','id':ident,'code':'RESULT_REQUIRES_RECONCILIATION',
                                            'reason':'结果或结算依据待核'})
    return {'outcome':outcome(summary.get('outcome','pending')),'legs':legs,'branches':branches,
            'options':options,'pending_reasons':pending}


def validate_response(value):
    """Final recursive shape defence for the constructed response."""
    text(value['publication_id']);text(value['recommendation_status'],nullable=True)
    text(value['leg_statistics_coverage'])
    if value['version'] is not None and (type(value['version']) is not int or value['version']<1):raise ValueError('version')
    for match in array(value['matches']):
        text(match['leg_id'])
        if not isinstance(match['identity'],dict):raise ValueError('identity')
        for item in match['identity'].values():text(item,nullable=True)
        text(match['orientation'],nullable=True)
        for key in ('home_score','away_score'):
            if match[key] is not None and (type(match[key]) is not int or match[key]<0):raise ValueError('score')
        for key in ('quarters','overtime'):
            for pair in array(match[key]):
                if not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int or v<0 for v in pair):raise ValueError('period')
    canonical(value)  # ensure no nonfinite/nonserializable data
    return value
