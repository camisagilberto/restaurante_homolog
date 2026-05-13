from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token
from ..services.auth_service import verify_manager_password
from ..services.cart_service import add_item, clear_cart, find_item, get_cart, remove_item, save_cart, totals, update_item
from ..services.catalog_service import list_products, validate_product_payload
from ..services.menu_import_service import import_menu_uploads
from ..services.onboarding_service import create_restaurant_account, get_restaurant_profile_for_admin
from ..services.order_service import create_order_from_cart, list_orders_for_table
from ..utils import parse_positive_int

client_bp = Blueprint('client', __name__)

PENDING_MENU_IMPORT_SESSION_KEY = 'pending_menu_import'


def _wants_json() -> bool:
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _payload() -> dict:
    return request.get_json(silent=True) if request.is_json else request.form.to_dict(flat=True)


def _current_table() -> str:
    return str(session.get('current_table') or '1')


def _restaurant_context() -> dict[str, str]:
    context = {
        'owner_name': session.get('restaurant_owner_name', ''),
        'restaurant_name': session.get('restaurant_name', ''),
        'email': session.get('restaurant_email', ''),
        'cnpj': session.get('restaurant_cnpj', ''),
        'restaurant_address': session.get('restaurant_address', ''),
        'cell_phone': session.get('restaurant_cell_phone', ''),
        'username': session.get('admin_username', ''),
    }

    admin_id = session.get('admin_id')
    if admin_id:
        db = get_db()
        profile = get_restaurant_profile_for_admin(db, admin_id)
        if profile:
            context.update(
                {
                    'owner_name': profile['owner_name'],
                    'restaurant_name': profile['restaurant_name'],
                    'email': profile['email'],
                    'cnpj': profile['cnpj'],
                    'restaurant_address': profile['restaurant_address'],
                    'cell_phone': profile['cell_phone'],
                    'username': profile['username'],
                }
            )
    return context


@client_bp.route('/')
def home():
    return render_template('landing.html')


@client_bp.route('/cadastro', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        db = get_db()
        try:
            account = create_restaurant_account(db, request.form.to_dict(flat=True))
        except ValidationError as exc:
            flash(str(exc), 'error')
        except Exception:
            flash('Não foi possível criar o acesso agora.', 'error')
        else:
            session.clear()
            session['admin_logged_in'] = True
            session['admin_id'] = account['admin_id']
            session['admin_username'] = account['username']
            session['restaurant_owner_name'] = account['owner_name']
            session['restaurant_name'] = account['restaurant_name']
            session['restaurant_email'] = account['email']
            session['restaurant_cnpj'] = account['cnpj']
            session['restaurant_address'] = account['restaurant_address']
            session['restaurant_cell_phone'] = account['cell_phone']
            flash('Cadastro realizado com sucesso.', 'success')
            return redirect(url_for('client.products_start'))

    return render_template('client/signup.html', csrf=csrf_token())


@client_bp.route('/produtos-inicio')
def products_start():
    profile = _restaurant_context()
    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))
    return render_template('client/products_start.html', profile=profile, csrf=csrf_token())


@client_bp.route('/produtos-inicio/manual')
def products_manual():
    return redirect(url_for('admin.products'))


def _pending_menu_import() -> dict | None:
    pending = session.get(PENDING_MENU_IMPORT_SESSION_KEY)

    if not isinstance(pending, dict):
        return None

    items = pending.get('items') or []

    if not isinstance(items, list) or not items:
        return None

    return pending


def _existing_product(db, name: str, category: str, price: float) -> bool:
    row = db.execute(
        '''
        SELECT 1
          FROM products
         WHERE lower(name) = lower(?)
           AND lower(category) = lower(?)
           AND abs(price - ?) < 0.01
         LIMIT 1
        ''',
        (name, category, price),
    ).fetchone()

    return row is not None


def _review_rows_from_form(form) -> list[dict]:
    names = form.getlist('item_name')
    categories = form.getlist('item_category')
    prices = form.getlist('item_price')
    descriptions = form.getlist('item_description')
    actives = form.getlist('item_active')

    total = max(len(names), len(categories), len(prices), len(descriptions), len(actives))
    rows: list[dict] = []

    for index in range(total):
        name = str(names[index]).strip() if index < len(names) else ''
        category = str(categories[index]).strip() if index < len(categories) else ''
        price = str(prices[index]).strip() if index < len(prices) else ''
        description = str(descriptions[index]).strip() if index < len(descriptions) else ''
        active = str(actives[index]).strip() if index < len(actives) else '1'

        if not any([name, category, price, description]):
            continue

        rows.append(
            {
                'name': name,
                'category': category or 'Cardápio',
                'price': price,
                'description': description,
                'active': active,
                'sort_order': index,
            }
        )

    return rows


@client_bp.route('/produtos-inicio/scannear', methods=['GET', 'POST'])
def scan_menu():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    if request.method == 'POST':
        uploads = [file for file in request.files.getlist('menu_images') if file and file.filename]

        if not uploads:
            flash('Envie pelo menos uma imagem do cardápio.', 'error')
        else:
            try:
                result = import_menu_uploads(uploads)
            except ValidationError as exc:
                flash(str(exc), 'error')
            else:
                items = result.get('items') or []

                if not items:
                    flash(
                        'Não consegui identificar produtos com preço. '
                        'Tente outra imagem ou cadastre manualmente.',
                        'warning',
                    )
                    session.pop(PENDING_MENU_IMPORT_SESSION_KEY, None)
                else:
                    session[PENDING_MENU_IMPORT_SESSION_KEY] = {
                        'items': items,
                        'processed_files': result.get('processed_files', len(uploads)),
                        'recognized_items': result.get('recognized_items', len(items)),
                        'failures': result.get('failures', []),
                    }
                    flash(f'Foram encontrados {len(items)} item(ns). Revise antes de salvar.', 'success')
                    return redirect(url_for('client.scan_menu_review'))

    return render_template('client/scan_menu.html', profile=profile, csrf=csrf_token())


@client_bp.route('/produtos-inicio/scannear/revisar')
def scan_menu_review():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    pending = _pending_menu_import()

    if not pending:
        flash('Envie as imagens do cardápio primeiro.', 'warning')
        return redirect(url_for('client.scan_menu'))

    return render_template(
        'client/scan_menu_review.html',
        profile=profile,
        items=pending['items'],
        processed_files=pending.get('processed_files', 0),
        recognized_items=pending.get('recognized_items', len(pending['items'])),
        failures=pending.get('failures', []),
        csrf=csrf_token(),
    )


@client_bp.route('/produtos-inicio/scannear/confirmar', methods=['POST'])
def scan_menu_confirm():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    pending = _pending_menu_import()

    if not pending:
        flash('Envie as imagens do cardápio primeiro.', 'warning')
        return redirect(url_for('client.scan_menu'))

    rows = _review_rows_from_form(request.form)

    if not rows:
        flash('Adicione pelo menos um produto para importar.', 'error')
        return redirect(url_for('client.scan_menu_review'))

    db = get_db()
    validated_rows = []

    for index, row in enumerate(rows, start=1):
        try:
            validated_rows.append(validate_product_payload(row))
        except ValidationError as exc:
            flash(f'Linha {index}: {exc}', 'error')
            return redirect(url_for('client.scan_menu_review'))

    created = 0
    skipped = 0

    for row in validated_rows:
        if _existing_product(db, row['name'], row['category'], row['price']):
            skipped += 1
            continue

        db.execute(
            '''
            INSERT INTO products (name, description, price, category, active, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                row['name'],
                row['description'],
                row['price'],
                row['category'],
                row['active'],
                row['sort_order'],
            ),
        )
        created += 1

    db.commit()
    session.pop(PENDING_MENU_IMPORT_SESSION_KEY, None)

    if created and skipped:
        flash(
            f'Importação concluída: {created} produto(s) cadastrado(s) e {skipped} duplicado(s) ignorado(s).',
            'success',
        )
    elif created:
        flash(f'Importação concluída: {created} produto(s) cadastrado(s).', 'success')
    else:
        flash('Nenhum produto novo foi cadastrado porque todos já existiam.', 'warning')

    return redirect(url_for('admin.products'))


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
    product = db.execute(
        'SELECT * FROM products WHERE id = ? AND active = 1',
        (product_id,),
    ).fetchone()

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
        return jsonify(
            success=True,
            message='Produto adicionado ao carrinho.',
            cart_quantity=cart_quantity,
            cart_total=cart_total,
        )

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
        return jsonify(
            success=True,
            removed=True,
            product_id=product_id,
            item_total=0.0,
            cart_total=cart_total,
            cart_quantity=cart_quantity,
        )

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
    order_id = create_order_from_cart(
        db,
        str(table_number),
        cart,
        customer_name=customer_name,
        notes=notes,
    )

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
