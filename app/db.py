from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA_SQL = '''
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS restaurant_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL UNIQUE,
    owner_name TEXT NOT NULL,
    age INTEGER NOT NULL,
    email TEXT NOT NULL,
    restaurant_name TEXT NOT NULL,
    cnpj TEXT NOT NULL,
    restaurant_address TEXT NOT NULL,
    cell_phone TEXT NOT NULL,
    table_count INTEGER NOT NULL DEFAULT 0,
    public_token TEXT,
    slug TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER,
    name TEXT NOT NULL,
    description TEXT,
    price REAL NOT NULL CHECK (price >= 0),
    category TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER,
    table_number TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'novo',
    notes TEXT,
    total_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('novo', 'preparando', 'pronto', 'entregue', 'cancelado'))
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name_snapshot TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);
'''

DEFAULT_PRODUCTS = [
    ('Hambúrguer Artesanal', 24.90, 'Lanches'),
    ('Batata Frita', 12.90, 'Acompanhamentos'),
    ('Refrigerante', 8.00, 'Bebidas'),
    ('Combo da Casa', 39.90, 'Combos'),
]


def get_db():
    if 'db' not in g:
        db = sqlite3.connect(current_app.config['DATABASE'])
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        g.db = db
    return g.db


def close_db(_exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_info(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_column(db: sqlite3.Connection, table: str, column_def: str) -> None:
    column_name = column_def.split()[0]
    if column_name not in _table_info(db, table):
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column_def}')


def _slugify(value: str) -> str:
    value = str(value or '').strip().lower()

    accents = str.maketrans(
        'áàãâäéèêëíìîïóòõôöúùûüçñ',
        'aaaaaeeeeiiiiooooouuuucn',
    )
    value = value.translate(accents)

    value = re.sub(r'[^a-z0-9]+', '-', value)
    value = re.sub(r'-+', '-', value).strip('-')
    return value or 'restaurante'


def _unique_slug(db: sqlite3.Connection, base: str, current_id: int | None = None) -> str:
    slug = _slugify(base)
    candidate = slug
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

        candidate = f'{slug}-{counter}'
        counter += 1


def _unique_token(db: sqlite3.Connection) -> str:
    while True:
        token = secrets.token_urlsafe(10).replace('-', '').replace('_', '')[:12]
        row = db.execute(
            'SELECT id FROM restaurant_profiles WHERE public_token = ?',
            (token,),
        ).fetchone()
        if not row:
            return token


def _migrate_admin_passwords(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'admins'):
        return

    columns = _table_info(db, 'admins')

    if 'password_hash' not in columns:
        _ensure_column(db, 'admins', 'password_hash TEXT')

    if 'is_active' not in columns:
        _ensure_column(db, 'admins', 'is_active INTEGER NOT NULL DEFAULT 1')

    columns = _table_info(db, 'admins')

    if 'password' in columns:
        rows = db.execute('SELECT id, password, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                continue

            raw = current_hash or row['password'] or os.getenv('ADMIN_PASSWORD', '123456')
            if not str(raw).startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                raw = generate_password_hash(str(raw))

            db.execute(
                'UPDATE admins SET password_hash = ? WHERE id = ?',
                (raw, row['id']),
            )
    else:
        rows = db.execute('SELECT id, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if not current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                db.execute(
                    'UPDATE admins SET password_hash = ? WHERE id = ?',
                    (
                        generate_password_hash(current_hash or os.getenv('ADMIN_PASSWORD', '123456')),
                        row['id'],
                    ),
                )


def _ensure_default_profile(db: sqlite3.Connection) -> int:
    admin = db.execute(
        'SELECT id, username FROM admins ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if not admin:
        return 0

    profile = db.execute(
        'SELECT id FROM restaurant_profiles WHERE admin_id = ?',
        (admin['id'],),
    ).fetchone()

    if profile:
        return profile['id']

    cursor = db.execute(
        '''
        INSERT INTO restaurant_profiles (
            admin_id,
            owner_name,
            age,
            email,
            restaurant_name,
            cnpj,
            restaurant_address,
            cell_phone,
            table_count,
            public_token,
            slug
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ''',
        (
            admin['id'],
            'Administrador',
            18,
            'admin@example.com',
            'Restaurante Demo',
            '00000000000000',
            'Endereço não informado',
            '00000000000',
            _unique_token(db),
            _unique_slug(db, 'Restaurante Demo'),
        ),
    )
    return cursor.lastrowid


def _seed_defaults(db: sqlite3.Connection) -> None:
    default_admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    default_admin_password = os.getenv('ADMIN_PASSWORD', '123456')

    if db.execute('SELECT COUNT(*) FROM admins').fetchone()[0] == 0:
        db.execute(
            'INSERT INTO admins (username, password_hash, is_active) VALUES (?, ?, 1)',
            (default_admin_username, generate_password_hash(default_admin_password)),
        )

    default_restaurant_id = _ensure_default_profile(db)

    if default_restaurant_id and db.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        for index, (name, price, category) in enumerate(DEFAULT_PRODUCTS):
            db.execute(
                '''
                INSERT INTO products (
                    restaurant_id,
                    name,
                    price,
                    category,
                    active,
                    sort_order
                )
                VALUES (?, ?, ?, ?, 1, ?)
                ''',
                (
                    default_restaurant_id,
                    name,
                    price,
                    category,
                    index,
                ),
            )


def _migrate_restaurant_profiles(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'restaurant_profiles'):
        return

    _ensure_column(db, 'restaurant_profiles', 'table_count INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'restaurant_profiles', 'public_token TEXT')
    _ensure_column(db, 'restaurant_profiles', 'slug TEXT')

    rows = db.execute(
        'SELECT id, restaurant_name, public_token, slug FROM restaurant_profiles'
    ).fetchall()

    for row in rows:
        token = row['public_token'] or _unique_token(db)
        slug = row['slug'] or _unique_slug(db, row['restaurant_name'], row['id'])
        db.execute(
            '''
            UPDATE restaurant_profiles
               SET public_token = ?,
                   slug = ?
             WHERE id = ?
            ''',
            (token, slug, row['id']),
        )


def _migrate_products(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'products'):
        return

    _ensure_column(db, 'products', 'restaurant_id INTEGER')
    _ensure_column(db, 'products', 'description TEXT')
    _ensure_column(db, 'products', 'sort_order INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'products', 'created_at TEXT')
    _ensure_column(db, 'products', 'updated_at TEXT')

    default_profile = db.execute(
        'SELECT id FROM restaurant_profiles ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if default_profile:
        db.execute(
            'UPDATE products SET restaurant_id = COALESCE(restaurant_id, ?)',
            (default_profile['id'],),
        )


def _migrate_orders(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'orders'):
        return

    _ensure_column(db, 'orders', 'restaurant_id INTEGER')
    _ensure_column(db, 'orders', 'customer_name TEXT NOT NULL DEFAULT ""')
    _ensure_column(db, 'orders', 'notes TEXT')
    _ensure_column(db, 'orders', 'total_amount REAL NOT NULL DEFAULT 0')
    _ensure_column(db, 'orders', 'updated_at TEXT')

    default_profile = db.execute(
        'SELECT id FROM restaurant_profiles ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if default_profile:
        db.execute(
            'UPDATE orders SET restaurant_id = COALESCE(restaurant_id, ?)',
            (default_profile['id'],),
        )

    db.execute('UPDATE orders SET customer_name = COALESCE(customer_name, "")')


def _migrate_order_items(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'order_items'):
        return

    _ensure_column(db, 'order_items', 'product_name_snapshot TEXT NOT NULL DEFAULT ""')


def _create_indexes(db: sqlite3.Connection) -> None:
    if _table_exists(db, 'restaurant_profiles'):
        columns = _table_info(db, 'restaurant_profiles')
        if 'public_token' in columns:
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_profiles_public_token ON restaurant_profiles(public_token)'
            )
        if 'slug' in columns:
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_profiles_slug ON restaurant_profiles(slug)'
            )

    if _table_exists(db, 'products'):
        columns = _table_info(db, 'products')
        if {'restaurant_id', 'active', 'category', 'name'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_products_restaurant_active_category ON products(restaurant_id, active, category, name)'
            )

    if _table_exists(db, 'orders'):
        columns = _table_info(db, 'orders')
        if {'restaurant_id', 'status', 'created_at'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_restaurant_status_created ON orders(restaurant_id, status, created_at)'
            )
        if {'restaurant_id', 'table_number', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_restaurant_table_status ON orders(restaurant_id, table_number, status)'
            )

    if _table_exists(db, 'order_items'):
        columns = _table_info(db, 'order_items')
        if 'order_id' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)'
            )


def migrate_schema(db: sqlite3.Connection) -> None:
    _migrate_admin_passwords(db)
    _migrate_restaurant_profiles(db)
    _migrate_products(db)
    _migrate_orders(db)
    _migrate_order_items(db)


def _backfill_timestamps(db: sqlite3.Connection) -> None:
    now = datetime.utcnow().isoformat(timespec='seconds')

    if _table_exists(db, 'products'):
        columns = _table_info(db, 'products')
        if 'created_at' in columns:
            db.execute('UPDATE products SET created_at = COALESCE(created_at, ?)', (now,))
        if 'updated_at' in columns:
            db.execute('UPDATE products SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'orders'):
        columns = _table_info(db, 'orders')
        if 'updated_at' in columns:
            db.execute('UPDATE orders SET updated_at = COALESCE(updated_at, ?)', (now,))


def init_db(app):
    @app.teardown_appcontext
    def _close_db(exception):
        close_db(exception)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db = get_db()

        db.executescript(SCHEMA_SQL)
        _seed_defaults(db)
        migrate_schema(db)
        _backfill_timestamps(db)
        _create_indexes(db)

        db.commit()
