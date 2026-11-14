"""Fail-closed runtime manifest for policy and analysis inputs."""
from hashlib import sha256
from pathlib import Path

POLICY_FILES = (
    "policy.md", "policy.yaml", "scoring.md",
    "analysis-policy.md", "analysis-contract.yaml",
)

def file_hashes(root: str | Path) -> dict[str, str]:
    base = Path(root).resolve()
    result = {}
    for name in POLICY_FILES:
        path = base / name
        if not path.is_file():
            raise FileNotFoundError(name)
        result[name] = sha256(path.read_bytes()).hexdigest()
    return result

def manifest(*, policy_revision: str, analysis_revision: str, schema_hash: str,
             code_hash: str, codex_identity: str, rules: dict[str, str]) -> dict:
    if codex_identity != "codex":
        raise ValueError("运行提供方必须为codex")
    if set(rules) != set(POLICY_FILES):
        raise ValueError("规则文件必须完整包含014三份和039两份")
    return {"policy_revision": policy_revision, "analysis_revision": analysis_revision,
            "schema_hash": schema_hash, "code_hash": code_hash,
            "runtime_provider": codex_identity, "rule_hashes": dict(sorted(rules.items()))}
