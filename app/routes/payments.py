from __future__ import annotations

import secrets
from time import time

from flask import Blueprint, flash, jsonify, redirect, request, session, url_for

from ..db import get_db
from ..security import login_required
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..services.payment_service import (
    SERVICE_MODE_DIGITAL_MENU,
    build_mercadopago_authorization_url,
    disable_payment_account,
    ensure_payment_account,
    exchange_authorization_code_for_token,
    payment_connection_summary,
    save_mercadopago_token_response,
    update_payment_account_status,
)

payments_bp = Blueprint('payments', __name__, url_prefix='/pagamentos')

OAUTH_STATE_SESSION_KEY = 'mp_oauth_state'
OAUTH_RESTAURANT_SESSION_KEY = 'mp_oauth_restaurant_id'
OAUTH_STARTED_AT_SESSION_KEY = 'mp_oauth_started_at'
OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60


def _current_restaurant_profile():
    admin_id = session.get('admin_id')
    if not admin_id:
        return None

    return get_restaurant_profile_for_admin(get_db(), admin_id)


def _wants_json() -> bool:
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _profile_or_redirect():
    profile = _current_restaurant_profile()
    if profile:
        return profile

    if _wants_json():
        return jsonify(success=False, message='Perfil do restaurante não encontrado.'), 404

    flash('Perfil do restaurante não encontrado. Faça login novamente.', 'error')
    return redirect(url_for('client.login'))


def _clear_oauth_session() -> None:
    session.pop(OAUTH_STATE_SESSION_KEY, None)
    session.pop(OAUTH_RESTAURANT_SESSION_KEY, None)
    session.pop(OAUTH_STARTED_AT_SESSION_KEY, None)


def _oauth_session_is_valid(profile, received_state: str) -> bool:
    expected_state = session.get(OAUTH_STATE_SESSION_KEY)
    restaurant_id = session.get(OAUTH_RESTAURANT_SESSION_KEY)
    started_at = session.get(OAUTH_STARTED_AT_SESSION_KEY)

    if not expected_state or not received_state or not secrets.compare_digest(str(expected_state), str(received_state)):
        return False

    if str(restaurant_id or '') != str(profile['id']):
        return False

    try:
        age = time() - float(started_at)
    except (TypeError, ValueError):
        return False

    return age <= OAUTH_STATE_MAX_AGE_SECONDS


@payments_bp.route('/mercadopago/status')
@login_required
def mercadopago_status():
    profile = _profile_or_redirect()
    if not hasattr(profile, 'keys'):
        return profile

    summary = payment_connection_summary(get_db(), profile)
    return jsonify(success=True, payment=summary)


@payments_bp.route('/mercadopago/conectar')
@login_required
def mercadopago_connect():
    profile = _profile_or_redirect()
    if not hasattr(profile, 'keys'):
        return profile

    db = get_db()
    summary = payment_connection_summary(db, profile)

    if profile['service_mode'] == SERVICE_MODE_DIGITAL_MENU:
        message = 'Este restaurante está no modo Cardápio digital. O Mercado Pago só será usado no modo Cardápio + pedido + pagamento.'
        if _wants_json():
            return jsonify(success=False, message=message, payment=summary), 400
        flash(message, 'warning')
        return redirect(url_for('client.profile'))

    ensure_payment_account(db, profile['id'])
    summary = payment_connection_summary(db, profile)

    if not summary['env_ready']:
        missing = ', '.join(summary['missing_config']) or 'configurações de pagamento'
        message = f'Antes de conectar o Mercado Pago, configure no Railway: {missing}.'
        update_payment_account_status(db, profile['id'], status='error', last_error=message)
        if _wants_json():
            return jsonify(success=False, message=message, payment=payment_connection_summary(db, profile)), 400
        flash(message, 'warning')
        return redirect(url_for('client.profile'))

    state = secrets.token_urlsafe(32)
    session[OAUTH_STATE_SESSION_KEY] = state
    session[OAUTH_RESTAURANT_SESSION_KEY] = profile['id']
    session[OAUTH_STARTED_AT_SESSION_KEY] = time()
    session.modified = True

    try:
        authorization_url = build_mercadopago_authorization_url(state)
    except RuntimeError as exc:
        message = str(exc)
        update_payment_account_status(db, profile['id'], status='error', last_error=message)
        if _wants_json():
            return jsonify(success=False, message=message, payment=payment_connection_summary(db, profile)), 400
        flash(message, 'warning')
        return redirect(url_for('client.profile'))

    update_payment_account_status(db, profile['id'], status='not_connected', last_error='')
    return redirect(authorization_url)


@payments_bp.route('/mercadopago/callback')
@login_required
def mercadopago_callback():
    profile = _profile_or_redirect()
    if not hasattr(profile, 'keys'):
        return profile

    db = get_db()
    received_state = str(request.args.get('state') or '')
    authorization_code = str(request.args.get('code') or '')
    provider_error = str(request.args.get('error') or '')
    provider_error_description = str(request.args.get('error_description') or '')

    try:
        if not _oauth_session_is_valid(profile, received_state):
            message = 'Não foi possível validar o retorno do Mercado Pago. Tente conectar novamente.'
            update_payment_account_status(db, profile['id'], status='error', last_error=message)
            flash(message, 'error')
            return redirect(url_for('client.profile'))

        if provider_error:
            message = provider_error_description or f'O Mercado Pago retornou erro: {provider_error}.'
            update_payment_account_status(db, profile['id'], status='error', last_error=message)
            flash(message, 'error')
            return redirect(url_for('client.profile'))

        if not authorization_code:
            message = 'O Mercado Pago não retornou o código de autorização.'
            update_payment_account_status(db, profile['id'], status='error', last_error=message)
            flash(message, 'error')
            return redirect(url_for('client.profile'))

        token_response = exchange_authorization_code_for_token(authorization_code)
        save_mercadopago_token_response(db, profile['id'], token_response)
        flash('Mercado Pago conectado com sucesso para este restaurante.', 'success')
        return redirect(url_for('client.profile'))
    except RuntimeError as exc:
        message = str(exc)
        update_payment_account_status(db, profile['id'], status='error', last_error=message)
        flash(message, 'error')
        return redirect(url_for('client.profile'))
    finally:
        _clear_oauth_session()


@payments_bp.route('/mercadopago/desconectar', methods=['POST'])
@login_required
def mercadopago_disconnect():
    profile = _profile_or_redirect()
    if not hasattr(profile, 'keys'):
        return profile

    account = disable_payment_account(get_db(), profile['id'])
    message = 'Conexão Mercado Pago desativada para este restaurante.'

    if _wants_json():
        return jsonify(success=True, message=message, status=account['status'] if account else 'disabled')

    flash(message, 'success')
    return redirect(url_for('client.profile'))
