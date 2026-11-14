"""Explicitly injected local publication chain. No default real publishing.

Only server task/batch/candidate references cross the request boundary. The
configured policy must use the same runtime/store and revalidate on review and
confirmation. No route accepts raw snapshots, scores, or approval flags.
"""
from contextlib import closing
import json
import sqlite3
from uuid import uuid4
from flask import Blueprint,g,jsonify,request
from werkzeug.exceptions import HTTPException
from .auth import require
from .publication import PublicationService,fingerprint,canonical
from .evidence_store import RepositoryReviewGate


class LocalPublicationChain:
    def __init__(self,runtime):
        self.runtime=runtime;self.policy=runtime.policy;self.store=runtime.store
        self.policy.decision_loader=runtime.load_execution
        self.service=PublicationService(self.store,trusted_gate=RepositoryReviewGate(
            self.policy.evidence,additional_gate=self.policy))

    def check(self):
        if self.runtime._executor is None:raise NotImplementedError('executor unavailable')
        if self.runtime.assembly is not None:self.runtime.assembly.assert_approved()

    def draft(self,principal,csrf,*,draft_id,batch_id,candidate_id,task_id):
        require(principal,{'admin'},csrf);self.check()
        from .publication_ticket import prepare_snapshot
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            expected=self.policy.final_snapshot(batch_id,candidate_id,task_id)
            old=db.execute('SELECT * FROM publication_drafts WHERE draft_id=?',(draft_id,)).fetchone()
            if old:
                frozen=json.loads(old['snapshot_json'])
                stripped={k:v for k,v in frozen.items() if k!='recommendation_freeze_id'}
                if canonical(stripped)!=canonical(expected):raise ValueError('draft source drift')
                return old['fingerprint']
            freeze_id='publication-freeze-'+draft_id
            frozen=prepare_snapshot(dict(expected,recommendation_freeze_id=freeze_id))
            frozen_body={'snapshot_hash':fingerprint(expected),
                         'recommendation_type':expected.get('recommendation_type'),
                         'recommendation_status':expected.get('recommendation_status')}
            self.policy.evidence._append(db,freeze_id,'recommendation','',frozen_body,principal.actor_id)
            digest=fingerprint(frozen)
            db.execute('INSERT INTO publication_drafts VALUES(?,?,?)',(draft_id,canonical(frozen),digest))
        return digest

    def audit(self,principal,csrf,*,draft_id,review_id):
        from .publication import now
        require(principal,{'admin','reviewer'},csrf);self.check()
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            snapshot,digest=self.service._draft(db,draft_id)
            self.service._validate(snapshot)
            old=db.execute('SELECT * FROM publication_reviews WHERE review_id=?',(review_id,)).fetchone()
            if old:
                if old['draft_id']!=draft_id or old['fingerprint']!=digest:
                    raise ValueError('review key conflict')
            else:
                db.execute('INSERT INTO publication_reviews VALUES(?,?,?,?,?)',
                           (review_id,draft_id,digest,principal.actor_id,now()))
        return digest


def register_publication_http(app,chain):
    bp=Blueprint('publication_api',__name__)

    def error(code,status,message='发布请求未完成',category='HARD_BLOCK'):
        return jsonify(code=code,category=category,message=message,request_id=uuid4().hex),status

    def authorize(roles,write=False):
        if write:require(g.principal,roles,request.headers.get('X-CSRF'))
        elif g.principal.role not in roles:raise PermissionError()

    def ready():
        if chain is None:raise NotImplementedError()
        chain.check()

    def body(required,optional=frozenset()):
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
            return None
        return value

    def nonempty(value):return isinstance(value,str) and bool(value.strip())

    @bp.errorhandler(PermissionError)
    def denied(exc):return error('FORBIDDEN',403,'权限、CSRF或发布门禁未通过')
    @bp.errorhandler(NotImplementedError)
    def unavailable(exc):return error('NOT_IMPLEMENTED',501,'可信发布运行链未配置','EXECUTION_FAILURE')
    @bp.errorhandler(ValueError)
    def conflict(exc):return error('PUBLICATION_CONFLICT',409,'发布状态、版本或输入校验失败')
    @bp.errorhandler(sqlite3.IntegrityError)
    def integrity(exc):return error('PUBLICATION_CONFLICT',409,'发布标识或活动槽位冲突')
    @bp.errorhandler(Exception)
    def internal(exc):
        if isinstance(exc,HTTPException):return error('HTTP_ERROR',exc.code or 500,'请求无效')
        return error('INTERNAL_ERROR',500,'发布服务内部错误','EXECUTION_FAILURE')

    @bp.post('/api/publication-drafts')
    def create_draft():
        authorize({'admin'},True);ready()
        value=body({'batch_id','candidate_id','task_id'})
        key=request.headers.get('Idempotency-Key')
        if value is None or not all(nonempty(v) for v in value.values()) or not nonempty(key):
            return error('INVALID_REQUEST',400,'仅接受服务器批次、候选、任务ID及幂等键')
        draft_id='draft-'+fingerprint(['publication.draft',g.principal.actor_id,key])
        digest=chain.draft(g.principal,request.headers.get('X-CSRF'),draft_id=draft_id,**value)
        return jsonify(data={'draft_id':draft_id,'fingerprint':digest,'status':'draft'}),201

    @bp.post('/api/publication-drafts/<draft_id>/audit')
    def audit(draft_id):
        authorize({'admin','reviewer'},True);ready()
        value=body(set());key=request.headers.get('Idempotency-Key')
        if value is None or not nonempty(key):return error('INVALID_REQUEST',400,'需要空对象与幂等键')
        review_id='review-'+fingerprint(['publication.audit',g.principal.actor_id,key])
        digest=chain.audit(g.principal,request.headers.get('X-CSRF'),draft_id=draft_id,review_id=review_id)
        return jsonify(data={'review_id':review_id,'draft_id':draft_id,'fingerprint':digest,'status':'audited'})

    @bp.post('/api/publications/<draft_id>/confirm')
    def confirm(draft_id):
        authorize({'admin'},True);ready()
        value=body({'review_id','human_confirmed'},{'replace_id'})
        key=request.headers.get('Idempotency-Key')
        if (value is None or value.get('human_confirmed') is not True or not nonempty(value.get('review_id'))
                or not nonempty(key) or ('replace_id' in value and not nonempty(value['replace_id']))):
            return error('INVALID_CONFIRMATION',400,'需要审核ID、明确人工确认和幂等键')
        publication_id='publication-'+fingerprint(['publication.confirm',g.principal.actor_id,key])
        result=chain.service.confirm(g.principal,request.headers.get('X-CSRF'),draft_id=draft_id,
            review_id=value['review_id'],publication_id=publication_id,request_id=key,
            human_confirmed=True,replace_id=value.get('replace_id'))
        return jsonify(data={'publication_id':result,'status':'published'})

    def view(row):
        original=json.loads(row['original_json'])
        value={'publication_id':row['publication_id'],'status':row['status'],'slot_key':row['slot_key'],
                'created_at':row['created_at'],'selection_hash':row['selection_hash'],
                'manifest_hash':row['manifest_hash'],'settlement_ticket':original.get('settlement_ticket'),
                'recommendation_status':original.get('recommendation_status'),
                'assessment_summary':original.get('assessment_summary'), 'model_probability':None,'EV':None}
        if g.principal.role=='viewer':
            from .public_projection import viewer_manifest
            value.update(viewer_manifest(row['manifest_hash']))
        return value

    @bp.get('/api/publications')
    def ledger():
        authorize({'admin','reviewer','viewer'})
        if chain is None:raise NotImplementedError()
        try:limit=int(request.args.get('limit','20'));offset=int(request.args.get('offset','0'))
        except ValueError:return error('INVALID_PAGINATION',400)
        if not 1<=limit<=100 or offset<0:return error('INVALID_PAGINATION',400)
        where="WHERE status IN ('published','frozen')" if g.principal.role=='viewer' else ''
        with closing(chain.store.connect()) as db:
            rows=list(db.execute('SELECT * FROM publications '+where+' ORDER BY publication_id LIMIT ? OFFSET ?',(limit,offset)))
        return jsonify(data=[view(row) for row in rows],limit=limit,offset=offset)

    @bp.get('/api/publications/<publication_id>')
    def get(publication_id):
        authorize({'admin','reviewer','viewer'})
        if chain is None:raise NotImplementedError()
        with closing(chain.store.connect()) as db:
            row=db.execute('SELECT * FROM publications WHERE publication_id=?',(publication_id,)).fetchone()
        if row is None or (g.principal.role=='viewer' and row['status'] not in {'published','frozen'}):
            return error('NOT_FOUND',404,'发布记录不存在或不可见')
        return jsonify(data=view(row))

    app.register_blueprint(bp)
