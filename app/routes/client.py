from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..security import csrf_token
from ..services.auth_service import verify_manager_password
from ..services.cart_service import add_item, clear_cart, find_item, get_cart, remove_item, save_cart, totals, update_item
from ..services.catalog_service import list_products
from ..services.order_service import create_order_from_cart, list_orders_for_table
from ..utils import parse_positive_int

client_bp = Blueprint('client', __name__)


def _wants_json() -> bool:
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _payload() -> dict:
    return request.get_json(silent=True) if request.is_json else request.form.to_dict(flat=True)


def _current_table() -> str:
    return str(session.get('current_table') or '1')


@client_bp.route("/")
def home():
    return render_template("landing.html")


@client_bp.route('/mesa/<table_number>')
def table_menu(table_number):
    table_number = str(parse_positive_int(table_number, default=1, minimum=1, maximum=999))
    session['current_table'] = table_number

    db = get_db()
    products = list_products(db, active_only=True)
    grouped: dict[str, list] = {}
    for product in products:
        grouped.setdefault(product['category'], []).append(product)
    cart = get_cart(session)
    cart_total, cart_quantity = totals(cart)
    return render_template(
        'client/menu.html',
        table_number=table_number,
        grouped_products=grouped,
        cart_quantity=cart_quantity,
        cart_total=cart_total,
        csrf=csrf_token(),
    )


@client_bp.route('/mesa/editar', methods=['POST'])
def edit_table():
    data = _payload()
    new_table = str(data.get('table_number') or '').strip()
    manager_password = str(data.get('manager_password') or '').strip()

    if not new_table:
        message = 'Informe o número da mesa.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    try:
        table_number = parse_positive_int(new_table, minimum=1, maximum=999)
    except (TypeError, ValueError):
        message = 'Número de mesa inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    if not manager_password:
        message = 'Informe a senha do gerente.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    db = get_db()
    if not verify_manager_password(db, manager_password):
        message = 'Senha do gerente inválida.'
        if _wants_json():
            return jsonify(success=False, message=message), 401
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    session['current_table'] = str(table_number)
    if _wants_json():
        return jsonify(
            success=True,
            message='Mesa atualizada com sucesso.',
            table_number=str(table_number),
            redirect_url=url_for('client.table_menu', table_number=table_number),
        )

    flash('Mesa atualizada com sucesso.', 'success')
    return redirect(url_for('client.table_menu', table_number=table_number))


@client_bp.route('/carrinho')
def cart():
    cart = get_cart(session)
    cart_total, cart_quantity = totals(cart)
    return render_template(
        'client/cart.html',
        cart=cart,
        cart_total=cart_total,
        cart_quantity=cart_quantity,
        table_number=_current_table(),
        csrf=csrf_token(),
    )


@client_bp.route('/pedidos')
def order_history():
    table_number = _current_table()
    db = get_db()
    orders = list_orders_for_table(db, table_number)
    return render_template(
        'client/orders.html',
        orders=orders,
        table_number=table_number,
        csrf=csrf_token(),
    )


@client_bp.route('/carrinho/adicionar', methods=['POST'])
def add_to_cart():
    data = _payload()
    try:
        product_id = int(data.get('product_id'))
        quantity = parse_positive_int(data.get('quantity'), default=0, minimum=0, maximum=50)
    except (TypeError, ValueError):
        message = 'Produto ou quantidade inválidos.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.home'))

    if quantity <= 0:
        message = 'Selecione uma quantidade maior que zero.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id = ? AND active = 1', (product_id,)).fetchone()
    if not product:
        message = 'Produto indisponível.'
        if _wants_json():
            return jsonify(success=False, message=message), 404
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=_current_table()))

    cart = get_cart(session)
    add_item(cart, product, quantity)
    save_cart(session, cart)
    cart_total, cart_quantity = totals(cart)

    if _wants_json():
        return jsonify(success=True, message='Produto adicionado ao carrinho.', cart_quantity=cart_quantity, cart_total=cart_total)

    flash('Produto adicionado ao carrinho.', 'success')
    return redirect(url_for('client.table_menu', table_number=_current_table()))


@client_bp.route('/carrinho/atualizar', methods=['POST'])
def update_cart_item():
    data = _payload()
    try:
        product_id = int(data.get('product_id'))
        quantity = parse_positive_int(data.get('quantity', 0), default=0, minimum=0, maximum=99)
    except (TypeError, ValueError):
        message = 'Produto inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    cart = get_cart(session)
    item = find_item(cart, product_id)
    if not item:
        message = 'Item não encontrado.'
        if _wants_json():
            return jsonify(success=False, message=message), 404
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    cart, removed = update_item(cart, product_id, quantity)
    save_cart(session, cart)
    cart_total, cart_quantity = totals(cart)

    if _wants_json():
        return jsonify(
            success=True,
            removed=removed,
            product_id=product_id,
            quantity=0 if removed else quantity,
            item_total=0 if removed else round(float(item['price']) * quantity, 2),
            cart_total=cart_total,
            cart_quantity=cart_quantity,
        )

    flash('Carrinho atualizado.', 'success')
    return redirect(url_for('client.cart'))


@client_bp.route('/carrinho/excluir', methods=['POST'])
def delete_cart_item():
    data = _payload()
    try:
        product_id = int(data.get('product_id'))
    except (TypeError, ValueError):
        message = 'Produto inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    cart = get_cart(session)
    new_cart = remove_item(cart, product_id)
    if len(new_cart) == len(cart):
        message = 'Item não encontrado.'
        if _wants_json():
            return jsonify(success=False, message=message), 404
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    save_cart(session, new_cart)
    cart_total, cart_quantity = totals(new_cart)
    if _wants_json():
        return jsonify(success=True, removed=True, product_id=product_id, item_total=0.0, cart_total=cart_total, cart_quantity=cart_quantity)
    flash('Item removido do carrinho.', 'success')
    return redirect(url_for('client.cart'))


@client_bp.route('/pedido/finalizar', methods=['POST'])
def finalize_order():
    data = _payload()
    table_number = _current_table()
    customer_name = str(data.get('customer_name') or '').strip()
    notes = (data.get('notes') or '').strip() or None
    cart = get_cart(session)

    if not cart:
        message = 'Seu carrinho está vazio.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.table_menu', table_number=table_number))

    if not customer_name:
        message = 'Informe seu nome para finalizar o pedido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    db = get_db()
    order_id = create_order_from_cart(db, str(table_number), cart, customer_name=customer_name, notes=notes)
    clear_cart(session)
    success_message = 'vá ao caixa, e diga seu nome para fazer o pagamento'

    if _wants_json():
        return jsonify(
            success=True,
            message=success_message,
            order_id=order_id,
            redirect_url=url_for('client.table_menu', table_number=table_number),
        )

    flash(success_message, 'success')
    return redirect(url_for('client.table_menu', table_number=table_number))
