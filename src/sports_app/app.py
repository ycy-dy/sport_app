"""Minimal Flask app factory; no scheduler/network work at import time."""
from flask import Flask, g, jsonify, request
from .auth import require


def create_app(*, data_dir: str, principal_loader, task_runtime=None, publication_chain=None, read_views=None, review_service=None) -> Flask:
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

    from .task_http import register_task_http
    register_task_http(app, task_runtime)

    from .publication_http import register_publication_http
    register_publication_http(app, publication_chain)

    from .read_api import register_read_api
    register_read_api(app, read_views)

    from .review_http import register_review_http
    register_review_http(app, review_service)

    return app
