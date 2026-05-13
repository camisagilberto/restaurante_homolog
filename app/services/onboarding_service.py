from __future__ import annotations

import sqlite3
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


def _validate_age(value: Any) -> int:
    try:
        age = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError('Informe uma idade válida.')
    if age < 1 or age > 120:
        raise ValidationError('Informe uma idade válida.')
    return age


def validate_onboarding_payload(payload: dict[str, Any]) -> dict[str, Any]:
    owner_name = normalize_text(payload.get('owner_name'))
    restaurant_name = normalize_text(payload.get('restaurant_name'))
    restaurant_address = normalize_text(payload.get('restaurant_address'))
    username = normalize_text(payload.get('username'))
    password = str(payload.get('password') or '').strip()
    password_confirm = str(payload.get('password_confirm') or '').strip()

    email = normalize_text(payload.get('email')).lower()
    cnpj = _only_digits(payload.get('cnpj'))
    cell_phone = _only_digits(payload.get('cell_phone'))
    age = _validate_age(payload.get('age'))

    if not owner_name:
        raise ValidationError('Informe o nome.')
    if not restaurant_name:
        raise ValidationError('Informe o nome do restaurante.')
    if not restaurant_address:
        raise ValidationError('Informe o endereço do restaurante.')
    if not username:
        raise ValidationError('Informe o usuário.')
    if len(username) < 3:
        raise ValidationError('O usuário deve ter pelo menos 3 caracteres.')
    if not password:
        raise ValidationError('Informe a senha.')
    if len(password) < 4:
        raise ValidationError('A senha deve ter pelo menos 4 caracteres.')
    if password != password_confirm:
        raise ValidationError('A confirmação de senha não confere.')
    if not email:
        raise ValidationError('Informe o e-mail.')
    _validate_email(email)
    if len(cnpj) != 14:
        raise ValidationError('Informe um CNPJ válido.')
    if len(cell_phone) < 10:
        raise ValidationError('Informe um celular válido.')

    return {
        'owner_name': owner_name,
        'age': age,
        'email': email,
        'restaurant_name': restaurant_name,
        'cnpj': cnpj,
        'restaurant_address': restaurant_address,
        'cell_phone': cell_phone,
        'username': username,
        'password': password,
    }


def create_restaurant_account(db, payload: dict[str, Any]) -> dict[str, Any]:
    data = validate_onboarding_payload(payload)
    password_hash = generate_password_hash(data['password'])

    try:
        cursor = db.execute(
            'INSERT INTO admins (username, password_hash, is_active) VALUES (?, ?, 1)',
            (data['username'], password_hash),
        )
        admin_id = cursor.lastrowid
        db.execute(
            '''
            INSERT INTO restaurant_profiles (
                admin_id,
                owner_name,
                age,
                email,
                restaurant_name,
                cnpj,
                restaurant_address,
                cell_phone
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise ValidationError('Esse usuário já existe. Escolha outro nome de usuário.') from exc

    return {
        'admin_id': admin_id,
        'username': data['username'],
        'owner_name': data['owner_name'],
        'restaurant_name': data['restaurant_name'],
        'email': data['email'],
        'cnpj': data['cnpj'],
        'restaurant_address': data['restaurant_address'],
        'cell_phone': data['cell_phone'],
        'age': data['age'],
        'table_count': 0,
    }


def validate_profile_update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    owner_name = normalize_text(payload.get('owner_name'))
    restaurant_name = normalize_text(payload.get('restaurant_name'))
    restaurant_address = normalize_text(payload.get('restaurant_address'))

    email = normalize_text(payload.get('email')).lower()
    cnpj = _only_digits(payload.get('cnpj'))
    cell_phone = _only_digits(payload.get('cell_phone'))
    age = _validate_age(payload.get('age'))

    if not owner_name:
        raise ValidationError('Informe o nome.')
    if not restaurant_name:
        raise ValidationError('Informe o nome do restaurante.')
    if not restaurant_address:
        raise ValidationError('Informe o endereço do restaurante.')
    if not email:
        raise ValidationError('Informe o e-mail.')
    _validate_email(email)
    if len(cnpj) != 14:
        raise ValidationError('Informe um CNPJ válido.')
    if len(cell_phone) < 10:
        raise ValidationError('Informe um celular válido.')

    return {
        'owner_name': owner_name,
        'age': age,
        'email': email,
        'restaurant_name': restaurant_name,
        'cnpj': cnpj,
        'restaurant_address': restaurant_address,
        'cell_phone': cell_phone,
    }


def update_restaurant_profile(db, admin_id: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    if not admin_id:
        raise ValidationError('Sessão inválida. Faça login novamente.')

    data = validate_profile_update_payload(payload)
    db.execute(
        '''
        UPDATE restaurant_profiles
           SET owner_name = ?,
               age = ?,
               email = ?,
               restaurant_name = ?,
               cnpj = ?,
               restaurant_address = ?,
               cell_phone = ?
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
            admin_id,
        ),
    )
    db.commit()
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
