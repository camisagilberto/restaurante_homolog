from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token, login_required
from ..services.auth_service import authenticate_admin, verify_manager_password
from ..services.catalog_service import create_product, delete_product, get_product, list_products, toggle_product, update_product
from ..services.order_service import (
    approve_attendant_order,
    get_attendant_orders_signature,
    list_orders_for_attendant,
    reject_attendant_order,
)
from ..services.onboarding_service import get_restaurant_profile_for_admin
from ..utils import normalize_text

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')



def _parse_money(value: str) -> float:
    normalized = str(value or '').strip().replace('.', '').replace(',', '.')
    try:
        parsed = float(normalized)
    except (TypeError, ValueError):
        return 0.0
    return round(parsed, 2)


def _parse_int(value: str) -> int:
    try:
        return max(0, int(str(value or '').strip()))
    except (TypeError, ValueError):
        return 0


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(timespec='seconds')


def _expire_restaurant_credit_allocations(db) -> None:
    db.execute(
        """
        UPDATE qrtotem_restaurant_credit_allocations
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'available'
           AND expires_at IS NOT NULL
           AND datetime(expires_at) <= datetime('now')
        """
    )


def _available_restaurant_credit_allocations(db, restaurant_id: int):
    _expire_restaurant_credit_allocations(db)
    return db.execute(
        """
        SELECT a.*,
               d.title AS distribution_title,
               (a.initial_amount - a.allocated_amount) AS remaining_amount
          FROM qrtotem_restaurant_credit_allocations a
          JOIN qrtotem_restaurant_credit_distributions d ON d.id = a.distribution_id
         WHERE a.restaurant_id = ?
           AND a.status = 'available'
           AND a.restaurant_was_active = 1
           AND (a.expires_at IS NULL OR datetime(a.expires_at) > datetime('now'))
           AND (a.initial_amount - a.allocated_amount) > 0
         ORDER BY datetime(a.expires_at) ASC, a.id ASC
        """,
        (restaurant_id,),
    ).fetchall()


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



def _expire_qrtotem_coupon_redemptions(db) -> None:
    now = datetime.utcnow().replace(microsecond=0).isoformat(timespec='seconds')
    db.execute(
        """
        UPDATE qrtotem_coupon_redemptions
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'code_generated'
           AND code_expires_at IS NOT NULL
           AND datetime(code_expires_at) <= datetime(?)
        """,
        (now,),
    )


def _qrtotem_coupon_code_lookup(db, code: str):
    _expire_qrtotem_coupon_redemptions(db)
    return db.execute(
        """
        SELECT cl.*,
               c.title AS campaign_title,
               c.value,
               c.min_purchase_amount,
               c.coupon_type,
               cu.name AS customer_name,
               cu.username AS customer_username,
               cu.email AS customer_email,
               cu.cell_phone AS customer_cell_phone
          FROM qrtotem_coupon_redemptions cl
          JOIN qrtotem_coupon_campaigns c ON c.id = cl.campaign_id
          JOIN customer_coupon_users cu ON cu.id = cl.customer_id
         WHERE cl.code = ?
         ORDER BY cl.created_at DESC, cl.id DESC
         LIMIT 1
        """,
        (code,),
    ).fetchone()


def _recent_qrtotem_coupon_uses(db, restaurant_id: int, limit: int = 12):
    return db.execute(
        """
        SELECT cl.*,
               c.title AS campaign_title,
               c.value,
               c.min_purchase_amount,
               cu.name AS customer_name,
               cu.username AS customer_username,
               cu.email AS customer_email
          FROM qrtotem_coupon_redemptions cl
          JOIN qrtotem_coupon_campaigns c ON c.id = cl.campaign_id
          JOIN customer_coupon_users cu ON cu.id = cl.customer_id
         WHERE cl.used_restaurant_id = ?
         ORDER BY cl.updated_at DESC, cl.created_at DESC, cl.id DESC
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
    flash('As promoções do restaurante não precisam de validação por código. Os cupons rastreáveis do QRTotem serão criados em uma área separada.', 'info')
    return redirect(url_for('admin.coupons'))

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
                flash('Código não encontrado. Consulte o código novamente antes de confirmar.', 'error')
            elif code and str(row['code'] or '') != code:
                flash('Código não confere com a consulta realizada. Consulte novamente.', 'error')
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


@admin_bp.route('/cupons-qrtotem/restaurante', methods=['GET', 'POST'])
@login_required
def restaurant_qrtotem_coupons():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('client.signup'))

    profile = _profile_context(db)
    is_restaurant_active = int(profile['is_active'] if profile and 'is_active' in profile.keys() else 1) == 1
    if not is_restaurant_active:
        flash('Seu restaurante está inativo. Ative o restaurante para criar cupons com crédito QRTotem.', 'warning')

    _expire_restaurant_credit_allocations(db)

    if request.method == 'POST':
        title = str(request.form.get('title') or '').strip()
        description = str(request.form.get('description') or '').strip()
        discount_amount = _parse_money(request.form.get('discount_amount'))
        quantity = _parse_int(request.form.get('quantity'))
        total_cost = round(discount_amount * quantity, 2)

        allocations = _available_restaurant_credit_allocations(db, restaurant_id)
        available_total = round(sum(float(row['remaining_amount'] or 0) for row in allocations), 2)
        selected_allocation = None
        for row in allocations:
            if float(row['remaining_amount'] or 0) + 0.0001 >= total_cost:
                selected_allocation = row
                break

        if not is_restaurant_active:
            flash('Restaurante inativo não pode criar cupons com crédito promocional.', 'error')
        elif not title:
            flash('Informe o nome do cupom.', 'error')
        elif discount_amount <= 0:
            flash('Informe um valor de cupom maior que zero.', 'error')
        elif quantity <= 0:
            flash('Informe a quantidade de cupons.', 'error')
        elif total_cost > available_total + 0.0001:
            flash('Saldo insuficiente para criar esses cupons.', 'error')
        elif not selected_allocation:
            flash('Seu saldo está dividido em créditos menores. Crie uma quantidade menor ou use outro valor de cupom.', 'warning')
        else:
            now = datetime.utcnow()
            expires_at = now + timedelta(days=30)
            min_purchase_amount = round(discount_amount + 5, 2)
            cursor = db.execute(
                """
                INSERT INTO qrtotem_coupon_campaigns (
                    title,
                    description,
                    coupon_type,
                    value,
                    min_purchase_amount,
                    total_quantity,
                    active,
                    restaurant_id,
                    credit_allocation_id,
                    starts_at,
                    ends_at,
                    expires_at,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'restaurant_credit', ?, ?, ?, 1, ?, ?, ?, ?, ?, 'restaurant', ?, ?)
                """,
                (
                    title,
                    description,
                    discount_amount,
                    min_purchase_amount,
                    quantity,
                    restaurant_id,
                    selected_allocation['id'],
                    _iso(now),
                    _iso(expires_at),
                    _iso(expires_at),
                    _iso(now),
                    _iso(now),
                ),
            )
            new_allocated = round(float(selected_allocation['allocated_amount'] or 0) + total_cost, 2)
            new_status = 'consumed' if new_allocated + 0.0001 >= float(selected_allocation['initial_amount'] or 0) else 'available'
            db.execute(
                """
                UPDATE qrtotem_restaurant_credit_allocations
                   SET allocated_amount = ?,
                       status = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (new_allocated, new_status, selected_allocation['id']),
            )
            db.commit()
            flash('Cupons criados com sucesso usando o crédito QRTotem. Eles vencem em 30 dias.', 'success')
            return redirect(url_for('admin.restaurant_qrtotem_coupons'))

    db.commit()
    allocations = _available_restaurant_credit_allocations(db, restaurant_id)
    available_total = round(sum(float(row['remaining_amount'] or 0) for row in allocations), 2)

    campaigns = db.execute(
        """
        SELECT c.*,
               COALESCE(SUM(CASE WHEN r.status = 'used' THEN 1 ELSE 0 END), 0) AS used_count,
               COALESCE(SUM(CASE WHEN r.status = 'code_generated' THEN 1 ELSE 0 END), 0) AS pending_count
          FROM qrtotem_coupon_campaigns c
          LEFT JOIN qrtotem_coupon_redemptions r ON r.campaign_id = c.id
         WHERE c.coupon_type = 'restaurant_credit'
           AND c.restaurant_id = ?
         GROUP BY c.id
         ORDER BY c.created_at DESC, c.id DESC
        """,
        (restaurant_id,),
    ).fetchall()

    return render_template(
        'admin/restaurant_qrtotem_coupons.html',
        profile=profile,
        allocations=allocations,
        available_total=available_total,
        campaigns=campaigns,
        validity_days=30,
        csrf=csrf_token(),
    )


@admin_bp.route('/cupons-qrtotem/validar', methods=['GET', 'POST'])
@login_required
def validate_qrtotem_coupon_code():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('client.signup'))

    profile = _profile_context(db)
    code = ''.join(ch for ch in str(request.form.get('code') or '').strip() if ch.isdigit())
    action = str(request.form.get('action') or 'lookup').strip().lower()
    claim_id = request.form.get('redemption_id') or request.form.get('claim_id')
    lookup_result = None

    if request.method == 'POST':
        if action == 'confirm':
            try:
                claim_id_int = int(claim_id or 0)
            except (TypeError, ValueError):
                claim_id_int = 0

            row = db.execute(
                """
                SELECT *
                  FROM qrtotem_coupon_redemptions
                 WHERE id = ?
                 LIMIT 1
                """,
                (claim_id_int,),
            ).fetchone()

            if not row:
                flash('Código não encontrado. Consulte o código novamente antes de confirmar.', 'error')
            elif code and str(row['code'] or '') != code:
                flash('Código não confere com a consulta realizada. Consulte novamente.', 'error')
            elif row['status'] != 'code_generated':
                flash('Este código não está mais disponível para uso.', 'error')
            elif row['code_expires_at'] and datetime.fromisoformat(str(row['code_expires_at']).replace('Z', '+00:00')).replace(tzinfo=None) <= datetime.utcnow():
                db.execute("UPDATE qrtotem_coupon_redemptions SET status = 'expired', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (claim_id_int,))
                db.commit()
                flash('Código expirado. Peça para o cliente gerar outro código.', 'error')
            else:
                db.execute(
                    """
                    UPDATE qrtotem_coupon_redemptions
                       SET status = 'used',
                           used_restaurant_id = ?,
                           used_at = CURRENT_TIMESTAMP,
                           validated_by_admin_id = ?,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                       AND status = 'code_generated'
                    """,
                    (restaurant_id, session.get('admin_id'), claim_id_int),
                )
                db.commit()
                flash('Cupom QRTotem validado e marcado como usado.', 'success')
                return redirect(url_for('admin.validate_qrtotem_coupon_code'))
        else:
            if not code:
                flash('Digite o código numérico apresentado pelo cliente.', 'error')
            else:
                lookup_result = _qrtotem_coupon_code_lookup(db, code)
                db.commit()

                if not lookup_result:
                    flash('Código inválido ou expirado.', 'error')
                elif lookup_result['status'] != 'code_generated':
                    flash('Código inválido, expirado ou já utilizado.', 'error')
                elif lookup_result['code_expires_at'] and datetime.fromisoformat(str(lookup_result['code_expires_at']).replace('Z', '+00:00')).replace(tzinfo=None) <= datetime.utcnow():
                    db.execute("UPDATE qrtotem_coupon_redemptions SET status = 'expired', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (lookup_result['id'],))
                    db.commit()
                    lookup_result = None
                    flash('Código expirado. Peça para o cliente gerar outro código.', 'error')
                else:
                    flash('Cupom válido. Confira valor mínimo e confirme o uso.', 'success')

    _expire_qrtotem_coupon_redemptions(db)
    recent_uses = _recent_qrtotem_coupon_uses(db, restaurant_id)
    db.commit()

    return render_template(
        'admin/qrtotem_coupon_validate.html',
        profile=profile,
        code=code,
        lookup_result=lookup_result,
        recent_uses=recent_uses,
        csrf=csrf_token(),
    )


@admin_bp.route('/atendente')
@login_required
def attendant_orders():
    db = get_db()
    profile = _profile_context(db)
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        flash('Perfil do restaurante não encontrado.', 'error')
        return redirect(url_for('admin.login'))

    orders = list_orders_for_attendant(db, restaurant_id)
    return render_template(
        'admin/attendant_orders.html',
        profile=profile,
        orders=orders,
        csrf=csrf_token(),
        initial_signature=get_attendant_orders_signature(db, restaurant_id),
    )


@admin_bp.route('/atendente/<int:order_id>/enviar', methods=['POST'])
@login_required
def attendant_send_order(order_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        approve_attendant_order(db, order_id, restaurant_id)
        flash('Pedido enviado para a cozinha.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('admin.attendant_orders'))


@admin_bp.route('/atendente/<int:order_id>/nao-enviar', methods=['POST'])
@login_required
def attendant_reject_order(order_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    try:
        reject_attendant_order(db, order_id, restaurant_id)
        flash('Pedido não enviado para a cozinha.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('admin.attendant_orders'))


@admin_bp.route('/atendente/assinatura')
@login_required
def attendant_signature():
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not restaurant_id:
        return jsonify(success=False, message='Perfil do restaurante não encontrado.'), 400

    return jsonify(success=True, signature=get_attendant_orders_signature(db, restaurant_id))
