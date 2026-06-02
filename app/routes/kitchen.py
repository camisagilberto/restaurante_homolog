from __future__ import annotations

from functools import wraps

from flask import (
    Blueprint,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token
from ..services.auth_service import verify_manager_password
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..services.order_service import (
    ORDER_STATUS_LABELS,
    delete_all_orders,
    get_kitchen_orders_signature,
    list_orders_for_kitchen,
    update_order_status,
)

kitchen_bp = Blueprint('kitchen', __name__, url_prefix='/cozinha')

SERVICE_MODE_DIGITAL_MENU = 'digital_menu'


def _service_mode_from_profile(profile) -> str:
    if not profile:
        return 'full_order_payment'

    try:
        if 'service_mode' in profile.keys():
            return profile['service_mode'] or 'full_order_payment'
    except AttributeError:
        pass

    return 'full_order_payment'


def _is_restaurant_active(profile) -> bool:
    if not profile:
        return True

    try:
        if 'is_active' in profile.keys():
            return bool(int(profile['is_active'] or 0))
    except (AttributeError, TypeError, ValueError):
        pass

    return True


def _restaurant_profile(db):
    return get_restaurant_profile_for_admin(db, session.get('admin_id'))


def _restaurant_id(db) -> int | None:
    profile = _restaurant_profile(db)

    if profile:
        return profile['id']

    return session.get('restaurant_id')


def kitchen_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin.login'))

        db = get_db()
        profile = _restaurant_profile(db)

        if profile and not _is_restaurant_active(profile):
            session.pop('kitchen_authorized', None)
            flash('Este restaurante está inativo no QRTotem. A cozinha fica bloqueada até a reativação.', 'warning')
            return redirect(url_for('admin.products'))

        if profile and _service_mode_from_profile(profile) == SERVICE_MODE_DIGITAL_MENU:
            session.pop('kitchen_authorized', None)
            flash('Este restaurante está configurado como cardápio digital. A cozinha fica desabilitada nesse modo.', 'warning')
            return redirect(url_for('admin.products'))

        session['kitchen_authorized'] = True
        return view(*args, **kwargs)

    return wrapped


def _no_cache_response(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@kitchen_bp.route('/validar', methods=['POST'])
def validar_acesso():
    session['kitchen_authorized'] = True
    return jsonify(success=True, redirect_url=url_for('kitchen.orders'))


@kitchen_bp.route('/sair', methods=['POST'])
def sair_cozinha():
    session.pop('kitchen_authorized', None)
    return jsonify(success=True)


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
        initial_signature=get_kitchen_orders_signature(db, restaurant_id),
    )


@kitchen_bp.route('/pedidos')
@kitchen_required
def orders_partial():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        return jsonify(success=False, message='Perfil do restaurante não encontrado.'), 400

    detailed_orders = list_orders_for_kitchen(db, restaurant_id)

    response = make_response(
        render_template(
            'kitchen/_orders_grid.html',
            orders=detailed_orders,
            status_labels=ORDER_STATUS_LABELS,
            csrf=csrf_token(),
        )
    )

    return _no_cache_response(response)


@kitchen_bp.route('/assinatura')
@kitchen_required
def orders_signature():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        return jsonify(success=False, message='Perfil do restaurante não encontrado.'), 400

    response = jsonify(
        success=True,
        signature=get_kitchen_orders_signature(db, restaurant_id),
    )

    return _no_cache_response(response)


@kitchen_bp.route('/<int:order_id>/status', methods=['POST'])
@kitchen_required
def update_status(order_id):
    status = request.form.get('status', 'novo')
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        update_order_status(db, order_id, status, restaurant_id)
    except ValidationError as exc:
        message = str(exc)

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(success=False, message=message), 400

        flash(message, 'error')
        return redirect(url_for('kitchen.orders'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(
            success=True,
            message='Status atualizado.',
            signature=get_kitchen_orders_signature(db, restaurant_id),
        )

    flash('Status atualizado.', 'success')
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
