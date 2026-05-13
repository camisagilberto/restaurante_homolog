from __future__ import annotations

import secrets
from functools import wraps

from flask import g, has_request_context, jsonify, redirect, request, session, url_for

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}


def init_security(app):
    @app.before_request
    def _ensure_csrf_token():
        token = session.get('csrf_token')
        if not token:
            token = secrets.token_urlsafe(32)
            session['csrf_token'] = token
        g.csrf_token = token

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
        return response


def csrf_token() -> str:
    if not has_request_context():
        return ''
    token = getattr(g, 'csrf_token', None) or session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
        g.csrf_token = token
    return token


def inject_globals():
    return {'csrf_token': csrf_token}


def _request_token() -> str:
    return (
        request.headers.get('X-CSRFToken')
        or request.headers.get('X-CSRF-Token')
        or request.form.get('csrf_token', '')
        or (request.get_json(silent=True) or {}).get('csrf_token', '')
    )


def csrf_protect():
    if request.method in SAFE_METHODS or request.endpoint == 'static':
        return None
    expected = session.get('csrf_token')
    provided = _request_token()
    if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(success=False, message='Token de segurança inválido.'), 403
        return redirect(url_for('client.home'))
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin.login'))
        return view(*args, **kwargs)
    return wrapped
