"""Minimal Flask app factory; no scheduler/network work at import time."""
from flask import Flask, g, jsonify, request, render_template
from pathlib import Path
from .auth import Principal, ROLES
from .auth import require


def create_app(*, data_dir: str, principal_loader, task_runtime=None, publication_chain=None, read_views=None, review_service=None, pairing_auth=None) -> Flask:
    root=Path(__file__).resolve().parents[2]
    app = Flask(__name__,template_folder=str(root/'templates'),static_folder=str(root/'static'))
    if pairing_auth is not None:
        if principal_loader is not None:raise ValueError('pair auth cannot mix injected test identity loader')
        principal_loader=pairing_auth.principal
    if not callable(principal_loader):raise ValueError('authentication adapter required')
    app.config.update(DATA_DIR=data_dir, PRINCIPAL_LOADER=principal_loader)

    @app.before_request
    def authenticate_request():
        if pairing_auth is not None:
            denied=pairing_auth.guard(request)
            if denied:return jsonify(category='HARD_BLOCK',code='ORIGIN_HOST_REJECTED',message='请求来源不允许'),denied
            if request.path in {'/auth/login','/auth/pair'}:
                return None
        principal = principal_loader(request)
        if (not isinstance(principal,Principal) or principal.role not in ROLES
                or not isinstance(principal.actor_id,str) or not principal.actor_id.strip()
                or not isinstance(principal.csrf_token,str) or not principal.csrf_token.strip()):
            return jsonify(category="HARD_BLOCK", code="UNAUTHENTICATED", message="需要登录"), 401
        g.principal = principal

    @app.after_request
    def protect_context(response):
        if request.path=='/' or request.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
            response.headers['Vary']='Cookie'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        return response

    def session_context():
        return {'actor_id':g.principal.actor_id,'role':g.principal.role,
                'csrf_token':g.principal.csrf_token}

    @app.get('/')
    def index():
        return render_template('index.html',session_context=session_context())

    @app.get('/api/session')
    def current_session():
        return jsonify(data=session_context())

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

    if pairing_auth is not None:pairing_auth.install(app)
    return app
