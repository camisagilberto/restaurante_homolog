from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

_HASH_PREFIXES = ('pbkdf2:', 'scrypt:', 'argon2:')
RESET_TOKEN_TTL_MINUTES = 30


def _table_columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _stored_password_matches(stored: str, password: str) -> bool:
    if not stored or not password:
        return False

    if stored.startswith(_HASH_PREFIXES):
        return check_password_hash(stored, password)

    return stored == password


def _password_column(db) -> str:
    columns = _table_columns(db, 'admins')

    if 'password_hash' in columns:
        return 'password_hash'
    if 'password' in columns:
        return 'password'

    raise RuntimeError('Tabela admins não possui coluna de senha.')


def _active_expr(columns: set[str]) -> str:
    if 'is_active' in columns:
        return 'is_active'
    if 'is active' in columns:
        return '"is active"'

    return '1'


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def validate_new_password(password: str, confirmation: str | None = None, *, label: str = 'senha') -> str:
    password = str(password or '').strip()
    confirmation = str(confirmation or '').strip() if confirmation is not None else password

    if not password:
        raise ValueError(f'Informe a {label}.')
    if len(password) < 8:
        raise ValueError(f'A {label} deve ter pelo menos 8 caracteres.')
    if password != confirmation:
        raise ValueError(f'A confirmação da {label} não confere.')

    return password


def authenticate_admin(db, username: str, password: str):
    columns = _table_columns(db, 'admins')
    password_column = _password_column(db)
    active_expr = _active_expr(columns)

    admin_password_column = f'a.{password_column}'
    admin_active_expr = '1'
    if active_expr == 'is_active':
        admin_active_expr = 'a.is_active'
    elif active_expr == '"is active"':
        admin_active_expr = 'a."is active"'

    identifier = str(username or '').strip()

    row = db.execute(
        f"""
        SELECT a.id,
               a.username,
               {admin_password_column} AS password_value,
               {admin_active_expr} AS is_active
          FROM admins a
          LEFT JOIN restaurant_profiles rp ON rp.admin_id = a.id
         WHERE lower(a.username) = lower(?)
            OR lower(rp.email) = lower(?)
         ORDER BY a.id DESC
         LIMIT 1
        """,
        (identifier, identifier),
    ).fetchone()

    if not row or not row['is_active']:
        return None

    stored = str(row['password_value'] or '')

    if _stored_password_matches(stored, password):
        if password_column == 'password' and 'password_hash' in columns and not stored.startswith(_HASH_PREFIXES):
            db.execute(
                'UPDATE admins SET password_hash = ? WHERE id = ?',
                (generate_password_hash(password), row['id']),
            )
            db.commit()

        return row

    return None

def verify_manager_password(db, password: str, *, admin_id: int | None = None) -> bool:
    columns = _table_columns(db, 'admins')
    password_column = _password_column(db)
    active_expr = _active_expr(columns)

    params = []
    where = ''

    if admin_id:
        where = ' WHERE id = ?'
        params.append(admin_id)

    rows = db.execute(
        f'SELECT id, {password_column} AS password_value, {active_expr} AS is_active FROM admins{where}',
        params,
    ).fetchall()

    for row in rows:
        if not row['is_active']:
            continue

        stored = str(row['password_value'] or '')

        if _stored_password_matches(stored, password):
            if password_column == 'password' and 'password_hash' in columns and not stored.startswith(_HASH_PREFIXES):
                db.execute(
                    'UPDATE admins SET password_hash = ? WHERE id = ?',
                    (generate_password_hash(password), row['id']),
                )
                db.commit()

            return True

    return False


def verify_kitchen_password(db, password: str, *, admin_id: int | None = None, allow_manager_password: bool = True) -> bool:
    if not password:
        return False

    columns = _table_columns(db, 'admins')
    if 'kitchen_password_hash' not in columns:
        return verify_manager_password(db, password, admin_id=admin_id) if allow_manager_password else False

    params = []
    where = ''

    if admin_id:
        where = ' WHERE id = ?'
        params.append(admin_id)

    rows = db.execute(
        f'SELECT id, kitchen_password_hash, {_active_expr(columns)} AS is_active FROM admins{where}',
        params,
    ).fetchall()

    for row in rows:
        if row['is_active'] and _stored_password_matches(str(row['kitchen_password_hash'] or ''), password):
            return True

    return verify_manager_password(db, password, admin_id=admin_id) if allow_manager_password else False


def update_manager_password(db, admin_id: int | None, current_password: str, new_password: str, confirmation: str) -> None:
    if not admin_id:
        raise ValueError('Sessão inválida. Faça login novamente.')
    if not verify_manager_password(db, current_password, admin_id=admin_id):
        raise ValueError('Senha atual inválida.')

    password = validate_new_password(new_password, confirmation, label='senha do usuário')
    db.execute(
        'UPDATE admins SET password_hash = ?, reset_token_hash = NULL, reset_token_expires_at = NULL WHERE id = ?',
        (generate_password_hash(password), admin_id),
    )
    db.commit()


def update_kitchen_password(db, admin_id: int | None, current_manager_password: str, new_password: str, confirmation: str) -> None:
    if not admin_id:
        raise ValueError('Sessão inválida. Faça login novamente.')
    if not verify_manager_password(db, current_manager_password, admin_id=admin_id):
        raise ValueError('Senha do usuário inválida.')

    password = validate_new_password(new_password, confirmation, label='senha da cozinha')
    db.execute(
        'UPDATE admins SET kitchen_password_hash = ? WHERE id = ?',
        (generate_password_hash(password), admin_id),
    )
    db.commit()


def request_password_reset(db, identifier: str) -> str | None:
    identifier = str(identifier or '').strip().lower()
    if not identifier:
        return None

    row = db.execute(
        '''
        SELECT a.id
          FROM admins a
          LEFT JOIN restaurant_profiles rp ON rp.admin_id = a.id
         WHERE lower(a.username) = lower(?)
            OR lower(rp.email) = lower(?)
         LIMIT 1
        ''',
        (identifier, identifier),
    ).fetchone()

    if not row:
        return None

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)).isoformat(timespec='seconds')
    db.execute(
        'UPDATE admins SET reset_token_hash = ?, reset_token_expires_at = ? WHERE id = ?',
        (_hash_reset_token(token), expires_at, row['id']),
    )
    db.commit()
    return token


def reset_password_with_token(db, token: str, new_password: str, confirmation: str) -> None:
    token = str(token or '').strip()
    password = validate_new_password(new_password, confirmation, label='nova senha')

    if not token:
        raise ValueError('Link de recuperação inválido.')

    row = db.execute(
        '''
        SELECT id, reset_token_expires_at
          FROM admins
         WHERE reset_token_hash = ?
         LIMIT 1
        ''',
        (_hash_reset_token(token),),
    ).fetchone()

    if not row or not row['reset_token_expires_at']:
        raise ValueError('Link de recuperação inválido ou expirado.')

    try:
        expires_at = datetime.fromisoformat(row['reset_token_expires_at'])
    except ValueError as exc:
        raise ValueError('Link de recuperação inválido ou expirado.') from exc

    if datetime.utcnow() > expires_at:
        db.execute('UPDATE admins SET reset_token_hash = NULL, reset_token_expires_at = NULL WHERE id = ?', (row['id'],))
        db.commit()
        raise ValueError('Link de recuperação expirado. Solicite uma nova recuperação.')

    db.execute(
        '''
        UPDATE admins
           SET password_hash = ?,
               reset_token_hash = NULL,
               reset_token_expires_at = NULL
         WHERE id = ?
        ''',
        (generate_password_hash(password), row['id']),
    )
    db.commit()
