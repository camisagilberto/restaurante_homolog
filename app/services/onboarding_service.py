from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime
from typing import Any

from werkzeug.security import generate_password_hash

from ..errors import ValidationError
from ..utils import normalize_text


def _only_digits(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _validate_email(email: str) -> str:
    if '@' not in email or '.' not in email.split('@')[-1]:
        raise ValidationError('Informe um e-mail válido.')
    return email




def find_referrer_customer(db, identifier: str):
    """Localiza o cliente indicado por e-mail ou usuário.

    Usuários de clientes são únicos por restaurante, não globalmente. Por isso,
    quando o identificador não for e-mail e houver mais de um usuário igual,
    o cadastro do restaurante deve pedir o e-mail do cliente indicado.
    """
    value = normalize_text(identifier).lower()
    if not value:
        return None, None

    if '@' in value:
        row = db.execute(
            """
            SELECT *
              FROM customer_coupon_users
             WHERE lower(email) = lower(?)
             ORDER BY datetime(created_at) DESC, id DESC
             LIMIT 1
            """,
            (value,),
        ).fetchone()
        return row, None

    rows = db.execute(
        """
        SELECT *
          FROM customer_coupon_users
         WHERE lower(username) = lower(?)
         ORDER BY datetime(created_at) DESC, id DESC
         LIMIT 2
        """,
        (value,),
    ).fetchall()

    if not rows:
        return None, None
    if len(rows) > 1:
        return None, 'Encontramos mais de um cliente com esse usuário. Use o e-mail do cliente indicado.'
    return rows[0], None


def _validate_age(value: Any) -> int:
    try:
        age = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError('Informe uma idade válida.')

    if age < 1 or age > 120:
        raise ValidationError('Informe uma idade válida.')

    return age




def _validate_service_mode(value: Any, *, required: bool = True) -> str:
    mode = normalize_text(value)

    if not mode and not required:
        return 'full_order_payment'

    if mode not in {'digital_menu', 'full_order_payment'}:
        raise ValidationError('Escolha entre cardápio digital ou cardápio + pedido + pagamento.')

    return mode

def _validate_order_payment_mode(value: Any, *, required: bool = True) -> str:
    mode = normalize_text(value)

    if not mode and not required:
        return 'pay_after'

    if mode not in {'pay_before', 'pay_after'}:
        raise ValidationError('Escolha se o pagamento será antes ou depois de finalizar o pedido.')

    return mode


def _slugify(value: str) -> str:
    text = str(value or '').strip().lower()
    table = str.maketrans(
        'áàãâäéèêëíìîïóòõôöúùûüçñ',
        'aaaaaeeeeiiiiooooouuuucn',
    )
    text = text.translate(table)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text or 'restaurante'


def _unique_slug(db, base: str, current_id: int | None = None) -> str:
    base_slug = _slugify(base)
    candidate = base_slug
    counter = 2

    while True:
        if current_id:
            row = db.execute(
                'SELECT id FROM restaurant_profiles WHERE slug = ? AND id <> ?',
                (candidate, current_id),
            ).fetchone()
        else:
            row = db.execute(
                'SELECT id FROM restaurant_profiles WHERE slug = ?',
                (candidate,),
            ).fetchone()

        if not row:
            return candidate

        candidate = f'{base_slug}-{counter}'
        counter += 1


def _unique_token(db) -> str:
    while True:
        token = secrets.token_urlsafe(10).replace('-', '').replace('_', '')[:12]
        row = db.execute(
            'SELECT id FROM restaurant_profiles WHERE public_token = ?',
            (token,),
        ).fetchone()

        if not row:
            return token


def validate_onboarding_payload(payload: dict[str, Any]) -> dict[str, Any]:
    owner_name = normalize_text(payload.get('owner_name'))
    restaurant_name = normalize_text(payload.get('restaurant_name'))
    restaurant_address = _only_digits(payload.get('restaurant_address'))
    service_mode = _validate_service_mode(payload.get('service_mode'), required=False)
    order_payment_mode = _validate_order_payment_mode(payload.get('order_payment_mode'), required=False)
    username = normalize_text(payload.get('username'))
    password = str(payload.get('password') or '').strip()
    password_confirm = str(payload.get('password_confirm') or '').strip()
    indicated_by = normalize_text(payload.get('indicated_by'))

    email = normalize_text(payload.get('email')).lower()
    cnpj = _only_digits(payload.get('cnpj'))
    cell_phone = _only_digits(payload.get('cell_phone'))
    age = _validate_age(payload.get('age'))

    if not owner_name:
        raise ValidationError('Informe o nome.')
    if not restaurant_name:
        raise ValidationError('Informe o nome do restaurante.')
    if len(restaurant_address) != 8:
        raise ValidationError('Informe um CEP válido com 8 números.')
    if not username:
        raise ValidationError('Informe o usuário.')
    if len(username) < 3:
        raise ValidationError('O usuário deve ter pelo menos 3 caracteres.')
    if not password:
        raise ValidationError('Informe a senha.')
    if len(password) < 8:
        raise ValidationError('A senha deve ter pelo menos 8 caracteres.')
    if password != password_confirm:
        raise ValidationError('A confirmação de senha não confere.')
    if not email:
        raise ValidationError('Informe o e-mail.')

    _validate_email(email)

    if cell_phone and len(cell_phone) < 10:
        raise ValidationError('Informe um celular válido ou deixe o campo em branco.')

    return {
        'owner_name': owner_name,
        'age': age,
        'email': email,
        'restaurant_name': restaurant_name,
        'cnpj': cnpj,
        'restaurant_address': restaurant_address,
        'cell_phone': cell_phone,
        'service_mode': service_mode,
        'order_payment_mode': order_payment_mode,
        'username': username,
        'password': password,
        'indicated_by': indicated_by,
    }


def create_restaurant_account(db, payload: dict[str, Any]) -> dict[str, Any]:
    data = validate_onboarding_payload(payload)
    password_hash = generate_password_hash(data['password'])
    kitchen_password_hash = password_hash

    referrer = None
    if data.get('indicated_by'):
        referrer, referral_error = find_referrer_customer(db, data['indicated_by'])
        if referral_error:
            raise ValidationError(referral_error)
        if not referrer:
            raise ValidationError('Cliente ainda não criado.')

    try:
        cursor = db.execute(
            'INSERT INTO admins (username, password_hash, kitchen_password_hash, is_active) VALUES (?, ?, ?, 1)',
            (data['username'], password_hash, kitchen_password_hash),
        )
        admin_id = cursor.lastrowid
        public_token = _unique_token(db)
        slug = _unique_slug(db, data['restaurant_name'])

        profile_cursor = db.execute(
            '''
            INSERT INTO restaurant_profiles (
                admin_id, owner_name, age, email, restaurant_name, cnpj,
                restaurant_address, cell_phone, service_mode, order_payment_mode, table_count, public_token, slug
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ''',
            (
                admin_id,
                data['owner_name'],
                data['age'],
                data['email'],
                data['restaurant_name'],
                data['cnpj'],
                data['restaurant_address'],
                data['cell_phone'],
                data['service_mode'],
                data['order_payment_mode'],
                public_token,
                slug,
            ),
        )
        if referrer:
            now = datetime.utcnow().isoformat(timespec='seconds')
            db.execute(
                """
                INSERT INTO qrtotem_referrals (
                    customer_id,
                    customer_name,
                    customer_email,
                    customer_username,
                    indicated_restaurant_name,
                    indicated_contact_name,
                    indicated_contact_phone,
                    notes,
                    status,
                    approved_restaurant_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    referrer['id'],
                    referrer['name'],
                    str(referrer['email'] or '').strip().lower(),
                    referrer['username'],
                    data['restaurant_name'],
                    data['owner_name'],
                    data['cell_phone'],
                    'Indicação informada pelo restaurante no primeiro cadastro.',
                    profile_cursor.lastrowid,
                    now,
                    now,
                ),
            )

        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise ValidationError('Esse usuário já existe. Escolha outro nome de usuário.') from exc

    return {
        'admin_id': admin_id,
        'restaurant_id': profile_cursor.lastrowid,
        'username': data['username'],
        'owner_name': data['owner_name'],
        'restaurant_name': data['restaurant_name'],
        'email': data['email'],
        'cnpj': data['cnpj'],
        'restaurant_address': data['restaurant_address'],
        'cell_phone': data['cell_phone'],
        'service_mode': data['service_mode'],
        'order_payment_mode': data['order_payment_mode'],
        'age': data['age'],
        'indicated_by': data.get('indicated_by', ''),
        'table_count': 0,
        'public_token': public_token,
        'slug': slug,
    }


def validate_profile_update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    owner_name = normalize_text(payload.get('owner_name'))
    restaurant_name = normalize_text(payload.get('restaurant_name'))
    restaurant_address = _only_digits(payload.get('restaurant_address'))
    service_mode = _validate_service_mode(payload.get('service_mode'), required=True)
    order_payment_mode = _validate_order_payment_mode(payload.get('order_payment_mode'), required=True)
    email = normalize_text(payload.get('email')).lower()
    cnpj = _only_digits(payload.get('cnpj'))
    cell_phone = _only_digits(payload.get('cell_phone'))
    age = _validate_age(payload.get('age'))

    if not owner_name:
        raise ValidationError('Informe o nome.')
    if not restaurant_name:
        raise ValidationError('Informe o nome do restaurante.')
    if len(restaurant_address) != 8:
        raise ValidationError('Informe um CEP válido com 8 números.')
    if not email:
        raise ValidationError('Informe o e-mail.')

    _validate_email(email)

    if cell_phone and len(cell_phone) < 10:
        raise ValidationError('Informe um celular válido ou deixe o campo em branco.')

    return {
        'owner_name': owner_name,
        'age': age,
        'email': email,
        'restaurant_name': restaurant_name,
        'cnpj': cnpj,
        'restaurant_address': restaurant_address,
        'cell_phone': cell_phone,
        'service_mode': service_mode,
        'order_payment_mode': order_payment_mode,
    }


def update_restaurant_profile(db, admin_id: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    if not admin_id:
        raise ValidationError('Sessão inválida. Faça login novamente.')

    profile = get_restaurant_profile_for_admin(db, admin_id)

    if not profile:
        raise ValidationError('Perfil do restaurante não encontrado.')

    data = validate_profile_update_payload(payload)
    slug = _unique_slug(db, data['restaurant_name'], profile['id'])

    db.execute(
        '''
        UPDATE restaurant_profiles
           SET owner_name = ?,
               age = ?,
               email = ?,
               restaurant_name = ?,
               cnpj = ?,
               restaurant_address = ?,
               cell_phone = ?,
               service_mode = ?,
               order_payment_mode = ?,
               slug = ?
         WHERE admin_id = ?
        ''',
        (
            data['owner_name'],
            data['age'],
            data['email'],
            data['restaurant_name'],
            data['cnpj'],
            data['restaurant_address'],
            data['cell_phone'],
            data['service_mode'],
            data['order_payment_mode'],
            slug,
            admin_id,
        ),
    )
    db.commit()
    data['slug'] = slug
    return data


def get_restaurant_profile_for_admin(db, admin_id: int | None):
    if not admin_id:
        return None

    return db.execute(
        '''
        SELECT rp.*, a.username
          FROM restaurant_profiles rp
          JOIN admins a ON a.id = rp.admin_id
         WHERE rp.admin_id = ?
         LIMIT 1
        ''',
        (admin_id,),
    ).fetchone()


def get_restaurant_profile_by_token(db, public_token: str | None):
    token = normalize_text(public_token)

    if not token:
        return None

    return db.execute(
        '''
        SELECT rp.*, a.username
          FROM restaurant_profiles rp
          JOIN admins a ON a.id = rp.admin_id
         WHERE rp.public_token = ?
         LIMIT 1
        ''',
        (token,),
    ).fetchone()


def get_restaurant_profile_by_slug(db, slug: str | None):
    normalized_slug = normalize_text(slug)

    if not normalized_slug:
        return None

    return db.execute(
        '''
        SELECT rp.*, a.username
          FROM restaurant_profiles rp
          JOIN admins a ON a.id = rp.admin_id
         WHERE rp.slug = ?
         LIMIT 1
        ''',
        (normalized_slug,),
    ).fetchone()
