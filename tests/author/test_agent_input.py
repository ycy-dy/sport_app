from hashlib import sha256
import pytest
from sports_app.agent_input import build_agent_input, REQUIRED_OPTION_FIELDS, IDENTITY_FIELDS
from sports_app.manifest import POLICY_FILES, manifest


def inputs():
    # Synthetic policy texts test plumbing only, not actual 039 semantic coverage.
    documents = {name: 'synthetic document '+name for name in POLICY_FILES}
    rules = {name: sha256(value.encode()).hexdigest() for name,value in documents.items()}
    versions = manifest(policy_revision='test-policy',analysis_revision='test-analysis',
                        schema_hash='test-schema',code_hash='test-code',codex_identity='codex',rules=rules)
    option = {key: 'synthetic research' for key in REQUIRED_OPTION_FIELDS}
    option['event_definition'] = {key: 'synthetic' for key in IDENTITY_FIELDS}
    option['evidence_as_of'] = '2026-11-14T05:00:00+00:00'
    return dict(options=[option],candidates=[{'id':'synthetic-candidate'}],exclusions=[],
                runtime_manifest=versions,rule_documents=documents)


def test_full_documents_and_manifest_frozen_without_execution():
    args = inputs()
    result = build_agent_input(**args)
    assert set(result['input']['rule_documents']) == set(POLICY_FILES)
    assert result['input']['execution_status'] == 'input_assembled_not_executed'
    args['options'][0]['covered_paths'] = 'modified'
    assert result['input']['options'][0]['covered_paths'] == 'synthetic research'


@pytest.mark.parametrize('field',REQUIRED_OPTION_FIELDS)
def test_missing_039_field_rejected(field):
    args = inputs()
    del args['options'][0][field]
    with pytest.raises(ValueError): build_agent_input(**args)


def test_policy_drift_and_unaccepted_probabilities_rejected():
    args = inputs()
    args['rule_documents']['analysis-contract.yaml'] += 'changed'
    with pytest.raises(ValueError): build_agent_input(**args)
    args = inputs()
    args['options'][0]['model_probability'] = 0.8
    with pytest.raises(ValueError): build_agent_input(**args)
