from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token, login_required
from ..services.auth_service import authenticate_admin
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
    }


def _restaurant_id(db) -> int | None:
    profile = get_restaurant_profile_for_admin(db, session.get('admin_id'))

    if profile:
        return profile['id']

    return session.get('restaurant_id')


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

    products = list_products(db, restaurant_id, active_only=False, query=query or None)
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
        create_product(db, request.form.to_dict(flat=True), restaurant_id)
        flash('Produto cadastrado com sucesso.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')

    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/editar', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)
    product = get_product(db, product_id, restaurant_id)

    if not product:
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    if request.method == 'POST':
        try:
            update_product(db, product_id, request.form.to_dict(flat=True), restaurant_id)
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

    if not get_product(db, product_id, restaurant_id):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    toggle_product(db, product_id, restaurant_id)
    flash('Status do produto atualizado.', 'success')
    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/excluir', methods=['POST'])
@login_required
def delete_product_route(product_id):
    db = get_db()
    restaurant_id = _restaurant_id(db)

    if not get_product(db, product_id, restaurant_id):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))

    removed, message = delete_product(db, product_id, restaurant_id)
    flash(message, 'success' if removed else 'warning')
    return redirect(url_for('admin.products'))
