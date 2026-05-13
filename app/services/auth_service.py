from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash

_HASH_PREFIXES = ('pbkdf2:', 'scrypt:', 'argon2:')


def _table_columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _stored_password_matches(stored: str, password: str) -> bool:
    if not stored:
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


def authenticate_admin(db, username: str, password: str):
    columns = _table_columns(db, 'admins')
    password_column = _password_column(db)
    active_expr = _active_expr(columns)

    row = db.execute(
        f'SELECT id, username, {password_column} AS password_value, {active_expr} AS is_active FROM admins WHERE username = ? LIMIT 1',
        (username,),
    ).fetchone()
    if not row or not row['is_active']:
        return None

    stored = str(row['password_value'] or '')
    if _stored_password_matches(stored, password):
        if password_column == 'password' and 'password_hash' in columns and not stored.startswith(_HASH_PREFIXES):
            db.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (generate_password_hash(password), row['id']))
            db.commit()
        return row
    return None


def verify_manager_password(db, password: str) -> bool:
    columns = _table_columns(db, 'admins')
    password_column = _password_column(db)
    active_expr = _active_expr(columns)

    rows = db.execute(
        f'SELECT id, {password_column} AS password_value, {active_expr} AS is_active FROM admins'
    ).fetchall()

    for row in rows:
        if not row['is_active']:
            continue
        stored = str(row['password_value'] or '')
        if _stored_password_matches(stored, password):
            if password_column == 'password' and 'password_hash' in columns and not stored.startswith(_HASH_PREFIXES):
                db.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (generate_password_hash(password), row['id']))
                db.commit()
            return True
    return False
