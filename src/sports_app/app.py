"""Minimal Flask app factory; no scheduler/network work at import time."""
from flask import Flask, g, jsonify, request
from .auth import require


def create_app(*, data_dir: str, principal_loader) -> Flask:
    app = Flask(__name__)
    app.config.update(DATA_DIR=data_dir, PRINCIPAL_LOADER=principal_loader)

    @app.before_request
    def authenticate_request():
        principal = principal_loader(request)
        if principal is None:
            return jsonify(category="HARD_BLOCK", code="UNAUTHENTICATED", message="需要登录"), 401
        g.principal = principal

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/api/analysis-runs")
    def start_analysis():
        try:
            require(g.principal, {"admin"}, request.headers.get("X-CSRF"))
        except PermissionError as exc:
            return jsonify(category="HARD_BLOCK", code="FORBIDDEN", message=str(exc)), 403
        return jsonify(category="EXECUTION_FAILURE", code="NOT_IMPLEMENTED", message="手动分析任务尚未接线"), 501

    @app.post("/api/publications/<draft_id>/confirm")
    def confirm(draft_id: str):
        try:
            require(g.principal, {"admin"}, request.headers.get("X-CSRF"))
        except PermissionError as exc:
            return jsonify(category="HARD_BLOCK", code="FORBIDDEN", message=str(exc)), 403
        return jsonify(category="EXECUTION_FAILURE", code="NOT_IMPLEMENTED", message="发布事务尚未接线", draft_id=draft_id), 501

    return app
