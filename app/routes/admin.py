from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token, login_required
from ..services.auth_service import authenticate_admin, verify_manager_password
from ..services.catalog_service import create_product, delete_product, get_product, list_products, toggle_product, update_product
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..utils import normalize_text

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def _profile_context(db):
    profile = get_restaurant_profile_for_admin(db, session.get('admin_id'))

    if profile:
        return profile

    return {
        'id': session.get('restaurant_id'),
        'owner_name': session.get('restaurant_owner_name', ''),
        'restaurant_name': session.get('restaurant_name', ''),
        'email': session.get('restaurant_email', ''),
        'cnpj': session.get('restaurant_cnpj', ''),
        'restaurant_address': session.get('restaurant_address', ''),
        'cell_phone': session.get('restaurant_cell_phone', ''),
        'username': session.get('admin_username', ''),
        'public_token': session.get('restaurant_public_token', ''),
        'is_active': session.get('restaurant_is_active', 1),
    }


def _restaurant_id(db) -> int | None:
    profile = get_restaurant_profile_for_admin(db, session.get('admin_id'))

    if profile:
        return profile['id']

    return session.get('restaurant_id')


def _expire_coupon_redemptions(db) -> None:
    now = datetime.utcnow().replace(microsecond=0).isoformat(timespec='seconds')
    db.execute(
        """
        UPDATE coupon_redemptions
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'reserved'
           AND expires_at IS NOT NULL
           AND datetime(expires_at) <= datetime(?)
        """,
        (now,),
    )
    db.execute(
        """
        UPDATE coupon_redemptions
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'code_generated'
           AND code_expires_at IS NOT NULL
           AND datetime(code_expires_at) <= datetime(?)
        """,
        (now,),
    )


def _coupon_code_lookup(db, restaurant_id: int, code: str):
    _expire_coupon_redemptions(db)
    return db.execute(
        """
        SELECT cr.*,
               p.name AS coupon_name,
               p.price AS coupon_price,
               ccu.name AS customer_name,
               ccu.username AS customer_username,
               ccu.email AS customer_email
          FROM coupon_redemptions cr
          JOIN products p ON p.id = cr.coupon_id
          JOIN customer_coupon_users ccu ON ccu.id = cr.customer_id
         WHERE cr.restaurant_id = ?
           AND cr.code = ?
         ORDER BY cr.created_at DESC, cr.id DESC
         LIMIT 1
        """,
        (restaurant_id, code),
    ).fetchone()


def _recent_coupon_redemptions(db, restaurant_id: int, limit: int = 12):
    return db.execute(
        """
        SELECT cr.*,
               p.name AS coupon_name,
               ccu.name AS customer_name,
               ccu.username AS customer_username
          FROM coupon_redemptions cr
          JOIN products p ON p.id = cr.coupon_id
          JOIN customer_coupon_users ccu ON ccu.id = cr.customer_id
         WHERE cr.restaurant_id = ?
         ORDER BY cr.updated_at DESC, cr.created_at DESC, cr.id DESC
         LIMIT ?
        """,
        (restaurant_id, limit),
    ).fetchall()


def _store_profile_in_session(admin, profile=None) -> None:
    session.clear()
    session['admin_logged_in'] = True
    session['admin_id'] = admin['id']
    session['admin_username'] = admin['username']

    if profile:
        session['restaurant_id'] = profile['id']
        session['restaurant_owner_name'] = profile['owner_name']
        session['restaurant_owner_age'] = profile['age']
        session['restaurant_name'] = profile['restaurant_name']
        session['restaurant_email'] = profile['email']
        session['restaurant_cnpj'] = profile['cnpj']
        session['restaurant_address'] = profile['restaurant_address']
        session['restaurant_cell_phone'] = profile['cell_phone']
        session['restaurant_order_payment_mode'] = profile['order_payment_mode'] if 'order_payment_mode' in profile.keys() else 'pay_after'
        service_mode = profile['service_mode'] if 'service_mode' in profile.keys() else 'full_order_payment'
        session['restaurant_service_mode'] = service_mode if service_mode in {'digital_menu', 'full_order_payment'} else 'full_order_payment'
        session['restaurant_is_active'] = int(profile['is_active'] if 'is_active' in profile.keys() else 1)
        session['restaurant_table_count'] = profile['table_count'] if 'table_count' in profile.keys() else 0
        session['restaurant_public_token'] = profile['public_token'] if 'public_token' in profile.keys() else ''
        session['restaurant_slug'] = profile['slug'] if 'slug' in profile.keys() else ''


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = normalize_text(request.form.get('username'))
        password = str(request.form.get('password') or '')

        if not username or not password:
            flash('Informe usuário e senha.', 'error')
        else:
            db = get_db()
            admin = authenticate_admin(db, username, password)

            if admin:
                profile = get_restaurant_profile_for_admin(db, admin['id'])
                _store_profile_in_session(admin, profile)
                flash('Login realizado com sucesso.', 'success')
                return redirect(url_for('admin.products'))

            flash('Usuário ou senha inválidos.', 'error')

    return render_template('admin/login.html', csrf=csrf_token())


@admin_bp.route('/logout')
@login_required
def logout():
    session.clear()
    flash('Sessão encerrada.', 'success')
    return redirect(url_for('admin.login'))


@admin_bp.route('/validar', methods=['POST'])
def validar():
    data = request.get_json(silent=True) or {}
    username = normalize_text(data.get('usuario') or data.get('username'))
    password = str(data.get('senha') or data.get('password') or '')

    if not username or not password:
        return jsonify(success=False, message='Credenciais inválidas.'), 400

    db = get_db()
    admin = authenticate_admin(db, username, password)
    return jsonify(success=bool(admin))


@admin_bp.route('/produtos')
@login_required
def products():
    query = normalize_text(request.args.get('q'))
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('client.signup'))

    products = list_products(db, restaurant_id, active_only=False, query=query or None, kind='menu')
    active_count = sum(1 for p in products if p['active'])
    profile = _profile_context(db)

    return render_template(
        'admin/products.html',
        products=products,
        query=query,
        active_count=active_count,
        profile=profile,
        csrf=csrf_token(),
    )


@admin_bp.route('/produtos/criar', methods=['POST'])
@login_required
def create_product_route():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        create_product(db, request.form.to_dict(flat=True), restaurant_id, kind='menu')
        flash('Produto cadastrado com sucesso.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/editar', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)
    product = get_product(db, product_id, restaurant_id, kind='menu')

    if not product:
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    if request.method == 'POST':
        try:
            update_product(db, product_id, request.form.to_dict(flat=True), restaurant_id, kind='menu')
            flash('Produto atualizado com sucesso.', 'success')
            return redirect(url_for('admin.products'))
        except ValidationError as exc:
            flash(str(exc), 'error')

    return render_template('admin/product_form.html', product=product, csrf=csrf_token())


@admin_bp.route('/produtos/<int:product_id>/toggle', methods=['POST'])
@login_required
def toggle_product_route(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not get_product(db, product_id, restaurant_id, kind='menu'):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    toggle_product(db, product_id, restaurant_id, kind='menu')
    flash('Status do produto atualizado.', 'success')
    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/excluir', methods=['POST'])
@login_required
def delete_product_route(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not get_product(db, product_id, restaurant_id, kind='menu'):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    manager_password = str(request.form.get('manager_password') or '').strip()
    if not verify_manager_password(db, manager_password, admin_id=session.get('admin_id')):
        flash('Senha do usuário inválida. Produto não excluído.', 'error')
        return redirect(url_for('admin.products'))

    removed, message = delete_product(db, product_id, restaurant_id, kind='menu')
    flash(message, 'success' if removed else 'warning')
    return redirect(url_for('admin.products'))


@admin_bp.route('/cupons')
@login_required
def coupons():
    query = normalize_text(request.args.get('q'))
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('client.signup'))

    products = list_products(db, restaurant_id, active_only=False, query=query or None, kind='coupon')
    active_count = sum(1 for p in products if p['active'])
    profile = _profile_context(db)

    return render_template(
        'admin/coupons.html',
        products=products,
        query=query,
        active_count=active_count,
        profile=profile,
        csrf=csrf_token(),
    )


@admin_bp.route('/cupons/validar', methods=['GET', 'POST'])
@login_required
def validate_coupon_code():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('client.signup'))

    profile = _profile_context(db)
    code = ''.join(ch for ch in str(request.form.get('code') or '').strip() if ch.isdigit())
    action = str(request.form.get('action') or 'lookup').strip().lower()
    redemption_id = request.form.get('redemption_id')
    lookup_result = None

    if request.method == 'POST':
        if action == 'confirm':
            try:
                redemption_id_int = int(redemption_id or 0)
            except (TypeError, ValueError):
                redemption_id_int = 0

            row = db.execute(
                """
                SELECT *
                  FROM coupon_redemptions
                 WHERE id = ?
                   AND restaurant_id = ?
                 LIMIT 1
                """,
                (redemption_id_int, restaurant_id),
            ).fetchone()

            if not row:
                flash('Código não encontrado.', 'error')
            elif row['status'] != 'code_generated':
                flash('Este código não está mais disponível para uso.', 'error')
            elif row['code_expires_at'] and datetime.fromisoformat(str(row['code_expires_at']).replace('Z', '+00:00')).replace(tzinfo=None) <= datetime.utcnow():
                db.execute(
                    "UPDATE coupon_redemptions SET status = 'expired', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (redemption_id_int,),
                )
                db.commit()
                flash('Código expirado. Peça para o cliente resgatar ou gerar outro cupom.', 'error')
            else:
                db.execute(
                    """
                    UPDATE coupon_redemptions
                       SET status = 'used',
                           used_at = CURRENT_TIMESTAMP,
                           validated_by_admin_id = ?,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                       AND restaurant_id = ?
                       AND status = 'code_generated'
                    """,
                    (session.get('admin_id'), redemption_id_int, restaurant_id),
                )
                db.commit()
                flash('Cupom validado e marcado como usado.', 'success')
                return redirect(url_for('admin.validate_coupon_code'))
        else:
            if not code:
                flash('Digite o código numérico apresentado pelo cliente.', 'error')
            else:
                lookup_result = _coupon_code_lookup(db, restaurant_id, code)
                db.commit()

                if not lookup_result:
                    flash('Código inválido, expirado ou de outro restaurante.', 'error')
                elif lookup_result['status'] != 'code_generated':
                    flash('Código inválido, expirado ou já utilizado.', 'error')
                elif lookup_result['code_expires_at'] and datetime.fromisoformat(str(lookup_result['code_expires_at']).replace('Z', '+00:00')).replace(tzinfo=None) <= datetime.utcnow():
                    db.execute(
                        "UPDATE coupon_redemptions SET status = 'expired', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (lookup_result['id'],),
                    )
                    db.commit()
                    lookup_result = None
                    flash('Código expirado. Peça para o cliente gerar outro código.', 'error')
                else:
                    flash('Cupom válido. Confira as informações e confirme o uso.', 'success')

    _expire_coupon_redemptions(db)
    recent_redemptions = _recent_coupon_redemptions(db, restaurant_id)
    db.commit()

    return render_template(
        'admin/coupon_validate.html',
        profile=profile,
        code=code,
        lookup_result=lookup_result,
        recent_redemptions=recent_redemptions,
        csrf=csrf_token(),
    )


@admin_bp.route('/cupons/criar', methods=['POST'])
@login_required
def create_coupon_route():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        create_product(db, request.form.to_dict(flat=True), restaurant_id, kind='coupon')
        flash('Cupom cadastrado com sucesso.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('admin.coupons'))


@admin_bp.route('/cupons/<int:product_id>/editar', methods=['GET', 'POST'])
@login_required
def edit_coupon(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)
    product = get_product(db, product_id, restaurant_id, kind='coupon')

    if not product:
        flash('Cupom não encontrado.', 'error')
        return redirect(url_for('admin.coupons'))

    if request.method == 'POST':
        try:
            update_product(db, product_id, request.form.to_dict(flat=True), restaurant_id, kind='coupon')
            flash('Cupom atualizado com sucesso.', 'success')
            return redirect(url_for('admin.coupons'))
        except ValidationError as exc:
            flash(str(exc), 'error')

    return render_template(
        'admin/coupon_form.html',
        product=product,
        csrf=csrf_token(),
    )


@admin_bp.route('/cupons/<int:product_id>/toggle', methods=['POST'])
@login_required
def toggle_coupon_route(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not get_product(db, product_id, restaurant_id, kind='coupon'):
        flash('Cupom não encontrado.', 'error')
        return redirect(url_for('admin.coupons'))

    toggle_product(db, product_id, restaurant_id, kind='coupon')
    flash('Status do cupom atualizado.', 'success')
    return redirect(url_for('admin.coupons'))


@admin_bp.route('/cupons/<int:product_id>/excluir', methods=['POST'])
@login_required
def delete_coupon_route(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not get_product(db, product_id, restaurant_id, kind='coupon'):
        flash('Cupom não encontrado.', 'error')
        return redirect(url_for('admin.coupons'))

    manager_password = str(request.form.get('manager_password') or '').strip()
    if not verify_manager_password(db, manager_password, admin_id=session.get('admin_id')):
        flash('Senha do usuário inválida. Cupom não excluído.', 'error')
        return redirect(url_for('admin.coupons'))

    removed, message = delete_product(db, product_id, restaurant_id, kind='coupon')
    message = message.replace('Produto', 'Cupom').replace('produto', 'cupom')
    flash(message, 'success' if removed else 'warning')
    return redirect(url_for('admin.coupons'))
