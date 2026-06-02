from __future__ import annotations

import secrets
from time import time

from flask import Blueprint, current_app, flash, jsonify, redirect, request, session, url_for

from ..db import get_db
from ..security import login_required
from ..services.order_service import update_order_payment_status
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
    fetch_mercadopago_payment_status,
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


def _extract_webhook_payment_id() -> str:
    """Extrai o ID do pagamento em diferentes formatos usados pelo Mercado Pago."""
    data = request.get_json(silent=True) or {}

    candidates = [
        request.args.get('data.id'),
        request.args.get('id'),
        request.args.get('payment_id'),
    ]

    if isinstance(data, dict):
        nested_data = data.get('data') if isinstance(data.get('data'), dict) else {}
        candidates.extend(
            [
                nested_data.get('id') if isinstance(nested_data, dict) else None,
                data.get('id'),
                data.get('resource'),
            ]
        )

    for candidate in candidates:
        value = str(candidate or '').strip()
        if value:
            # Alguns webhooks antigos podem mandar uma URL em "resource".
            return value.rstrip('/').split('/')[-1]

    return ''


def _is_payment_webhook_event() -> bool:
    """Ignora notificações que não sejam de pagamento."""
    data = request.get_json(silent=True) or {}
    query_topic = str(request.args.get('topic') or request.args.get('type') or '').strip().lower()

    body_type = ''
    body_action = ''

    if isinstance(data, dict):
        body_type = str(data.get('type') or '').strip().lower()
        body_action = str(data.get('action') or '').strip().lower()

    values = {query_topic, body_type, body_action}
    return (
        not any(values)
        or 'payment' in values
        or 'payment.created' in values
        or 'payment.updated' in values
    )


def _find_order_by_payment_external_id(db, payment_external_id: str):
    payment_id = str(payment_external_id or '').strip()
    if not payment_id:
        return None

    return db.execute(
        """
        SELECT *
          FROM orders
         WHERE payment_provider = ?
           AND payment_external_id = ?
         LIMIT 1
        """,
        ('mercadopago', payment_id),
    ).fetchone()


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


@payments_bp.route('/mercadopago/webhook', methods=['POST'])
def mercadopago_webhook():
    """Recebe notificações do Mercado Pago e atualiza o status do pedido.

    A rota não depende da sessão do cliente/restaurante. Para evitar confiar apenas
    no payload recebido, ela usa o ID do pagamento recebido no webhook para consultar
    o Mercado Pago novamente com o token da conta conectada ao restaurante.
    """
    db = get_db()

    if not _is_payment_webhook_event():
        return jsonify(success=True, ignored=True, reason='Evento ignorado.'), 200

    payment_external_id = _extract_webhook_payment_id()
    if not payment_external_id:
        current_app.logger.warning('Webhook Mercado Pago sem ID de pagamento. args=%s body=%s', dict(request.args), request.get_json(silent=True))
        return jsonify(success=False, message='ID de pagamento ausente.'), 400

    order = _find_order_by_payment_external_id(db, payment_external_id)
    if not order:
        # Pode acontecer se o Mercado Pago enviar teste de webhook ou evento antigo.
        current_app.logger.info('Webhook Mercado Pago ignorado: pagamento %s não encontrado no QRTotem.', payment_external_id)
        return jsonify(success=True, ignored=True, reason='Pagamento não encontrado no QRTotem.'), 200

    restaurant_id = int(order['restaurant_id'])
    order_id = int(order['id'])

    try:
        payment_result = fetch_mercadopago_payment_status(
            db,
            restaurant_id=restaurant_id,
            payment_external_id=payment_external_id,
        )
    except RuntimeError as exc:
        current_app.logger.exception('Falha ao consultar Mercado Pago no webhook para pedido %s.', order_id)
        return jsonify(success=False, message=str(exc)), 502

    expected_reference = str(order['payment_external_reference'] or '')
    returned_reference = str(payment_result.get('external_reference') or '')

    if expected_reference and returned_reference and returned_reference != expected_reference:
        current_app.logger.warning(
            'Webhook Mercado Pago com external_reference divergente. pedido=%s esperado=%s recebido=%s',
            order_id,
            expected_reference,
            returned_reference,
        )
        return jsonify(success=True, ignored=True, reason='Referência externa divergente.'), 200

    provider_status = str(payment_result.get('status') or 'pending')

    if provider_status in {'approved', 'rejected', 'cancelled', 'expired', 'pending'}:
        update_order_payment_status(
            db,
            restaurant_id=restaurant_id,
            order_id=order_id,
            payment_status=provider_status,
            approved_at=payment_result.get('approved_at') or None,
            payment_error='' if provider_status in {'approved', 'pending'} else payment_result.get('status_detail', ''),
        )

    return jsonify(
        success=True,
        order_id=order_id,
        payment_external_id=payment_external_id,
        payment_status=provider_status,
        approved=provider_status == 'approved',
    ), 200



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
