"""Seven read-only views, explicit public projections and snapshot health.

No reads of task payloads, source URLs, draft tables or raw rule documents.
Feed snapshots are internal admin-approved projections, NOT fetched data. No
HTTP write endpoint exists. Source unavailable is distinct from a verified
empty snapshot. Historical publication views still enforce viewer visibility.
"""
from contextlib import closing
from datetime import datetime,timezone
import json
from uuid import uuid4
from flask import Blueprint,g,jsonify,request
from werkzeug.exceptions import HTTPException
from .auth import require
from .publication import canonical

FEEDS={'schedules','analyses','analysis-system','recommendation-system'}
STATES={'ready','empty','failed'}
SCHEMA="""
CREATE TABLE IF NOT EXISTS public_view_snapshots (
 snapshot_id TEXT PRIMARY KEY, feed TEXT NOT NULL, status TEXT NOT NULL,
 as_of TEXT NOT NULL, valid_until TEXT NOT NULL, body TEXT NOT NULL,
 actor_id TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS public_view_no_update BEFORE UPDATE ON public_view_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable view snapshot'); END;
CREATE TRIGGER IF NOT EXISTS public_view_no_delete BEFORE DELETE ON public_view_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable view snapshot'); END;
"""
IDENTITY_KEYS=('sport','league','business_day','match_id','kickoff','home_id','away_id')


def timestamp(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('timezone required')
    return result


def project(feed,row):
    """Construct fields, never copy arbitrary nested caller objects."""
    def scalar(value):
        if value is None or type(value) in (str,int,bool):return value
        raise ValueError('public field must be scalar')
    keys={'schedules':('event_id','status'), 'analyses':('analysis_id','status','title'),
          'analysis-system':('version','status','title'),
          'recommendation-system':('version','status','title')}[feed]
    allowed={'schedules':{'scheduled','finished','pending'},
             'analyses':{'published','frozen','ready','pending','failed','completed','cancelled'},
             'analysis-system':{'ready','not_configured','not_integrated','partial','stale'},
             'recommendation-system':{'ready','not_configured','not_integrated','partial','stale'}}
    if row.get('status') not in allowed[feed]:
        raise ValueError('draft/internal status is not a public view')
    result={key:scalar(row[key]) for key in keys if key in row}
    if 'identity' in row:
        result['identity']={key:scalar(row['identity'][key]) for key in IDENTITY_KEYS if key in row['identity']}
    result.update(model_probability=None,EV=None)
    from .public_projection import enrich_feed
    return enrich_feed(feed,row,result)


class ReadViews:
    def __init__(self,store,*,clock=None):
        self.store=store;self.clock=clock or (lambda:datetime.now(timezone.utc))
        with closing(store.connect()) as db,db:db.executescript(SCHEMA)

    def record(self,principal,csrf,*,snapshot_id,feed,status,as_of,valid_until,rows):
        """Server integration boundary: only explicitly approved public rows."""
        require(principal,{'admin'},csrf)
        if feed not in FEEDS or status not in STATES or not isinstance(rows,list):
            raise ValueError('invalid view snapshot')
        if not isinstance(snapshot_id,str) or not snapshot_id.strip():raise ValueError('empty snapshot id')
        if not timestamp(as_of)<=self.clock()<timestamp(valid_until):raise ValueError('invalid snapshot time')
        if status in {'failed','empty'} and rows:raise ValueError('failed/empty snapshot cannot contain rows')
        if status=='ready' and not rows:raise ValueError('use explicit empty state')
        safe=[project(feed,row) for row in rows]
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO public_view_snapshots VALUES(?,?,?,?,?,?,?,?)',
                       (snapshot_id,feed,status,as_of,valid_until,canonical(safe),principal.actor_id,self.clock().isoformat()))

    def feed(self,name):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM public_view_snapshots WHERE feed=? ORDER BY recorded_at DESC,rowid DESC LIMIT 1',(name,)).fetchone()
        if row is None:return [],{'status':'not_configured','snapshot_id':None,'as_of':None}
        status=row['status']
        if status!='failed' and self.clock()>=timestamp(row['valid_until']):status='stale'
        return json.loads(row['body']),{'status':status,'snapshot_id':row['snapshot_id'],'as_of':row['as_of']}

    def publications(self,role,*,current=False,reviews=False):
        from .public_projection import fields, string
        with closing(self.store.connect()) as db:
            where=" WHERE status IN ('published','frozen')" if role=='viewer' or current else ''
            rows=list(db.execute('SELECT * FROM publications'+where+' ORDER BY publication_id'))
            has_reviews=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='published_settlement_versions'").fetchone()
            settlements={}
            if reviews and has_reviews:
                for item in db.execute('SELECT s.* FROM published_settlement_versions s WHERE s.version='
                                       '(SELECT MAX(v.version) FROM published_settlement_versions v WHERE v.publication_id=s.publication_id)'):
                    settlements[item['publication_id']]=item
            audit_rows=list(db.execute("SELECT payload FROM audit_events WHERE event='publication_confirmed' ORDER BY id"))
        visible_ids={row['publication_id'] for row in rows}
        replacements={}
        for audit in audit_rows:
            detail=json.loads(audit['payload'])
            if isinstance(detail,dict):
                ident=detail.get('publication_id');prior=detail.get('replaces')
                if isinstance(ident,str) and (prior is None or isinstance(prior,str)):
                    replacements[ident]=prior
        approved,_=self.feed('analyses')
        # Only explicitly approved public snapshots can provide research prose.
        public_by_id={row['publication_id']:row for row in approved
                      if isinstance(row,dict) and isinstance(row.get('publication_id'),str)}
        output=[]
        for row in rows:
            original=json.loads(row['original_json'])
            value={'publication_id':row['publication_id'],'status':row['status'],'created_at':row['created_at'],
                   'recommendation_status':original.get('recommendation_status'),
                   'selection_hash':row['selection_hash'],'manifest_hash':row['manifest_hash'],
                   'model_probability':None,'EV':None}
            extra=fields(public_by_id.get(row['publication_id'],{}),
                         ('risk','no_pair_reason','withdrawal_reason'),('reasons','withdraw_if'))
            value.update(extra)
            if role=='viewer':
                from .public_projection import viewer_manifest
                projected=viewer_manifest(row['manifest_hash'])
                value['manifest_hash']=projected['manifest_hash']
                value['field_coverage'].update(projected['field_coverage'])
            value['recommendation_type']=string(original.get('recommendation_type'))
            value['period']=None
            # Explicit frozen period only; do not guess from kickoff/slot text.
            value['period']=string(original.get('period'))
            from .public_projection import strings
            value['pass_types']=strings(original.get('pass_types'))
            value['frozen_at']=None
            prior=replacements.get(row['publication_id'])
            value['replaces_publication_id']=prior if prior in visible_ids else None
            value['field_coverage'].update({key:'available' if value[key] is not None else 'unknown'
                for key in ('recommendation_type','period','frozen_at','replaces_publication_id')})
            value['field_coverage']['pass_types']='available' if 'pass_types' in original else 'unknown'
            legs=[]
            for leg in original.get('settlement_ticket',{}).get('legs',[]):
                identity={key:leg['identity'][key] for key in IDENTITY_KEYS if key in leg.get('identity',{})}
                legs.append({'leg_id':leg['leg_id'],'identity':identity,'home_team':None,'away_team':None,
                             'field_coverage':{'home_team':'unknown','away_team':'unknown'},
                             'options':[{key:option.get(key) for key in ('option_id','market','selection','line','original_odds')}
                                        for option in leg.get('options',[])]})
            value['legs']=legs
            if reviews:
                item=settlements.get(row['publication_id'])
                summary=json.loads(item['summary_json']) if item else {}
                outcome=summary.get('outcome','pending')
                if outcome not in {'hit','miss','pending'}:raise ValueError('invalid settlement status')
                from .published_settlement import frozen_ticket
                try:
                    frozen_ticket(original)
                    coverage='complete'
                except (KeyError,ValueError,TypeError):
                    coverage='insufficient_frozen_structure'
                value.update(outcome=outcome,version=item['version'] if item else None,
                             leg_statistics_coverage=coverage)
            validate_publication_view(value)
            output.append(value)
        return output,{'status':'ready' if output else 'empty','snapshot_id':None,'as_of':None}


def validate_publication_view(value):
    """Validate every projected leaf; never serialize untyped historical fields."""
    def string(item,nullable=False):
        if nullable and item is None:return
        if not isinstance(item,str):raise ValueError('invalid public text field')
    for key in ('publication_id','status','created_at','selection_hash'):
        string(value[key])
    string(value['manifest_hash'],nullable=True)
    string(value['recommendation_status'],nullable=True)
    for key in ('recommendation_type','period','risk','no_pair_reason','withdrawal_reason','frozen_at','replaces_publication_id'):
        string(value[key],nullable=True)
    for key in ('pass_types','reasons','withdraw_if'):
        if not isinstance(value[key],list) or any(not isinstance(item,str) for item in value[key]):
            raise ValueError('invalid public text array')
    if value['model_probability'] is not None or value['EV'] is not None:
        raise ValueError('unaccepted probability')
    if not isinstance(value['legs'],list):raise ValueError('invalid public legs')
    for leg in value['legs']:
        if not isinstance(leg,dict) or set(leg)!={'leg_id','identity','options','home_team','away_team','field_coverage'}:
            raise ValueError('invalid public leg shape')
        string(leg['leg_id'])
        string(leg['home_team'],nullable=True);string(leg['away_team'],nullable=True)
        if not isinstance(leg['identity'],dict) or set(leg['identity'])-set(IDENTITY_KEYS):
            raise ValueError('invalid public identity')
        for item in leg['identity'].values():string(item)
        if not isinstance(leg['options'],list):raise ValueError('invalid public options')
        for option in leg['options']:
            if not isinstance(option,dict) or set(option)!={'option_id','market','selection','line','original_odds'}:
                raise ValueError('invalid public option shape')
            for key in ('option_id','market','selection','original_odds'):string(option[key])
            string(option['line'],nullable=True)
    if 'outcome' in value:
        string(value['outcome'])
        string(value['leg_statistics_coverage'])
        if value['version'] is not None and (type(value['version']) is not int or value['version']<1):
            raise ValueError('invalid public version')


def register_read_api(app,views):
    bp=Blueprint('public_read_api',__name__)
    routes={'/api/schedules':'schedules','/api/analyses':'analyses','/api/recommendations':'recommendations',
            '/api/ledger':'ledger','/api/reviews':'reviews','/api/systems/analysis':'analysis-system',
            '/api/systems/recommendation':'recommendation-system'}

    def error(code,status):
        return jsonify(code=code,category='EXECUTION_FAILURE' if status>=500 else 'HARD_BLOCK',
                       message='只读视图请求未完成',request_id=uuid4().hex,
                       retryable=status>=500,field=None,stage='projection'),status

    @bp.errorhandler(Exception)
    def internal(exc):
        if isinstance(exc,HTTPException):return error('HTTP_ERROR',exc.code or 500)
        return error('INTERNAL_ERROR',500)

    def read():
        if g.principal.role not in {'admin','reviewer','viewer'}:return error('FORBIDDEN',403)
        if set(request.args)-{'limit','offset','sport','business_day','status'}:return error('INVALID_FILTER',400)
        try:limit=int(request.args.get('limit','20'));offset=int(request.args.get('offset','0'))
        except ValueError:return error('INVALID_PAGINATION',400)
        if not 1<=limit<=100 or offset<0:return error('INVALID_PAGINATION',400)
        sport=request.args.get('sport');day=request.args.get('business_day');status=request.args.get('status')
        if sport is not None and sport not in {'football','basketball'}:return error('INVALID_FILTER',400)
        if day is not None:
            from datetime import date
            try:
                if date.fromisoformat(day).isoformat()!=day:raise ValueError()
            except ValueError:return error('INVALID_FILTER',400)
        if status is not None and status not in {'scheduled','finished','pending','hit','miss','published','frozen','withdrawn','superseded','ready','draft'}:
            return error('INVALID_FILTER',400)
        feed=routes[request.path]
        if views is None:
            rows=[];meta={'status':'not_configured','snapshot_id':None,'as_of':None}
        elif feed in FEEDS:rows,meta=views.feed(feed)
        else:rows,meta=views.publications(g.principal.role,current=feed=='recommendations',reviews=feed=='reviews')
        def matches(row):
            identities=[row['identity']] if 'identity' in row else [leg['identity'] for leg in row.get('legs',[])]
            identity_match=(sport is None and day is None) or any(
                (sport is None or identity.get('sport')==sport)
                and (day is None or identity.get('business_day')==day)
                for identity in identities)
            return identity_match and (status is None or row.get('outcome',row.get('status'))==status)
        filtered=[row for row in rows if matches(row)]
        return jsonify(data=filtered[offset:offset+limit],meta=dict(meta,total=len(filtered),limit=limit,offset=offset,
                       schema_version='read-v1',model_probability=None,EV=None))

    for path in routes:bp.add_url_rule(path,endpoint=routes[path],view_func=read,methods=['GET'])
    app.register_blueprint(bp)
