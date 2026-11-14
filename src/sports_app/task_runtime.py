"""Manual injected executor, approved rules and append-only task transitions.

No subprocess, credentials, scheduler or network. Trust boundary is application
assembly: only the configured executor can supply completion payloads. This is
not cryptographic proof of a genuine Codex process; real adapter unconfigured.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from .auth import require
from .manifest import POLICY_FILES,manifest
from .publication import canonical,fingerprint
from .agent_input import build_agent_input

SCHEMA="""
CREATE TABLE IF NOT EXISTS approved_rules (
 registry_id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL, actor_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_tasks (
 task_id TEXT PRIMARY KEY, input_id TEXT NOT NULL, registry_id TEXT NOT NULL,
 deadline TEXT NOT NULL, actor_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_transitions (
 task_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
 payload TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(task_id,version)
);
CREATE TRIGGER IF NOT EXISTS rules_no_update BEFORE UPDATE ON approved_rules BEGIN SELECT RAISE(ABORT,'immutable rules'); END;
CREATE TRIGGER IF NOT EXISTS rules_no_delete BEFORE DELETE ON approved_rules BEGIN SELECT RAISE(ABORT,'immutable rules'); END;
CREATE TRIGGER IF NOT EXISTS tasks_no_update BEFORE UPDATE ON manual_tasks BEGIN SELECT RAISE(ABORT,'immutable task'); END;
CREATE TRIGGER IF NOT EXISTS tasks_no_delete BEFORE DELETE ON manual_tasks BEGIN SELECT RAISE(ABORT,'immutable task'); END;
CREATE TRIGGER IF NOT EXISTS transitions_no_update BEFORE UPDATE ON task_transitions BEGIN SELECT RAISE(ABORT,'immutable execution'); END;
CREATE TRIGGER IF NOT EXISTS transitions_no_delete BEFORE DELETE ON task_transitions BEGIN SELECT RAISE(ABORT,'immutable execution'); END;
"""


def instant(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None:raise ValueError('timezone required')
    return result


class ManualRuntime:
    def __init__(self,policy,*,executor=None,clock=None,assembly=None):
        self.policy=policy;self.store=policy.store;self._executor=executor
        self.assembly=assembly
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        with closing(self.store.connect()) as db,db:db.executescript(SCHEMA)

    def approve_rules(self,principal,csrf,*,registry_id,documents,policy_revision,analysis_revision,schema_hash,code_hash):
        require(principal,{'admin'},csrf)
        if set(documents)!=set(POLICY_FILES) or any(not isinstance(v,str) or not v.strip() for v in documents.values()):
            raise ValueError('five complete rule contents required')
        version=manifest(policy_revision=policy_revision,analysis_revision=analysis_revision,
                         schema_hash=schema_hash,code_hash=code_hash,codex_identity='codex',
                         rules={key:sha256(value.encode()).hexdigest() for key,value in documents.items()})
        if version!=self.policy.approved_manifest:raise ValueError('not approved deployment manifest')
        body={'documents':documents,'manifest':version}
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO approved_rules VALUES(?,?,?,?)',(registry_id,canonical(body),fingerprint(body),principal.actor_id))

    def create(self,principal,csrf,*,task_id,input_id,registry_id,batch_id,deadline):
        require(principal,{'admin'},csrf)
        if self._executor is None:raise ValueError('NOT_IMPLEMENTED: executor unconfigured')
        if self.assembly is not None:self.assembly.assert_approved()
        if not all(isinstance(v,str) and v.strip() for v in (task_id,input_id,registry_id)):
            raise ValueError('empty task identity')
        if instant(deadline)<=self.clock():raise ValueError('expired task')
        batch,_=self.policy._load(batch_id)
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM approved_rules WHERE registry_id=?',(registry_id,)).fetchone()
        if row is None:raise ValueError('approved rules missing')
        rules=json.loads(row['body'])
        if fingerprint(rules)!=row['digest'] or rules['manifest']!=batch['manifest']:
            raise ValueError('rules drift')
        assembled=build_agent_input(options=list(batch['options'].values()),candidates=batch['candidates'],
                                   exclusions=batch.get('exclusions',[]),runtime_manifest=rules['manifest'],
                                   rule_documents=rules['documents'])
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            self.policy.freeze_agent_input(principal,csrf,input_id=input_id,batch_id=batch_id,
                                           assembled_input=assembled,transaction=db)
            db.execute('INSERT INTO manual_tasks VALUES(?,?,?,?,?)',(task_id,input_id,registry_id,deadline,principal.actor_id))
            self._transition(db,task_id,1,'created',{})
        return task_id

    def _transition(self,db,task_id,version,state,payload):
        db.execute('INSERT INTO task_transitions VALUES(?,?,?,?,?)',
                   (task_id,version,state,canonical(payload),self.clock().isoformat()))

    def cancel(self,principal,csrf,task_id):
        require(principal,{'admin'},csrf)
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM task_transitions WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
            if row is None:raise ValueError('unknown task')
            if row['state'] in {'created','running'}:
                self._transition(db,task_id,row['version']+1,'cancelled',{})

    def execute(self,principal,csrf,task_id):
        require(principal,{'admin'},csrf)
        if self._executor is None:raise ValueError('NOT_IMPLEMENTED: executor unconfigured')
        if self.assembly is not None:self.assembly.assert_approved()
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            task=db.execute('SELECT * FROM manual_tasks WHERE task_id=?',(task_id,)).fetchone()
            state=db.execute('SELECT * FROM task_transitions WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
            if task is None or state['state']!='created':raise ValueError('task not executable')
            if self.clock()>=instant(task['deadline']):
                self._transition(db,task_id,state['version']+1,'expired',{})
                return 'expired'
            source=db.execute('SELECT * FROM agent_input_records WHERE input_id=?',(task['input_id'],)).fetchone()
            body=json.loads(source['body'])
            if fingerprint(body)!=source['digest']:raise ValueError('input corruption')
            registry=db.execute('SELECT * FROM approved_rules WHERE registry_id=?',(task['registry_id'],)).fetchone()
            if registry is None:raise ValueError('task rule registry missing')
            rules=json.loads(registry['body'])
            assembled=body['assembled_input']
            payload_input=assembled['input']
            if (fingerprint(rules)!=registry['digest'] or fingerprint(payload_input)!=assembled['input_hash']
                    or payload_input['runtime_manifest']!=rules['manifest']
                    or payload_input['rule_documents']!=rules['documents']
                    or body['batch']['manifest']!=rules['manifest']
                    or {o['option_id']:o for o in payload_input['options']}!=body['batch']['options']
                    or payload_input['candidates']!=body['batch']['candidates']):
                raise ValueError('task input/rules/batch mismatch')
            self._transition(db,task_id,state['version']+1,'running',{})
        try:
            response=self._executor(deepcopy(body))
            if (not isinstance(response,dict) or not isinstance(response.get('raw_response'),str)
                    or not response['raw_response'].strip()):raise ValueError('empty executor response')
            payload=dict(response,provider='codex',status='completed',execution_id=task_id,
                         input_id=task['input_id'],input_hash=source['digest'],batch_hash=source['batch_hash'],
                         manifest_hash=source['manifest_hash'],response_hash=fingerprint(response['raw_response']),
                         completed_at=self.clock().isoformat())
            outcome='completed'
        except Exception as exc:
            outcome='failed';payload={'error_type':type(exc).__name__}  # no raw secrets/log text
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            state=db.execute('SELECT * FROM task_transitions WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
            if state['state']!='running':return state['state']  # cancellation wins late return
            if self.clock()>=instant(task['deadline']):outcome='expired';payload={}
            self._transition(db,task_id,state['version']+1,outcome,payload)
        return outcome

    def load_execution(self,task_id):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM task_transitions WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
        if row is None or row['state']!='completed':return None
        return json.loads(row['payload'])
