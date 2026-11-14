"""Authenticated manual task HTTP; synchronous explicit execute, no fake queue."""
from contextlib import closing
import json
import sqlite3
from uuid import uuid4
from datetime import datetime
from werkzeug.exceptions import HTTPException
from flask import Blueprint,g,jsonify,request
from .auth import require
from .publication import fingerprint


def register_task_http(app,runtime):
    bp=Blueprint('manual_task_api',__name__)
    if runtime is not None:
        with closing(runtime.store.connect()) as db,db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS task_http_requests (
 scope_key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, task_id TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS task_http_no_update BEFORE UPDATE ON task_http_requests
 BEGIN SELECT RAISE(ABORT,'immutable task request'); END;
CREATE TRIGGER IF NOT EXISTS task_http_no_delete BEFORE DELETE ON task_http_requests
 BEGIN SELECT RAISE(ABORT,'immutable task request'); END;
''')

    def error(code,message,status,category='HARD_BLOCK'):
        return jsonify(category=category,code=code,message=message,request_id=uuid4().hex),status

    def auth(write=False):
        if write:
            require(g.principal,{'admin'},request.headers.get('X-CSRF'))
        elif g.principal.role not in {'admin','reviewer'}:
            raise PermissionError('无权读取管理任务')

    def unavailable():
        return runtime is None or runtime._executor is None

    def detail(task_id,events=False):
        with closing(runtime.store.connect()) as db:
            task=db.execute('SELECT * FROM manual_tasks WHERE task_id=?',(task_id,)).fetchone()
            rows=list(db.execute('SELECT * FROM task_transitions WHERE task_id=? ORDER BY version',(task_id,)))
        if task is None or not rows:raise LookupError('task not found')
        latest=rows[-1];payload=json.loads(latest['payload'])
        value={'task_id':task_id,'input_id':task['input_id'],'status':latest['state'],
               'stage':'executor_return_recorded_not_decision_validated' if latest['state']=='completed' else latest['state'],
               'deadline':task['deadline'],'error':{'code':'EXECUTOR_FAILURE'} if 'error_type' in payload else None,
               'model_probability':None,'EV':None}
        if events:
            value['events']=[{'version':row['version'],'status':row['state'],'recorded_at':row['recorded_at']} for row in rows]
        return value

    @bp.errorhandler(PermissionError)
    def forbidden(exc):return error('FORBIDDEN','角色权限或CSRF校验失败',403)
    @bp.errorhandler(LookupError)
    def missing(exc):return error('NOT_FOUND','任务不存在',404)
    @bp.errorhandler(ValueError)
    def invalid(exc):
        # Only exact known domain messages classify state conflicts. Never echo
        # exception text; injected/internal ValueErrors are server failures.
        known={'expired task','approved rules missing','rules drift','batch not found',
               'batch no longer current; refresh upstream','task not executable',
               'input corruption','task rule registry missing','task input/rules/batch mismatch',
               'local policy/schema/code drift or unapproved revision'}
        if str(exc) in known:return error('TASK_STATE_CONFLICT','任务状态或已批准输入不允许该操作',409)
        return error('INTERNAL_ERROR','任务服务内部错误',500,'EXECUTION_FAILURE')

    @bp.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc,HTTPException):
            return error('HTTP_ERROR','请求无效',exc.code or 500)
        return error('INTERNAL_ERROR','任务服务内部错误',500,'EXECUTION_FAILURE')
    @bp.errorhandler(sqlite3.IntegrityError)
    def conflict(exc):return error('TASK_CONFLICT','任务、输入或幂等键冲突',409)

    @bp.post('/api/analysis-runs')
    def create():
        auth(True)
        if unavailable():return error('NOT_IMPLEMENTED','手动执行器未配置',501,'EXECUTION_FAILURE')
        body=request.get_json(silent=True)
        keys={'registry_id','batch_id','deadline'}
        if not isinstance(body,dict) or set(body)!=keys or any(not isinstance(v,str) or not v.strip() for v in body.values()):
            return error('INVALID_REQUEST','仅允许registry_id、batch_id、deadline非空字段',400)
        try:
            deadline=datetime.fromisoformat(body['deadline'])
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise ValueError('timezone absent')
        except (ValueError,TypeError,OverflowError):
            return error('INVALID_DEADLINE','deadline必须是含时区的有效ISO时间',400)
        key=request.headers.get('Idempotency-Key')
        if not key or not key.strip():return error('IDEMPOTENCY_REQUIRED','需要非空幂等键',400)
        scope=fingerprint(['analysis-runs.create',g.principal.actor_id,key])
        digest=fingerprint(body)
        with closing(runtime.store.connect()) as db:
            old=db.execute('SELECT * FROM task_http_requests WHERE scope_key=?',(scope,)).fetchone()
        if old:
            if old['body_hash']!=digest:return error('IDEMPOTENCY_CONFLICT','同键请求内容不一致',409)
            return jsonify(data=detail(old['task_id'])),200
        task_id='run-'+scope;input_id='input-'+scope
        try:
            runtime.create(g.principal,request.headers.get('X-CSRF'),task_id=task_id,input_id=input_id,
                           **body,http_request=(scope,digest))
        except sqlite3.IntegrityError:
            with closing(runtime.store.connect()) as db:
                old=db.execute('SELECT * FROM task_http_requests WHERE scope_key=?',(scope,)).fetchone()
            if old and old['body_hash']==digest:return jsonify(data=detail(old['task_id'])),200
            raise
        return jsonify(data=detail(task_id)),201

    @bp.get('/api/analysis-runs')
    def listing():
        auth()
        if runtime is None:return error('NOT_IMPLEMENTED','任务仓储未配置',501,'EXECUTION_FAILURE')
        try:
            limit=int(request.args.get('limit','20'));offset=int(request.args.get('offset','0'))
        except ValueError:return error('INVALID_PAGINATION','分页参数无效',400)
        if not 1<=limit<=100 or offset<0:return error('INVALID_PAGINATION','分页参数越界',400)
        with closing(runtime.store.connect()) as db:
            rows=list(db.execute('SELECT task_id FROM manual_tasks ORDER BY task_id LIMIT ? OFFSET ?',(limit,offset)))
        return jsonify(data=[detail(row['task_id']) for row in rows],limit=limit,offset=offset)

    @bp.get('/api/analysis-runs/<task_id>')
    @bp.get('/api/analysis-runs/<task_id>/events')
    def get(task_id):
        auth()
        if runtime is None:return error('NOT_IMPLEMENTED','任务仓储未配置',501,'EXECUTION_FAILURE')
        return jsonify(data=detail(task_id,events=request.path.endswith('/events')))

    @bp.post('/api/analysis-runs/<task_id>/cancel')
    def cancel(task_id):
        auth(True)
        if runtime is None:return error('NOT_IMPLEMENTED','任务仓储未配置',501,'EXECUTION_FAILURE')
        detail(task_id)
        runtime.cancel(g.principal,request.headers.get('X-CSRF'),task_id)
        return jsonify(data=detail(task_id))

    @bp.post('/api/analysis-runs/<task_id>/execute')
    def execute(task_id):
        auth(True)
        if unavailable():return error('NOT_IMPLEMENTED','手动执行器未配置',501,'EXECUTION_FAILURE')
        value=detail(task_id)
        if value['status']!='created':return jsonify(data=value),200
        runtime.execute(g.principal,request.headers.get('X-CSRF'),task_id)
        return jsonify(data=detail(task_id))

    app.register_blueprint(bp)
