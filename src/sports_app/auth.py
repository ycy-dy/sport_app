"""Server-side role and CSRF checks for write operations."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Principal:
    actor_id: str
    role: str
    csrf_token: str

ROLES = frozenset({"admin", "reviewer", "viewer"})

def require(principal: Principal, allowed: set[str], csrf_header: str | None) -> None:
    if principal.role not in ROLES or principal.role not in allowed:
        raise PermissionError("角色无权执行该操作")
    from hmac import compare_digest
    if (not isinstance(principal.actor_id, str) or not principal.actor_id.strip()
            or not isinstance(principal.csrf_token, str) or not principal.csrf_token.strip()
            or not isinstance(csrf_header, str) or not csrf_header.strip()
            or not compare_digest(csrf_header.encode(), principal.csrf_token.encode())):
        raise PermissionError("CSRF校验失败")
