from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..errors import ValidationError
from ..security import csrf_token, login_required
from ..services.auth_service import (
    authenticate_admin,
    request_password_reset,
    reset_password_with_token,
    update_kitchen_password,
    update_manager_password,
    verify_manager_password,
)
from ..services.cart_service import add_item, clear_cart, find_item, get_cart, remove_item, save_cart, totals, update_item
from ..services.catalog_service import list_products, validate_product_payload
from ..services.menu_import_service import import_menu_uploads
from ..services.onboarding_service import (
    create_restaurant_account,
    get_restaurant_profile_by_token,
    get_restaurant_profile_for_admin,
    update_restaurant_profile,
)
from ..services.order_service import (
    count_open_orders_for_table,
    create_order_from_cart,
    get_order_for_payment,
    list_orders_for_table,
    mark_order_payment_error,
    update_order_payment_pending,
)
from ..services.payment_service import (
    PROVIDER_MERCADO_PAGO,
    create_pix_payment_for_order,
    payment_connection_summary,
)
from ..services.table_service import build_qr_code_data_uri, parse_table_count, save_table_count
from ..utils import normalize_text, parse_positive_int

client_bp = Blueprint('client', __name__)

PENDING_MENU_IMPORT_SESSION_KEY = 'pending_menu_import'
CLIENT_RESTAURANT_SESSION_KEY = 'client_restaurant_id'
CLIENT_RESTAURANT_TOKEN_SESSION_KEY = 'client_restaurant_token'
PUBLIC_CLIENT_MODE_SESSION_KEY = 'public_client_mode'

COUPON_CUSTOMER_RESTAURANT_SESSION_KEY = 'coupon_customer_restaurant_id'
COUPON_CUSTOMER_ID_SESSION_KEY = 'coupon_customer_id'
COUPON_CUSTOMER_USERNAME_SESSION_KEY = 'coupon_customer_username'
CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY = 'customer_after_login_target'


SERVICE_MODE_DIGITAL_MENU = 'digital_menu'
SERVICE_MODE_FULL_ORDER_PAYMENT = 'full_order_payment'


def _row_get(row, key: str, default=None):
    if not row:
        return default

    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass

    return default


def _service_mode_from_profile(profile) -> str:
    mode = _row_get(profile, 'service_mode', SERVICE_MODE_FULL_ORDER_PAYMENT)
    return mode if mode in {SERVICE_MODE_DIGITAL_MENU, SERVICE_MODE_FULL_ORDER_PAYMENT} else SERVICE_MODE_FULL_ORDER_PAYMENT


def _is_full_order_mode(profile) -> bool:
    return _service_mode_from_profile(profile) == SERVICE_MODE_FULL_ORDER_PAYMENT


def _current_restaurant_profile(db, restaurant_id: int | None = None):
    restaurant_id = restaurant_id or _client_restaurant_id()

    if not restaurant_id:
        return None

    return db.execute(
        'SELECT * FROM restaurant_profiles WHERE id = ? LIMIT 1',
        (restaurant_id,),
    ).fetchone()


def _orders_unavailable_response(profile=None, *, status_code: int = 403):
    message = 'Este restaurante utiliza o QRTotem apenas como cardápio digital. Para fazer pedidos, fale com a equipe do restaurante.'
    clear_cart(session)

    if _wants_json():
        return jsonify(success=False, message=message), status_code

    flash(message, 'warning')
    return redirect(_public_menu_url())




def _technical_payer_email(order_id: int) -> str:
    return f'cliente+pedido{int(order_id)}@qrtotem.com'

def _wants_json() -> bool:
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _payload() -> dict:
    return request.get_json(silent=True) if request.is_json else request.form.to_dict(flat=True)


def _current_table() -> str:
    return str(session.get('current_table') or '1')


def _restaurant_context() -> dict:
    context = {
        'id': session.get('restaurant_id'),
        'owner_name': session.get('restaurant_owner_name', ''),
        'age': session.get('restaurant_owner_age', ''),
        'restaurant_name': session.get('restaurant_name', ''),
        'email': session.get('restaurant_email', ''),
        'cnpj': session.get('restaurant_cnpj', ''),
        'restaurant_address': session.get('restaurant_address', ''),
        'cell_phone': session.get('restaurant_cell_phone', ''),
        'order_payment_mode': session.get('restaurant_order_payment_mode', 'pay_after'),
        'service_mode': session.get('restaurant_service_mode', SERVICE_MODE_FULL_ORDER_PAYMENT),
        'username': session.get('admin_username', ''),
        'table_count': session.get('restaurant_table_count', 0),
        'public_token': session.get('restaurant_public_token', ''),
        'slug': session.get('restaurant_slug', ''),
    }

    admin_id = session.get('admin_id')

    if admin_id:
        db = get_db()
        profile = get_restaurant_profile_for_admin(db, admin_id)

        if profile:
            context.update(
                {
                    'id': profile['id'],
                    'owner_name': profile['owner_name'],
                    'age': profile['age'],
                    'restaurant_name': profile['restaurant_name'],
                    'email': profile['email'],
                    'cnpj': profile['cnpj'],
                    'restaurant_address': profile['restaurant_address'],
                    'cell_phone': profile['cell_phone'],
                    'order_payment_mode': profile['order_payment_mode'] if 'order_payment_mode' in profile.keys() else 'pay_after',
                    'service_mode': _service_mode_from_profile(profile),
                    'username': profile['username'],
                    'table_count': profile['table_count'] if 'table_count' in profile.keys() else 0,
                    'public_token': profile['public_token'] if 'public_token' in profile.keys() else '',
                    'slug': profile['slug'] if 'slug' in profile.keys() else '',
                }
            )

    return context


def _store_admin_profile_session(account: dict) -> None:
    session['admin_logged_in'] = True
    session['admin_id'] = account['admin_id']
    session['admin_username'] = account['username']
    session['restaurant_id'] = account.get('restaurant_id')
    session['restaurant_owner_name'] = account['owner_name']
    session['restaurant_owner_age'] = account['age']
    session['restaurant_name'] = account['restaurant_name']
    session['restaurant_email'] = account['email']
    session['restaurant_cnpj'] = account['cnpj']
    session['restaurant_address'] = account['restaurant_address']
    session['restaurant_cell_phone'] = account['cell_phone']
    session['restaurant_order_payment_mode'] = account.get('order_payment_mode', 'pay_after')
    session['restaurant_service_mode'] = account.get('service_mode', SERVICE_MODE_FULL_ORDER_PAYMENT)
    session['restaurant_table_count'] = account.get('table_count', 0)
    session['restaurant_public_token'] = account.get('public_token', '')
    session['restaurant_slug'] = account.get('slug', '')


def _client_restaurant_id() -> int | None:
    value = session.get(CLIENT_RESTAURANT_SESSION_KEY) or session.get('restaurant_id')

    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _clear_coupon_customer_session() -> None:
    session.pop(COUPON_CUSTOMER_RESTAURANT_SESSION_KEY, None)
    session.pop(COUPON_CUSTOMER_ID_SESSION_KEY, None)
    session.pop(COUPON_CUSTOMER_USERNAME_SESSION_KEY, None)
    session.pop(CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY, None)


def _is_public_client_mode() -> bool:
    return bool(session.get(PUBLIC_CLIENT_MODE_SESSION_KEY))


def _set_client_restaurant(profile) -> None:
    current = session.get(CLIENT_RESTAURANT_SESSION_KEY)

    if current and str(current) != str(profile['id']):
        clear_cart(session)
        _clear_coupon_customer_session()

    session[CLIENT_RESTAURANT_SESSION_KEY] = profile['id']
    session[CLIENT_RESTAURANT_TOKEN_SESSION_KEY] = profile['public_token']
    session['client_restaurant_service_mode'] = _service_mode_from_profile(profile)

    if not _is_full_order_mode(profile):
        clear_cart(session)


def _has_coupon_access(restaurant_id: int | None) -> bool:
    if session.get('admin_logged_in') and not _is_public_client_mode():
        return True

    try:
        current_restaurant_id = int(session.get(COUPON_CUSTOMER_RESTAURANT_SESSION_KEY) or 0)
        expected_restaurant_id = int(restaurant_id or 0)
    except (TypeError, ValueError):
        return False

    return bool(expected_restaurant_id and current_restaurant_id == expected_restaurant_id and session.get(COUPON_CUSTOMER_ID_SESSION_KEY))


def _current_customer(db, restaurant_id: int | None = None):
    restaurant_id = restaurant_id or _client_restaurant_id()
    customer_id = session.get(COUPON_CUSTOMER_ID_SESSION_KEY)

    if not restaurant_id or not customer_id:
        return None

    return db.execute(
        '''
        SELECT *
          FROM customer_coupon_users
         WHERE id = ?
           AND restaurant_id = ?
         LIMIT 1
        ''',
        (customer_id, restaurant_id),
    ).fetchone()


def _coupon_entry_url() -> str:
    return url_for('client.coupon_entry')


def _public_menu_url(table_number: str | int | None = None) -> str:
    table_number = table_number or _current_table()

    if str(table_number).lower() == 'espelho' and session.get('admin_logged_in') and not _is_public_client_mode():
        return url_for('client.client_mirror')

    token = session.get(CLIENT_RESTAURANT_TOKEN_SESSION_KEY) or session.get('restaurant_public_token')

    if token:
        return url_for('client.restaurant_table_menu', public_token=token, table_number=table_number)

    return url_for('client.home')


def _after_customer_login_redirect():
    target = session.pop(CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY, None)

    if target == 'profile':
        return redirect(url_for('client.customer_profile'))

    return redirect(url_for('client.coupon_menu'))


def _client_table_redirect(table_number: int | str):
    if str(table_number).lower() == 'espelho' and session.get('admin_logged_in') and not _is_public_client_mode():
        return redirect(url_for('client.client_mirror'))

    token = session.get(CLIENT_RESTAURANT_TOKEN_SESSION_KEY) or session.get('restaurant_public_token')

    if token:
        return redirect(url_for('client.restaurant_table_menu', public_token=token, table_number=table_number, qr=1))

    return redirect(url_for('client.home'))


def _render_client_menu(
    profile,
    table_number: str,
    *,
    can_manage_table: bool = False,
    is_client_mirror: bool = False,
    is_coupon_page: bool = False,
):
    db = get_db()

    product_kind = 'coupon' if is_coupon_page else 'menu'
    products = list_products(db, profile['id'], active_only=True, kind=product_kind)

    grouped: dict[str, list] = {}
    for product in products:
        grouped.setdefault(product['category'] or 'Cardápio', []).append(product)

    can_order = _is_full_order_mode(profile)

    if not can_order:
        clear_cart(session)

    cart = get_cart(session) if can_order else []
    cart_total, cart_quantity = totals(cart)
    cart_quantities = {int(item['product_id']): int(item['quantity']) for item in cart}

    open_orders_count = 0
    if can_order and not is_client_mirror:
        open_orders_count = count_open_orders_for_table(db, profile['id'], table_number)

    current_customer = _current_customer(db, profile['id'])
    radar_enabled = bool(current_customer and current_customer['radar_enabled'])

    show_radar_flag = (
        (not session.get('admin_logged_in') or _is_public_client_mode())
        and not is_client_mirror
        and not is_coupon_page
        and str(table_number).lower() != 'espelho'
    )

    return render_template(
        'client/menu.html',
        table_number=table_number,
        grouped_products=grouped,
        cart_quantity=cart_quantity,
        cart_total=cart_total,
        cart_quantities=cart_quantities,
        open_orders_count=open_orders_count,
        csrf=csrf_token(),
        can_manage_table=can_manage_table,
        is_client_mirror=is_client_mirror,
        is_coupon_page=is_coupon_page,
        coupon_url=_coupon_entry_url(),
        menu_url=_public_menu_url(table_number),
        profile_url=url_for('client.customer_profile'),
        radar_action_url=url_for('client.toggle_radar'),
        radar_enabled=radar_enabled,
        show_radar_flag=show_radar_flag,
        can_order=can_order,
        service_mode=_service_mode_from_profile(profile),
        can_send_promotions=bool(session.get('admin_logged_in') and not _is_public_client_mode() and is_coupon_page and is_client_mirror),
        send_promotions_url=url_for('client.send_promotions'),
    )


@client_bp.route('/')
def home():
    return render_template('landing.html', csrf=csrf_token())


@client_bp.route('/entrar', methods=['GET', 'POST'])
def login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin.products'))

    if request.method == 'POST':
        username = normalize_text(request.form.get('username'))
        password = request.form.get('password', '')

        if not username or not password:
            flash('Informe usuário e senha.', 'error')
        else:
            db = get_db()
            admin = authenticate_admin(db, username, password)

            if admin:
                profile = get_restaurant_profile_for_admin(db, admin['id'])
                if not profile:
                    flash('Perfil do restaurante não encontrado.', 'error')
                    return redirect(url_for('client.signup'))

                account = {
                    'admin_id': admin['id'],
                    'username': admin['username'],
                    'restaurant_id': profile['id'],
                    'owner_name': profile['owner_name'],
                    'age': profile['age'],
                    'restaurant_name': profile['restaurant_name'],
                    'email': profile['email'],
                    'cnpj': profile['cnpj'],
                    'restaurant_address': profile['restaurant_address'],
                    'cell_phone': profile['cell_phone'],
                    'order_payment_mode': profile['order_payment_mode'] if 'order_payment_mode' in profile.keys() else 'pay_after',
                    'service_mode': _service_mode_from_profile(profile),
                    'table_count': profile['table_count'] if 'table_count' in profile.keys() else 0,
                    'public_token': profile['public_token'] if 'public_token' in profile.keys() else '',
                    'slug': profile['slug'] if 'slug' in profile.keys() else '',
                }
                session.clear()
                _store_admin_profile_session(account)
                flash('Login realizado com sucesso.', 'success')
                return redirect(url_for('admin.products'))

            flash('Usuário ou senha inválidos.', 'error')

    return render_template('admin/login.html', csrf=csrf_token())


@client_bp.route('/sair')
def logout():
    session.clear()
    flash('Você saiu da conta.', 'success')
    return redirect(url_for('client.home'))


@client_bp.route('/cadastro', methods=['GET', 'POST'])
def signup():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin.products'))

    if request.method == 'POST':
        try:
            account = create_restaurant_account(get_db(), request.form.to_dict(flat=True))
        except ValidationError as exc:
            flash(str(exc), 'error')
        else:
            session.clear()
            _store_admin_profile_session(account)
            flash('Cadastro realizado com sucesso.', 'success')
            return redirect(url_for('admin.products'))

    return render_template('client/signup.html', csrf=csrf_token())


@client_bp.route('/produtos-inicio')
@login_required
def products_start():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    return render_template('client/products_start.html', profile=profile, csrf=csrf_token())


@client_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def profile():
    profile_data = _restaurant_context()

    if not profile_data.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    if request.method == 'POST':
        try:
            updated = update_restaurant_profile(db=get_db(), admin_id=session.get('admin_id'), payload=request.form.to_dict(flat=True))
        except ValidationError as exc:
            flash(str(exc), 'error')
        else:
            session['restaurant_owner_name'] = updated['owner_name']
            session['restaurant_owner_age'] = updated['age']
            session['restaurant_name'] = updated['restaurant_name']
            session['restaurant_email'] = updated['email']
            session['restaurant_cnpj'] = updated['cnpj']
            session['restaurant_address'] = updated['restaurant_address']
            session['restaurant_cell_phone'] = updated['cell_phone']
            session['restaurant_service_mode'] = updated.get('service_mode', SERVICE_MODE_FULL_ORDER_PAYMENT)
            session['restaurant_order_payment_mode'] = updated.get('order_payment_mode', 'pay_after')

            if updated.get('service_mode') == SERVICE_MODE_DIGITAL_MENU:
                session.pop('kitchen_authorized', None)
                clear_cart(session)
            session['restaurant_slug'] = updated.get('slug', session.get('restaurant_slug', ''))
            flash('Perfil atualizado com sucesso.', 'success')
            return redirect(url_for('client.profile'))

    profile_data = _restaurant_context()
    payment_status = payment_connection_summary(get_db(), profile_data)
    return render_template('client/profile_v2.html', profile=profile_data, payment_status=payment_status, csrf=csrf_token())


@client_bp.route('/perfil/alterar-senha-usuario', methods=['POST'])
@login_required
def change_manager_password():
    try:
        update_manager_password(
            get_db(),
            session.get('admin_id'),
            request.form.get('current_password'),
            request.form.get('new_password'),
            request.form.get('new_password_confirm'),
        )
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        flash('Senha do usuário alterada com sucesso.', 'success')

    return redirect(url_for('client.profile'))


@client_bp.route('/perfil/alterar-senha-cozinha', methods=['POST'])
@login_required
def change_kitchen_password():
    try:
        update_kitchen_password(
            get_db(),
            session.get('admin_id'),
            request.form.get('manager_password'),
            request.form.get('new_kitchen_password'),
            request.form.get('new_kitchen_password_confirm'),
        )
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        session.pop('kitchen_authorized', None)
        flash('Senha da cozinha alterada com sucesso.', 'success')

    return redirect(url_for('client.profile'))


@client_bp.route('/esqueci-senha', methods=['GET', 'POST'])
def forgot_password():
    reset_url = None

    if request.method == 'POST':
        identifier = normalize_text(request.form.get('identifier'))
        token = request_password_reset(get_db(), identifier)

        if token:
            reset_url = url_for('client.reset_password', token=token, _external=True)

        flash('Se os dados estiverem cadastrados, será gerado um link de recuperação válido por 30 minutos.', 'success')

    return render_template('client/forgot_password.html', csrf=csrf_token(), reset_url=reset_url)


@client_bp.route('/recuperar-senha/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'POST':
        try:
            reset_password_with_token(
                get_db(),
                token,
                request.form.get('password'),
                request.form.get('password_confirm'),
            )
        except ValueError as exc:
            flash(str(exc), 'error')
        else:
            flash('Senha alterada com sucesso. Faça login com a nova senha.', 'success')
            return redirect(url_for('admin.login'))

    return render_template('client/reset_password.html', csrf=csrf_token(), token=token)


@client_bp.route('/mesas', methods=['GET', 'POST'])
@login_required
def tables_setup():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    db = get_db()

    if request.method == 'POST':
        try:
            table_count = parse_table_count(request.form.get('table_count'))
            save_table_count(db, session.get('admin_id'), table_count)
        except ValidationError as exc:
            flash(str(exc), 'error')
        else:
            session['restaurant_table_count'] = table_count
            profile['table_count'] = table_count
            flash(f'{table_count} QR Code(s) de mesa gerado(s) com sucesso.', 'success')
            return redirect(url_for('client.tables_setup'))

    table_count = int(profile.get('table_count') or 0)
    table_cards = []

    for table_number in range(1, table_count + 1):
        table_url = url_for(
            'client.restaurant_table_menu',
            public_token=profile.get('public_token'),
            table_number=table_number,
            qr=1,
            _external=True,
        )

        table_cards.append(
            {
                'number': table_number,
                'url': table_url,
                'qr_data_uri': build_qr_code_data_uri(table_url, table_number),
            }
        )

    return render_template(
        'client/tables_setup.html',
        profile=profile,
        table_count=table_count,
        table_cards=table_cards,
        csrf=csrf_token(),
    )


@client_bp.route('/produtos-inicio/manual')
@login_required
def products_manual_redirect():
    return redirect(url_for('admin.products'))


@client_bp.route('/produtos-inicio/scannear', methods=['GET', 'POST'])
@login_required
def scan_menu():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    if request.method == 'POST':
        files = request.files.getlist('images')

        if not files or not any(file and file.filename for file in files):
            flash('Envie ao menos uma foto do cardápio.', 'error')
        else:
            try:
                imported = import_menu_uploads(files)
            except ValidationError as exc:
                flash(str(exc), 'error')
            except Exception:
                flash('Não foi possível analisar as imagens. Tente novamente com fotos mais nítidas.', 'error')
            else:
                if not imported:
                    flash(
                        'Não consegui identificar produtos com preço. Tente outra imagem ou cadastre manualmente.',
                        'warning',
                    )
                    session.pop(PENDING_MENU_IMPORT_SESSION_KEY, None)
                else:
                    session[PENDING_MENU_IMPORT_SESSION_KEY] = {
                        'products': imported,
                    }
                    flash(f'{len(imported)} produto(s) identificados. Revise antes de confirmar.', 'success')
                    return redirect(url_for('client.scan_menu_review'))

    return render_template('client/scan_menu.html', profile=profile, csrf=csrf_token())


@client_bp.route('/produtos-inicio/scannear/revisar')
@login_required
def scan_menu_review():
    profile = _restaurant_context()

    if not profile.get('restaurant_name'):
        return redirect(url_for('client.signup'))

    pending = session.get(PENDING_MENU_IMPORT_SESSION_KEY) or {}
    products = pending.get('products') or []

    if not products:
        flash('Nenhum produto pendente para revisar.', 'warning')
        return redirect(url_for('client.scan_menu'))

    return render_template(
        'client/scan_menu_review.html',
        profile=profile,
        products=products,
        csrf=csrf_token(),
    )


@client_bp.route('/produtos-inicio/scannear/confirmar', methods=['POST'])
@login_required
def scan_menu_confirm():
    profile = _restaurant_context()
    restaurant_id = profile.get('id')

    if not profile.get('restaurant_name') or not restaurant_id:
        return redirect(url_for('client.signup'))

    pending = session.get(PENDING_MENU_IMPORT_SESSION_KEY) or {}
    imported = pending.get('products') or []

    if not imported:
        flash('Adicione pelo menos um produto para importar.', 'error')
        return redirect(url_for('client.scan_menu_review'))

    products = []
    errors = []

    for index, item in enumerate(imported):
        prefix = f'products[{index}]'
        active_key = f'{prefix}[active]'

        if request.form.get(active_key) != '1':
            continue

        products.append(
            {
                'name': request.form.get(f'{prefix}[name]'),
                'category': request.form.get(f'{prefix}[category]'),
                'price': request.form.get(f'{prefix}[price]'),
                'description': request.form.get(f'{prefix}[description]'),
                'active': '1',
            }
        )

    if not products:
        flash('Adicione pelo menos um produto para importar.', 'error')
        return redirect(url_for('client.scan_menu_review'))

    db = get_db()
    created = 0
    skipped = 0

    existing_names = {
        row['name'].strip().lower()
        for row in db.execute(
            'SELECT name FROM products WHERE restaurant_id = ? AND kind = ?',
            (restaurant_id, 'menu'),
        ).fetchall()
    }

    try:
        for product in products:
            try:
                payload = validate_product_payload(product)
            except ValidationError as exc:
                errors.append(f"{product.get('name') or 'Produto'}: {exc}")
                continue

            normalized_name = payload['name'].strip().lower()

            if normalized_name in existing_names:
                skipped += 1
                continue

            max_sort = db.execute(
                'SELECT COALESCE(MAX(sort_order), 0) FROM products WHERE restaurant_id = ? AND kind = ?',
                (restaurant_id, 'menu'),
            ).fetchone()[0]

            db.execute(
                '''
                INSERT INTO products (
                    restaurant_id,
                    name,
                    category,
                    price,
                    description,
                    active,
                    sort_order,
                    kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    restaurant_id,
                    payload['name'],
                    payload['category'],
                    payload['price'],
                    payload['description'],
                    payload['active'],
                    max_sort + created + 1,
                    'menu',
                ),
            )
            existing_names.add(normalized_name)
            created += 1

        db.commit()
    except Exception:
        db.rollback()
        flash('Erro ao importar produtos. Tente novamente.', 'error')
        return redirect(url_for('client.scan_menu_review'))

    session.pop(PENDING_MENU_IMPORT_SESSION_KEY, None)

    if errors:
        flash('Alguns produtos não foram importados: ' + ' | '.join(errors[:4]), 'warning')

    if created and skipped:
        flash(
            f'Importação concluída: {created} produto(s) cadastrado(s) e {skipped} duplicado(s) ignorado(s).',
            'success',
        )
    elif created:
        flash(f'Importação concluída: {created} produto(s) cadastrado(s).', 'success')
    elif skipped:
        flash('Nenhum produto novo foi cadastrado porque todos já existiam.', 'warning')
    else:
        flash('Nenhum produto foi importado.', 'warning')

    return redirect(url_for('admin.products'))


@client_bp.route('/cliente-espelho')
@login_required
def client_mirror():
    profile = _restaurant_context()

    if not profile.get('restaurant_name') or not profile.get('id'):
        return redirect(url_for('client.signup'))

    session.pop(PUBLIC_CLIENT_MODE_SESSION_KEY, None)
    session['current_table'] = 'espelho'
    _set_client_restaurant(profile)

    return _render_client_menu(
        profile,
        'Espelho',
        can_manage_table=False,
        is_client_mirror=True,
        is_coupon_page=False,
    )


@client_bp.route('/cupons-espelho')
@login_required
def coupon_mirror():
    profile = _restaurant_context()

    if not profile.get('restaurant_name') or not profile.get('id'):
        return redirect(url_for('client.signup'))

    session.pop(PUBLIC_CLIENT_MODE_SESSION_KEY, None)
    session['current_table'] = 'espelho'
    _set_client_restaurant(profile)

    return _render_client_menu(
        profile,
        'Espelho',
        can_manage_table=False,
        is_client_mirror=True,
        is_coupon_page=True,
    )


@client_bp.route('/promocoes/enviar', methods=['GET', 'POST'])
@login_required
def send_promotions():
    profile = _restaurant_context()

    if not profile.get('restaurant_name') or not profile.get('id'):
        return redirect(url_for('client.signup'))

    db = get_db()
    radar_count = db.execute(
        '''
        SELECT COUNT(*)
          FROM customer_coupon_users
         WHERE restaurant_id = ?
           AND radar_enabled = 1
        ''',
        (profile['id'],),
    ).fetchone()[0]

    if request.method == 'POST':
        title = normalize_text(request.form.get('title'))
        message = normalize_text(request.form.get('message'))

        manager_password = str(request.form.get('manager_password') or '').strip()

        if not verify_manager_password(db, manager_password, admin_id=session.get('admin_id')):
            flash('Senha do usuário inválida. Promoção não enviada.', 'error')
        elif not title or not message:
            flash('Informe o título e a mensagem da promoção.', 'error')
        else:
            files_count = len([file for file in request.files.getlist('images') if file and file.filename])
            flash(
                f'Prévia criada com sucesso. Nenhum envio real foi feito ainda. Público no radar: {radar_count} cliente(s). Imagens anexadas: {files_count}.',
                'success',
            )
            return redirect(url_for('client.send_promotions'))

    return render_template(
        'client/promo_send.html',
        profile=profile,
        radar_count=radar_count,
        csrf=csrf_token(),
    )


@client_bp.route('/r/<public_token>/mesa/<table_number>')
def restaurant_table_menu(public_token, table_number):
    table_number = str(parse_positive_int(table_number, default=1, minimum=1, maximum=999))
    db = get_db()
    profile = get_restaurant_profile_by_token(db, public_token)

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('client.home'))

    session['current_table'] = table_number
    _set_client_restaurant(profile)

    if request.args.get('qr') == '1':
        _clear_coupon_customer_session()

    session[PUBLIC_CLIENT_MODE_SESSION_KEY] = True

    return _render_client_menu(
        profile,
        table_number,
        can_manage_table=False,
        is_client_mirror=False,
        is_coupon_page=False,
    )


@client_bp.route('/cupons-cliente')
def coupon_entry():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if session.get('admin_logged_in') and not _is_public_client_mode():
        return redirect(url_for('client.coupon_mirror'))

    if _has_coupon_access(restaurant_id):
        return redirect(url_for('client.coupon_menu'))

    return redirect(url_for('client.coupon_login'))


@client_bp.route('/cupons-cliente/login', methods=['GET', 'POST'])
def coupon_login():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if session.get('admin_logged_in') and not _is_public_client_mode():
        return redirect(url_for('client.coupon_mirror'))

    if request.method == 'POST':
        login_identifier = normalize_text(request.form.get('username')).lower()

        if not login_identifier:
            flash('Informe seu usuário ou e-mail.', 'error')
        else:
            db = get_db()
            customer = db.execute(
                '''
                SELECT *
                  FROM customer_coupon_users
                 WHERE restaurant_id = ?
                   AND (
                        lower(username) = lower(?)
                        OR lower(email) = lower(?)
                   )
                 LIMIT 1
                ''',
                (restaurant_id, login_identifier, login_identifier),
            ).fetchone()

            if customer:
                session[COUPON_CUSTOMER_RESTAURANT_SESSION_KEY] = restaurant_id
                session[COUPON_CUSTOMER_ID_SESSION_KEY] = customer['id']
                session[COUPON_CUSTOMER_USERNAME_SESSION_KEY] = customer['username']
                flash('Acesso liberado.', 'success')
                return _after_customer_login_redirect()

            flash('Usuário ou e-mail não encontrado. Faça seu cadastro para acessar os cupons e seu perfil.', 'error')

    return render_template(
        'client/coupon_login.html',
        csrf=csrf_token(),
        signup_url=url_for('client.coupon_signup'),
        menu_url=_public_menu_url(),
    )


@client_bp.route('/cupons-cliente/cadastro', methods=['GET', 'POST'])
def coupon_signup():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if request.method == 'POST':
        name = normalize_text(request.form.get('name'))
        username = normalize_text(request.form.get('username'))
        cell_phone = normalize_text(request.form.get('cell_phone'))
        email = normalize_text(request.form.get('email'))
        cep = normalize_text(request.form.get('cep'))
        receive_whatsapp = 1 if request.form.get('receive_whatsapp') else 0
        receive_email = 1 if request.form.get('receive_email') else 0

        if not all([name, username, cell_phone, email, cep]):
            flash('Preencha todos os campos obrigatórios.', 'error')
        elif '@' not in email or '.' not in email:
            flash('Informe um e-mail válido.', 'error')
        else:
            db = get_db()
            exists = db.execute(
                '''
                SELECT id
                  FROM customer_coupon_users
                 WHERE restaurant_id = ?
                   AND (
                        lower(username) = lower(?)
                        OR lower(email) = lower(?)
                   )
                 LIMIT 1
                ''',
                (restaurant_id, username, email.lower()),
            ).fetchone()

            if exists:
                flash('Este usuário ou email já existe. Faça login para acessar os cupons ou seu perfil.', 'warning')
                return redirect(url_for('client.coupon_login'))

            db.execute(
                '''
                INSERT INTO customer_coupon_users (
                    restaurant_id,
                    name,
                    username,
                    cell_phone,
                    email,
                    cep,
                    receive_whatsapp,
                    receive_email
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    restaurant_id,
                    name,
                    username,
                    cell_phone,
                    email,
                    cep,
                    receive_whatsapp,
                    receive_email,
                ),
            )
            db.commit()
            flash('Cadastro realizado com sucesso. Agora informe seu usuário para acessar.', 'success')
            return redirect(url_for('client.coupon_login'))

    return render_template(
        'client/coupon_signup.html',
        csrf=csrf_token(),
        login_url=url_for('client.coupon_login'),
        menu_url=_public_menu_url(),
    )


@client_bp.route('/cupons-cliente/cardapio')
def coupon_menu():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if session.get('admin_logged_in') and not _is_public_client_mode():
        return redirect(url_for('client.coupon_mirror'))

    if not _has_coupon_access(restaurant_id):
        return redirect(url_for('client.coupon_login'))

    db = get_db()

    profile = None
    token = session.get(CLIENT_RESTAURANT_TOKEN_SESSION_KEY) or session.get('restaurant_public_token')

    if token:
        profile = get_restaurant_profile_by_token(db, token)

    if not profile:
        profile = db.execute(
            'SELECT * FROM restaurant_profiles WHERE id = ?',
            (restaurant_id,),
        ).fetchone()

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('client.home'))

    return _render_client_menu(
        profile,
        _current_table(),
        can_manage_table=False,
        is_client_mirror=False,
        is_coupon_page=True,
    )


@client_bp.route('/cliente/perfil', methods=['GET', 'POST'])
def customer_profile():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if session.get('admin_logged_in') and not _is_public_client_mode():
        flash('O perfil do cliente é acessado pelo cliente da mesa.', 'warning')
        return redirect(url_for('client.client_mirror'))

    if not _has_coupon_access(restaurant_id):
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        flash('Informe seu usuário para acessar ou criar seu perfil.', 'warning')
        return redirect(url_for('client.coupon_login'))

    db = get_db()
    customer = _current_customer(db, restaurant_id)

    if not customer:
        session.pop(COUPON_CUSTOMER_ID_SESSION_KEY, None)
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        flash('Faça login novamente para acessar seu perfil.', 'warning')
        return redirect(url_for('client.coupon_login'))

    if request.method == 'POST':
        name = normalize_text(request.form.get('name'))
        username = normalize_text(request.form.get('username'))
        cell_phone = normalize_text(request.form.get('cell_phone'))
        email = normalize_text(request.form.get('email'))
        cep = normalize_text(request.form.get('cep'))
        receive_whatsapp = 1 if request.form.get('receive_whatsapp') else 0
        receive_email = 1 if request.form.get('receive_email') else 0

        if not all([name, username, cell_phone, email, cep]):
            flash('Preencha todos os campos obrigatórios.', 'error')
        elif '@' not in email or '.' not in email:
            flash('Informe um e-mail válido.', 'error')
        else:
            duplicated = db.execute(
                '''
                SELECT id
                  FROM customer_coupon_users
                 WHERE restaurant_id = ?
                   AND lower(username) = lower(?)
                   AND id <> ?
                 LIMIT 1
                ''',
                (restaurant_id, username, customer['id']),
            ).fetchone()

            if duplicated:
                flash('Este usuário já está em uso neste restaurante.', 'error')
            else:
                old_username = customer['username']

                db.execute(
                    '''
                    UPDATE customer_coupon_users
                       SET name = ?,
                           username = ?,
                           cell_phone = ?,
                           email = ?,
                           cep = ?,
                           receive_whatsapp = ?,
                           receive_email = ?,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                       AND restaurant_id = ?
                    ''',
                    (
                        name,
                        username,
                        cell_phone,
                        email,
                        cep,
                        receive_whatsapp,
                        receive_email,
                        customer['id'],
                        restaurant_id,
                    ),
                )

                if old_username != username:
                    db.execute(
                        '''
                        UPDATE customer_coupon_users
                           SET username = ?
                         WHERE lower(username) = lower(?)
                        ''',
                        (username, old_username),
                    )

                db.commit()
                session[COUPON_CUSTOMER_USERNAME_SESSION_KEY] = username
                flash('Perfil atualizado com sucesso.', 'success')
                return redirect(url_for('client.customer_profile'))

    customer = _current_customer(db, restaurant_id)
    username = session.get(COUPON_CUSTOMER_USERNAME_SESSION_KEY) or customer['username']

    radar_restaurants = db.execute(
        '''
        SELECT ccu.id,
               ccu.restaurant_id,
               ccu.radar_enabled,
               ccu.receive_whatsapp,
               ccu.receive_email,
               rp.restaurant_name,
               rp.restaurant_address
          FROM customer_coupon_users ccu
          JOIN restaurant_profiles rp ON rp.id = ccu.restaurant_id
         WHERE lower(ccu.username) = lower(?)
         ORDER BY ccu.radar_enabled DESC, rp.restaurant_name ASC
        ''',
        (username,),
    ).fetchall()

    return render_template(
        'client/customer_profile.html',
        customer=customer,
        radar_restaurants=radar_restaurants,
        menu_url=_public_menu_url(),
        csrf=csrf_token(),
    )


@client_bp.route('/cliente/perfil/radar/<int:customer_id>/toggle', methods=['POST'])
def customer_profile_toggle_radar(customer_id):
    restaurant_id = _client_restaurant_id()

    if not restaurant_id or not _has_coupon_access(restaurant_id):
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        return redirect(url_for('client.coupon_login'))

    username = session.get(COUPON_CUSTOMER_USERNAME_SESSION_KEY)

    if not username:
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        return redirect(url_for('client.coupon_login'))

    db = get_db()
    row = db.execute(
        '''
        SELECT id, radar_enabled
          FROM customer_coupon_users
         WHERE id = ?
           AND lower(username) = lower(?)
         LIMIT 1
        ''',
        (customer_id, username),
    ).fetchone()

    if not row:
        flash('Restaurante não encontrado no seu perfil.', 'error')
        return redirect(url_for('client.customer_profile'))

    new_value = 0 if row['radar_enabled'] else 1

    db.execute(
        '''
        UPDATE customer_coupon_users
           SET radar_enabled = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        ''',
        (new_value, customer_id),
    )
    db.commit()

    flash('Radar atualizado com sucesso.', 'success')
    return redirect(url_for('client.customer_profile'))


@client_bp.route('/cliente/radar/toggle', methods=['POST'])
def toggle_radar():
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    if session.get('admin_logged_in') and not _is_public_client_mode():
        return redirect(url_for('client.client_mirror'))

    if not _has_coupon_access(restaurant_id):
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        flash('Informe seu usuário para deixar este restaurante no radar.', 'warning')
        return redirect(url_for('client.coupon_login'))

    db = get_db()
    customer = _current_customer(db, restaurant_id)

    if not customer:
        session[CUSTOMER_AFTER_LOGIN_TARGET_SESSION_KEY] = 'profile'
        flash('Faça login novamente para atualizar seu radar.', 'warning')
        return redirect(url_for('client.coupon_login'))

    radar_enabled = 1 if request.form.get('radar_enabled') else 0

    db.execute(
        '''
        UPDATE customer_coupon_users
           SET radar_enabled = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (radar_enabled, customer['id'], restaurant_id),
    )
    db.commit()

    if radar_enabled:
        flash('Restaurante adicionado ao seu radar.', 'success')
    else:
        flash('Restaurante removido do seu radar.', 'success')

    return redirect(_public_menu_url())


@client_bp.route('/mesa/<table_number>')
def table_menu(table_number):
    return _client_table_redirect(parse_positive_int(table_number, default=1, minimum=1, maximum=999))


@client_bp.route('/mesa/editar', methods=['POST'])
def edit_table():
    data = _payload()
    new_table = str(data.get('table_number') or '').strip()
    manager_password = str(data.get('manager_password') or '').strip()
    restaurant_id = _client_restaurant_id()

    if not new_table:
        message = 'Informe o número da mesa.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return _client_table_redirect(_current_table())

    try:
        table_number = parse_positive_int(new_table, minimum=1, maximum=999)
    except (TypeError, ValueError):
        message = 'Número de mesa inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return _client_table_redirect(_current_table())

    if not manager_password:
        message = 'Informe a senha do gerente.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return _client_table_redirect(_current_table())

    db = get_db()
    profile = db.execute(
        'SELECT admin_id FROM restaurant_profiles WHERE id = ?',
        (restaurant_id,),
    ).fetchone()

    if not profile or not verify_manager_password(db, manager_password, admin_id=profile['admin_id']):
        message = 'Senha do gerente inválida.'
        if _wants_json():
            return jsonify(success=False, message=message), 401
        flash(message, 'error')
        return _client_table_redirect(_current_table())

    session['current_table'] = str(table_number)

    if _wants_json():
        token = session.get(CLIENT_RESTAURANT_TOKEN_SESSION_KEY) or session.get('restaurant_public_token')
        return jsonify(
            success=True,
            message='Mesa atualizada com sucesso.',
            table_number=table_number,
            redirect_url=url_for('client.restaurant_table_menu', public_token=token, table_number=table_number, qr=1),
        )

    flash('Mesa atualizada com sucesso.', 'success')
    return _client_table_redirect(table_number)


@client_bp.route('/carrinho')
def cart():
    cart_items = get_cart(session)
    restaurant_id = _client_restaurant_id()
    table_number = _current_table()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    db = get_db()
    profile = _current_restaurant_profile(db, restaurant_id)

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    cart_total, cart_quantity = totals(cart_items)

    return render_template(
        'client/cart.html',
        cart=cart_items,
        cart_total=cart_total,
        cart_quantity=cart_quantity,
        table_number=table_number,
        csrf=csrf_token(),
        menu_url=_public_menu_url(table_number),
    )

@client_bp.route('/carrinho/adicionar', methods=['POST'])
def add_to_cart():
    data = _payload()

    try:
        product_id = parse_positive_int(data.get('product_id'), minimum=1)
        quantity = parse_positive_int(data.get('quantity'), minimum=1, maximum=99)
    except (TypeError, ValueError):
        message = 'Produto ou quantidade inválidos.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return _client_table_redirect(_current_table())

    db = get_db()
    restaurant_id = _client_restaurant_id()
    profile = _current_restaurant_profile(db, restaurant_id)

    if not profile:
        message = 'Restaurante não identificado.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    product = None
    if restaurant_id:
        product = db.execute(
            '''
            SELECT *
              FROM products
             WHERE id = ?
               AND restaurant_id = ?
               AND active = 1
             LIMIT 1
            ''',
            (product_id, restaurant_id),
        ).fetchone()

    if not product:
        message = 'Produto indisponível.'
        if _wants_json():
            return jsonify(success=False, message=message), 404
        flash(message, 'error')
        return _client_table_redirect(_current_table())

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
    return _client_table_redirect(_current_table())


@client_bp.route('/carrinho/atualizar', methods=['POST'])
def update_cart():
    data = _payload()

    try:
        product_id = parse_positive_int(data.get('product_id'), minimum=1)
        quantity = parse_positive_int(data.get('quantity'), default=0, minimum=0, maximum=99)
    except (TypeError, ValueError):
        message = 'Produto inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    db = get_db()
    profile = _current_restaurant_profile(db)

    if not profile:
        message = 'Restaurante não identificado.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    cart = get_cart(session)
    old_quantity = 0
    existing = find_item(cart, product_id)

    if existing:
        old_quantity = int(existing['quantity'])

    cart, _ = update_item(cart, product_id, quantity)
    save_cart(session, cart)    
    cart_total, cart_quantity = totals(cart)

    if _wants_json():
        return jsonify(
            success=True,
            message='Carrinho atualizado.',
            quantity=quantity,
            old_quantity=old_quantity,
            removed=quantity == 0,
            cart_quantity=cart_quantity,
            cart_total=cart_total,
        )

    flash('Carrinho atualizado.', 'success')
    return redirect(url_for('client.cart'))


@client_bp.route('/carrinho/excluir', methods=['POST'])
def remove_from_cart():
    data = _payload()

    try:
        product_id = parse_positive_int(data.get('product_id'), minimum=1)
    except (TypeError, ValueError):
        message = 'Produto inválido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    db = get_db()
    profile = _current_restaurant_profile(db)

    if not profile:
        message = 'Restaurante não identificado.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    cart = get_cart(session)
    before_count = len(cart)
    cart = remove_item(cart, product_id)
    removed = len(cart) < before_count
    save_cart(session, cart)    
    cart_total, cart_quantity = totals(cart)

    if _wants_json():
        return jsonify(
            success=True,
            message='Produto removido.' if removed else 'Produto não estava no carrinho.',
            removed=removed,
            cart_quantity=cart_quantity,
            cart_total=cart_total,
        )

    flash('Produto removido.' if removed else 'Produto não estava no carrinho.', 'success')
    return redirect(url_for('client.cart'))


@client_bp.route('/pedido/finalizar', methods=['POST'])
def finalize_order():
    cart = get_cart(session)
    table_number = _current_table()
    restaurant_id = _client_restaurant_id()

    if not restaurant_id:
        message = 'Restaurante não identificado.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.home'))

    db = get_db()
    profile = _current_restaurant_profile(db, restaurant_id)

    if not profile:
        message = 'Restaurante não encontrado.'
        if _wants_json():
            return jsonify(success=False, message=message), 404
        flash(message, 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    payload = _payload() or {}
    notes = normalize_text(payload.get('notes'))
    customer_name = f'Cliente mesa {table_number}'

    payment_status = payment_connection_summary(db, profile)
    if payment_status.get('status') != 'connected':
        message = 'Este restaurante ainda não conectou o Mercado Pago. Avise o responsável antes de finalizar o pedido.'
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    order_id = None

    try:
        order_id = create_order_from_cart(
            db,
            restaurant_id,
            table_number,
            cart,
            customer_name,
            notes,
            payment_required=True,
            payment_status='pending',
            payment_provider=PROVIDER_MERCADO_PAGO,
        )

        order = get_order_for_payment(db, restaurant_id, order_id, table_number)
        external_reference = f'qrtotem_order_{order_id}'
        customer_email = _technical_payer_email(order_id)
        pix_payment = create_pix_payment_for_order(
            db,
            restaurant_id=restaurant_id,
            order_id=order_id,
            amount=float(order['total_amount']),
            customer_name=customer_name,
            customer_email=customer_email,
            external_reference=external_reference,
        )
        update_order_payment_pending(
            db,
            restaurant_id=restaurant_id,
            order_id=order_id,
            provider=PROVIDER_MERCADO_PAGO,
            external_id=pix_payment['id'],
            external_reference=pix_payment['external_reference'],
            qr_code=pix_payment['qr_code'],
            qr_code_base64=pix_payment.get('qr_code_base64', ''),
            ticket_url=pix_payment.get('ticket_url', ''),
        )
    except (ValidationError, RuntimeError) as exc:
        message = str(exc)
        if order_id:
            mark_order_payment_error(db, restaurant_id=restaurant_id, order_id=order_id, error=message)
        if _wants_json():
            return jsonify(success=False, message=message), 400
        flash(message, 'error')
        return redirect(url_for('client.cart'))

    clear_cart(session)
    payment_url = url_for('client.payment_order', order_id=order_id)

    if _wants_json():
        return jsonify(
            success=True,
            message=f'Pedido #{order_id} criado. Pague o Pix para enviar à cozinha.',
            redirect_url=payment_url,
            payment_url=payment_url,
        )

    flash(f'Pedido #{order_id} criado. Pague o Pix para enviar à cozinha.', 'success')
    return redirect(payment_url)



@client_bp.route('/pagamento/pedido/<int:order_id>')
def payment_order(order_id: int):
    restaurant_id = _client_restaurant_id()
    table_number = _current_table()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    db = get_db()
    profile = _current_restaurant_profile(db, restaurant_id)

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    order = get_order_for_payment(db, restaurant_id, order_id, table_number)

    if not order:
        flash('Pedido não encontrado para esta mesa.', 'error')
        return redirect(_public_menu_url(table_number))

    if not int(_row_get(order, 'payment_required', 0) or 0):
        flash('Este pedido não possui pagamento Pix pendente.', 'warning')
        return redirect(url_for('client.order_history'))

    return render_template(
        'client/payment.html',
        order=order,
        table_number=table_number,
        menu_url=_public_menu_url(table_number),
        orders_url=url_for('client.order_history'),
        csrf=csrf_token(),
    )


@client_bp.route('/pedidos')
def order_history():
    restaurant_id = _client_restaurant_id()
    table_number = _current_table()

    if not restaurant_id:
        flash('Restaurante não identificado.', 'error')
        return redirect(url_for('client.home'))

    db = get_db()
    profile = _current_restaurant_profile(db, restaurant_id)

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('client.home'))

    if not _is_full_order_mode(profile):
        return _orders_unavailable_response(profile)

    orders = list_orders_for_table(db, restaurant_id, table_number)

    return render_template('client/orders.html', orders=orders, menu_url=_public_menu_url(), csrf=csrf_token())
