from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..security import csrf_token, login_required
from ..services.auth_service import authenticate_admin
from ..services.catalog_service import create_product, delete_product, get_product, list_products, toggle_product, update_product
from ..utils import normalize_text
from ..errors import ValidationError

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


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
                session.clear()
                session['admin_logged_in'] = True
                session['admin_id'] = admin['id']
                session['admin_username'] = admin['username']
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
    products = list_products(db, active_only=False, query=query or None)
    active_count = sum(1 for p in products if p['active'])
    return render_template('admin/products.html', products=products, query=query, active_count=active_count, csrf=csrf_token())


@admin_bp.route('/produtos/criar', methods=['POST'])
@login_required
def create_product_route():
    db = get_db()
    try:
        create_product(db, request.form.to_dict(flat=True))
        flash('Produto cadastrado com sucesso.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/editar', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    db = get_db()
    product = get_product(db, product_id)
    if not product:
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))
    if request.method == 'POST':
        try:
            update_product(db, product_id, request.form.to_dict(flat=True))
            flash('Produto atualizado com sucesso.', 'success')
            return redirect(url_for('admin.products'))
        except ValidationError as exc:
            flash(str(exc), 'error')
    return render_template('admin/product_form.html', product=product, csrf=csrf_token())


@admin_bp.route('/produtos/<int:product_id>/toggle', methods=['POST'])
@login_required
def toggle_product_route(product_id):
    db = get_db()
    if not get_product(db, product_id):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))
    toggle_product(db, product_id)
    flash('Status do produto atualizado.', 'success')
    return redirect(url_for('admin.products'))


@admin_bp.route('/produtos/<int:product_id>/excluir', methods=['POST'])
@login_required
def delete_product_route(product_id):
    db = get_db()
    if not get_product(db, product_id):
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('admin.products'))
    removed, message = delete_product(db, product_id)
    flash(message, 'success' if removed else 'warning')
    return redirect(url_for('admin.products'))
