"""Read-only explicit policy/code inventory; never discovers secrets or approves.

The caller supplies an already approved manifest. capture() is inspection only;
assert_approved() rejects ALL drift. No automatic policy revision acceptance.
"""
import ast
from hashlib import sha256
from pathlib import Path
import re
from .manifest import manifest
from .publication import fingerprint, canonical

RULE_PATHS = {
 'policy.md':'specs/014-evidence-gated-recommendations/policy.md',
 'policy.yaml':'specs/014-evidence-gated-recommendations/policy.yaml',
 'scoring.md':'specs/014-evidence-gated-recommendations/scoring.md',
 'analysis-policy.md':'specs/039-history-led-method-upgrade/analysis-policy.md',
 'analysis-contract.yaml':'specs/039-history-led-method-upgrade/analysis-contract.yaml',
}
CODE_FILES = ('__init__.py','app.py','auth.py','core.py','evidence.py','evidence_store.py',
              'manifest.py','agent_input.py','publication.py','publication_ticket.py',
              'football_markets.py','settlement.py','published_settlement.py','pair_policy.py',
              'storage.py','task_runtime.py','local_assembly.py','decision_review.py','task_http.py')
SCHEMA_FILES = ('storage.py','evidence_store.py','published_settlement.py','settlement.py',
                'pair_policy.py','task_runtime.py','decision_review.py','task_http.py')


def read_explicit(root, relative):
    root=Path(root).resolve()
    path=(root/relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError('explicit inventory path missing or escapes root')
    return path.read_bytes()


def revision(text,key):
    result=re.search(r'^'+re.escape(key)+r":\s*'([^']+)'\s*$",text,re.MULTILINE)
    if not result:raise ValueError('missing policy revision: '+key)
    return result.group(1)


class LocalAssembly:
    def __init__(self,*,policy_root,app_root,approved_manifest):
        self.policy_root=Path(policy_root)
        self.app_root=Path(app_root)
        # Detached copy; inspection cannot silently update approval.
        import json
        self.approved=json.loads(canonical(approved_manifest))

    def capture(self):
        documents={key:read_explicit(self.policy_root,path).decode('utf-8') for key,path in RULE_PATHS.items()}
        code={name:sha256(read_explicit(self.app_root,'src/sports_app/'+name)).hexdigest() for name in CODE_FILES}
        schemas={}
        for name in SCHEMA_FILES:
            source=read_explicit(self.app_root,'src/sports_app/'+name).decode('utf-8')
            tree=ast.parse(source)
            # Bind actual DDL string literals including nested executescript SQL.
            schemas[name]=[node.value for node in ast.walk(tree)
                           if isinstance(node,ast.Constant) and isinstance(node.value,str)
                           and ('CREATE TABLE' in node.value or 'CREATE TRIGGER' in node.value)]
            if not schemas[name]:raise ValueError('schema inventory empty: '+name)
        version=manifest(policy_revision=revision(documents['policy.yaml'],'spec_revision'),
                         analysis_revision=revision(documents['analysis-contract.yaml'],'analysis_revision'),
                         schema_hash=fingerprint(schemas),code_hash=fingerprint(code),codex_identity='codex',
                         rules={key:sha256(value.encode('utf-8')).hexdigest() for key,value in documents.items()})
        return {'documents':documents,'manifest':version,'code_inventory':code,'schema_inventory':schemas}

    def assert_approved(self):
        captured=self.capture()
        if captured['manifest']!=self.approved:
            raise ValueError('local policy/schema/code drift or unapproved revision')
        return captured

    def register(self,runtime,principal,csrf,*,registry_id):
        captured=self.assert_approved()
        version=captured['manifest']
        runtime.approve_rules(principal,csrf,registry_id=registry_id,documents=captured['documents'],
                              policy_revision=version['policy_revision'],analysis_revision=version['analysis_revision'],
                              schema_hash=version['schema_hash'],code_hash=version['code_hash'])
