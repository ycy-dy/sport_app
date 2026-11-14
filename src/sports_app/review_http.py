"""Manual review requests/comments and admin-only deterministic settlement HTTP."""
from contextlib import closing
from dataclasses import fields
import json
from uuid import uuid4
import sqlite3
from flask import Blueprint,g,jsonify,request
from werkzeug.exceptions import HTTPException
from .auth import require
from .publication import canonical,fingerprint
from .settlement import Identity,ResultSnapshot,statistics
from functools import wraps


class ReviewNotFound(Exception):
    """Explicit missing/invisible publication, never a malformed JSON KeyError."""


class ReviewProjectionFailure(Exception):
    """Stored data cannot safely be represented; not a business conflict."""


def projection_boundary(function):
    @wraps(function)
    def guarded(*args,**kwargs):
        try:
            return function(*args,**kwargs)
        except Exception as exc:
            raise ReviewProjectionFailure() from exc
    return guarded

SCHEMA="""
CREATE TABLE IF NOT EXISTS review_actions (
 action_id TEXT PRIMARY KEY, scope_key TEXT UNIQUE NOT NULL, body_hash TEXT NOT NULL,
 publication_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS review_action_no_update BEFORE UPDATE ON review_actions
 BEGIN SELECT RAISE(ABORT,'immutable review action'); END;
CREATE TRIGGER IF NOT EXISTS review_action_no_delete BEFORE DELETE ON review_actions
 BEGIN SELECT RAISE(ABORT,'immutable review action'); END;
"""


def parse_results(value):
    if not isinstance(value,dict):raise TypeError('invalid results')
    answer={}
    allowed={f.name for f in fields(ResultSnapshot)}
    for key,row in value.items():
        if not isinstance(key,str) or not isinstance(row,dict) or set(row)-allowed:raise TypeError('invalid result')
        body=dict(row);body['identity']=Identity(**body['identity'])
        if body.get('half_identity') is not None:body['half_identity']=Identity(**body['half_identity'])
        if 'periods' in body:body['periods']=tuple(tuple(p) for p in body['periods'])
        answer[key]=ResultSnapshot(**body)
    return answer


def register_review_http(app,service):
    bp=Blueprint('review_write_api',__name__)
    if service is not None:
        with closing(service.store.connect()) as db,db:db.executescript(SCHEMA)

    def error(code,status):
        return jsonify(code=code,category='EXECUTION_FAILURE' if status>=500 else 'HARD_BLOCK',
                       message='复盘请求未完成',request_id=uuid4().hex),status
    def auth(roles,write=False):
        if write:require(g.principal,roles,request.headers.get('X-CSRF'))
        elif g.principal.role not in roles:raise PermissionError()
    def ready():
        if service is None:raise NotImplementedError()
    def visible(ident):
        with closing(service.store.connect()) as db:
            row=db.execute('SELECT * FROM publications WHERE publication_id=?',(ident,)).fetchone()
        if row is None or (g.principal.role=='viewer' and row['status'] not in {'published','frozen'}):raise ReviewNotFound()
        return row
    def body(required):
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or set(value)!=set(required):raise TypeError()
        return value
    def paging():
        try:limit=int(request.args.get('limit','20'));offset=int(request.args.get('offset','0'))
        except ValueError:raise TypeError()
        if not 1<=limit<=100 or offset<0:raise TypeError()
        return limit,offset

    @bp.errorhandler(PermissionError)
    def forbidden(exc):return error('FORBIDDEN',403)
    @bp.errorhandler(ReviewProjectionFailure)
    def projection_failed(exc):return error('PROJECTION_FAILED',500)
    @bp.errorhandler(ReviewNotFound)
    def missing(exc):return error('NOT_FOUND',404)
    @bp.errorhandler(NotImplementedError)
    def missing_service(exc):return error('NOT_IMPLEMENTED',501)
    @bp.errorhandler(TypeError)
    def invalid(exc):return error('INVALID_REQUEST',400)
    @bp.errorhandler(ValueError)
    @bp.errorhandler(sqlite3.IntegrityError)
    def conflict(exc):return error('REVIEW_CONFLICT',409)
    @bp.errorhandler(Exception)
    def internal(exc):return error('HTTP_ERROR',exc.code or 500) if isinstance(exc,HTTPException) else error('INTERNAL_ERROR',500)

    def action(ident,kind,value):
        key=request.headers.get('Idempotency-Key')
        if not isinstance(key,str) or not key.strip():raise TypeError()
        scope=fingerprint(['reviews.'+kind,g.principal.actor_id,key]);digest=fingerprint(value)
        with closing(service.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM publications WHERE publication_id=?',(ident,)).fetchone() is None:raise ReviewNotFound()
            old=db.execute('SELECT * FROM review_actions WHERE scope_key=?',(scope,)).fetchone()
            if old:
                if old['body_hash']!=digest or old['publication_id']!=ident:raise ValueError()
                return old['action_id']
            ident_action='review-action-'+scope
            db.execute('INSERT INTO review_actions VALUES(?,?,?,?,?,?)',
                       (ident_action,scope,digest,ident,kind,canonical(value)))
        return ident_action

    @bp.post('/api/reviews')
    def start():
        auth({'admin','reviewer'},True);ready();value=body({'publication_id'})
        ident=value['publication_id']
        if not isinstance(ident,str) or not ident.strip():raise TypeError()
        result=action(ident,'initiate',value)
        return jsonify(data={'review_request_id':result,'publication_id':ident,'status':'requested'}),201

    @bp.post('/api/reviews/<ident>/comments')
    def comment(ident):
        auth({'admin','reviewer'},True);ready();value=body({'comment'})
        if not isinstance(value['comment'],str) or not value['comment'].strip():raise TypeError()
        result=action(ident,'comment',value)
        return jsonify(data={'comment_id':result,'publication_id':ident,'status':'recorded'}),201

    @bp.get('/api/reviews/<ident>/comments')
    def comments(ident):
        auth({'admin','reviewer'});ready();visible(ident);limit,offset=paging()
        with closing(service.store.connect()) as db:
            total=db.execute("SELECT COUNT(*) FROM review_actions WHERE publication_id=? AND kind='comment'",(ident,)).fetchone()[0]
            rows=list(db.execute("SELECT action_id,body FROM review_actions WHERE publication_id=? AND kind='comment' ORDER BY action_id LIMIT ? OFFSET ?",(ident,limit,offset)))
        # Opinion prose is untrusted internal input: disclose only existence/ID,
        # never arbitrary body text that can embed sources, credentials or logs.
        return jsonify(data=[{'comment_id':row['action_id'],'publication_id':ident,
                              'status':'recorded','content':None,'content_visibility':'redacted'} for row in rows],
                       meta={'total':total,'limit':limit,'offset':offset})

    @bp.post('/api/reviews/<ident>/settle')
    @bp.post('/api/reviews/<ident>/corrections')
    def settle(ident):
        auth({'admin'},True);ready();visible(ident)
        correction=request.path.endswith('/corrections')
        value=body({'results','correction_reason'} if correction else {'results'})
        if correction and (not isinstance(value['correction_reason'],str) or not value['correction_reason'].strip()):raise TypeError()
        results=parse_results(value['results'])
        summary=service.record(g.principal,request.headers.get('X-CSRF'),publication_id=ident,
                 results=results,correction_reason=value.get('correction_reason',''),mode='correction' if correction else 'initial')
        # Re-read the exact committed version and use the same projection as history.
        with closing(service.store.connect()) as db:
            version=db.execute('SELECT * FROM published_settlement_versions WHERE publication_id=? AND version=?',
                               (ident,summary['version'])).fetchone()
        return jsonify(data=projection(visible(ident),version))

    @projection_boundary
    def projection(row,version):
        from .review_projection import safe_summary,validate_response
        summary=safe_summary(json.loads(version['summary_json']) if version else {})
        original=json.loads(row['original_json'])
        matches=[]
        raw=json.loads(version['result_json']) if version else {}
        for leg in original.get('settlement_ticket',{}).get('legs',[]):
            result=raw.get(leg['leg_id']);identity=leg['identity']
            item={'leg_id':leg['leg_id'],'identity':{k:identity.get(k) for k in ('sport','league','business_day','match_id','kickoff','home_id','away_id')},
                  'orientation':None,'home_score':None,'away_score':None,'quarters':[],'overtime':[]}
            if result:
                direction=result.get('orientation');score=result.get('scores')
                if direction in {'H:A','A:H'} and isinstance(score,list) and len(score)==2 and all(type(v) is int and v>=0 for v in score):
                    item.update(orientation='H:A',home_score=score[0 if direction=='H:A' else 1],away_score=score[1 if direction=='H:A' else 0])
                periods=result.get('periods',[])
                if isinstance(periods,list) and all(isinstance(p,list) and len(p)==2 and all(type(v) is int and v>=0 for v in p) for p in periods):
                    normalized=[p if direction=='H:A' else p[::-1] for p in periods] if direction in {'H:A','A:H'} else []
                    item['quarters']=normalized[:4];item['overtime']=normalized[4:]
            matches.append(item)
        from .published_settlement import frozen_ticket
        try:
            frozen_ticket(original)
            coverage='complete'
        except (KeyError,ValueError,TypeError):coverage='insufficient_frozen_structure'
        return validate_response({'publication_id':row['publication_id'],'leg_statistics_coverage':coverage,
                'version':version['version'] if version else None,
                'outcome':summary.get('outcome','pending'),'recommendation_status':original.get('recommendation_status'),
                'legs':summary.get('legs',[]),'branches':summary.get('branches',[]),
                'options':summary['options'],'matches':matches,'pending_reasons':summary['pending_reasons'],
                'actual_stake':None,'actual_return':None,'model_probability':None,'EV':None})

    @bp.get('/api/reviews/<ident>')
    def detail(ident):
        auth({'admin','reviewer','viewer'});ready();row=visible(ident)
        with closing(service.store.connect()) as db:
            version=db.execute('SELECT * FROM published_settlement_versions WHERE publication_id=? ORDER BY version DESC LIMIT 1',(ident,)).fetchone()
        return jsonify(data=projection(row,version))

    @bp.get('/api/reviews/<ident>/history')
    def history(ident):
        auth({'admin','reviewer','viewer'});ready();row=visible(ident);limit,offset=paging()
        with closing(service.store.connect()) as db:
            total=db.execute('SELECT COUNT(*) FROM published_settlement_versions WHERE publication_id=?',(ident,)).fetchone()[0]
            versions=list(db.execute('SELECT * FROM published_settlement_versions WHERE publication_id=? ORDER BY version LIMIT ? OFFSET ?',(ident,limit,offset)))
        return jsonify(data=[projection(row,v) for v in versions],meta={'total':total,'limit':limit,'offset':offset})

    @bp.get('/api/review-statistics')
    def stats():
        auth({'admin','reviewer','viewer'});ready()
        rows=service.ledger()
        if g.principal.role=='viewer':rows=[r for r in rows if r['publication_status'] in {'published','frozen'}]
        missing=sum(row['leg_statistics_coverage']!='complete' for row in rows)
        return jsonify(data=dict(statistics(rows),tickets_missing_leg_structure=missing,
            leg_statistics_coverage='incomplete' if missing else 'complete',
            scope='visible_publications' if g.principal.role=='viewer' else 'all_publications',model_probability=None,EV=None))

    app.register_blueprint(bp)
