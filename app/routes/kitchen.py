from __future__ import annotations

from functools import wraps

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token
from ..services.auth_service import verify_manager_password
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..services.order_service import ORDER_STATUS_LABELS, delete_all_orders, list_orders_for_kitchen, update_order_status

kitchen_bp = Blueprint('kitchen', __name__, url_prefix='/cozinha')


def _restaurant_id(db) -> int | None:
    profile = get_restaurant_profile_for_admin(db, session.get('admin_id'))

    if profile:
        return profile['id']

    return session.get('restaurant_id')


def kitchen_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin.login'))

        return view(*args, **kwargs)

    return wrapped


@kitchen_bp.route('/validar', methods=['POST'])
def validar_acesso():
    data = request.get_json(silent=True) or {}
    password = str(data.get('password') or '').strip()

    if not password:
        return jsonify(success=False, message='Informe a senha.'), 400

    db = get_db()
    admin_id = session.get('admin_id')

    if not verify_manager_password(db, password, admin_id=admin_id):
        return jsonify(success=False, message='Senha inválida.'), 401

    session['kitchen_authorized'] = True
    return jsonify(success=True, redirect_url=url_for('kitchen.orders'))


@kitchen_bp.route('/')
@kitchen_required
def orders():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('admin.login'))

    detailed_orders = list_orders_for_kitchen(db, restaurant_id)

    return render_template(
        'kitchen/orders.html',
        orders=detailed_orders,
        status_labels=ORDER_STATUS_LABELS,
        csrf=csrf_token(),
    )


@kitchen_bp.route('/<int:order_id>/status', methods=['POST'])
@kitchen_required
def update_status(order_id):
    status = request.form.get('status', 'novo')
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        update_order_status(db, order_id, status, restaurant_id)
        flash('Status atualizado.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('kitchen.orders'))


@kitchen_bp.route('/apagar-pedidos', methods=['POST'])
@kitchen_required
def delete_orders_history():
    data = request.get_json(silent=True) or {}
    password = str(data.get('password') or '').strip()

    if not password:
        return jsonify(success=False, message='Informe a senha do admin.'), 400

    db = get_db()
    admin_id = session.get('admin_id')
    restaurant_id = _restaurant_id(db)

    if not verify_manager_password(db, password, admin_id=admin_id):
        return jsonify(success=False, message='Senha inválida.'), 401

    try:
        delete_all_orders(db, restaurant_id)
    except ValidationError as exc:
        return jsonify(success=False, message=str(exc)), 400

    return jsonify(
        success=True,
        message='Histórico de pedidos apagado com sucesso.',
        redirect_url=url_for('kitchen.orders'),
    )
