from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, request, session, url_for

from ..db import get_db
from ..security import login_required
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..services.payment_service import (
    SERVICE_MODE_DIGITAL_MENU,
    disable_payment_account,
    ensure_payment_account,
    payment_connection_summary,
    update_payment_account_status,
)

payments_bp = Blueprint('payments', __name__, url_prefix='/pagamentos')


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

    message = 'Estrutura de conexão pronta. No próximo bloco ativaremos o OAuth real do Mercado Pago.'
    update_payment_account_status(db, profile['id'], status='not_connected', last_error='')

    if _wants_json():
        return jsonify(success=True, message=message, payment=payment_connection_summary(db, profile))

    flash(message, 'info')
    return redirect(url_for('client.profile'))


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
